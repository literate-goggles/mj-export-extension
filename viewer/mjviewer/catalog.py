"""Hosted mode: the published catalog and the verified ZIP cache.

A deployment publishes exports to a private blob container that holds
catalog.json and exports/<sha256>.zip. The server reads both with a read-only
query string (for example an Azure container SAS with sp=rl). That query string
is treated like a password: no log line, API response, error message or
exception ever contains it, and redirects are never followed.

Opening a catalog entry downloads its ZIP in the background into
<cache>/zips/<sha256>.zip: the bytes stream into a unique temporary file in the
same folder, size and SHA-256 are checked against the catalog, and the file is
renamed atomically and made read-only. The export is then opened with the same
ZIP reader local mode uses. Several server instances may share the cache
folder: a lock file lets one of them download a ZIP while the others report its
progress and wait, every process verifies a present ZIP once before using it,
and every data file is written as a temporary file plus an atomic rename. ZIPs
are evicted least recently used first when the cache would pass its size limit
(a ZIP used in the last 10 minutes is never evicted: the download fails
instead), and every minute ZIPs (and thumbnails) of exports the catalog no
longer lists are deleted once unused for 10 minutes.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import __version__
from .model import iso_utc, parse_date
from .sources import CHUNK_SIZE, SourceError

CATALOG_NAME = 'catalog.json'
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_EXPORT_BYTES = 256 * 1024 ** 3
MIN_ZIP_BYTES = 22  # an empty ZIP is just its end record
VERIFY_CHUNK = 1024 * 1024
SHA_RE = re.compile(r'[0-9a-f]{64}')
# Hosted export ids: the catalog id, plus -<n> for the n-th root when a ZIP holds several.
EXPORT_ID_RE = re.compile(r'([0-9a-f]{16})(?:-([1-9][0-9]{0,3}))?')
ZIP_NAME_RE = re.compile(r'([0-9a-f]{64})\.zip')
LOCK_NAME_RE = re.compile(r'\.dl-([0-9a-f]{64})\.lock')
LOCK_STALE_S = 60.0      # a download lock nobody touched for this long is abandoned
HEARTBEAT_S = 5.0        # how often a downloading process touches its lock
PART_STALE_S = 600.0     # an untouched temporary download file older than this is an orphan
PRUNE_MIN_AGE_S = 600.0  # ZIPs used this recently are never pruned or evicted (another instance may use them)
TOUCH_EVERY_S = PRUNE_MIN_AGE_S / 2  # a ZIP in use is touched this often, so it always looks recently used
POLL_S = 1.0
CLOSE_WAIT_S = 5.0       # on shutdown, how long preparations get to remove their temporary files and locks
BUSY = ('queued', 'waiting', 'downloading', 'verifying', 'opening')
LOOPBACK = ('localhost', '127.0.0.1')
USER_AGENT = 'mjviewer/%s' % __version__
SUMMARY_COUNTS = ('jobs', 'moodboards', 'profiles', 'images')
SUMMARY_TEXT = ('extension_version', 'status')
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]')


class CatalogError(Exception):
    """A catalog, storage or cache problem. The message is safe to show and to log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Storage and sign-in endpoints answer directly; a redirect is an error, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def opener_for(url: str) -> urllib.request.OpenerDirector:
    handlers = [_NoRedirect()]
    if (urllib.parse.urlsplit(url).hostname or '').lower() in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))  # test servers on this machine: never via a proxy
    return urllib.request.build_opener(*handlers)


def describe(exc: BaseException) -> str:
    """Content-free description of a network failure. Never includes a URL."""
    if isinstance(exc, urllib.error.HTTPError):
        return 'HTTP %d' % exc.code
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, str):
        return 'request refused'
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return 'timed out'
    if isinstance(reason, ssl.SSLError):
        return 'TLS error'
    if isinstance(reason, ConnectionRefusedError):
        return 'connection refused'
    if isinstance(reason, socket.gaierror):
        return 'host name not found'
    if isinstance(reason, OSError):
        return 'connection failed: %s' % (reason.strerror or type(reason).__name__)
    if isinstance(reason, http.client.HTTPException):
        return 'invalid HTTP response (%s)' % type(reason).__name__
    return type(reason).__name__


def _http_problem(what: str, status: int) -> CatalogError:
    if status == 403:
        return CatalogError('forbidden', '%s: storage refused the request (HTTP 403); '
                                         'the read-only access may have expired' % what)
    if status == 404:
        return CatalogError('not_found', '%s: not found in storage (HTTP 404)' % what)
    if 300 <= status < 400:
        return CatalogError('redirected', '%s: storage answered with a redirect (HTTP %d), '
                                          'which is not followed' % (what, status))
    return CatalogError('storage_error', '%s: storage answered HTTP %d' % (what, status))


def _secret_parts(query: str) -> List[str]:
    """The query string and its long values, in the forms they could take inside a message."""
    found = {query}
    for _, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if len(value) >= 16:
            found.update({value, urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value)})
    return sorted((s for s in found if len(s) >= 8), key=len, reverse=True)


class BlobStore:
    """Objects of one private container, read with a fixed read-only query string."""

    def __init__(self, url: str, timeout: float = 30.0):
        parts = urllib.parse.urlsplit(url)
        self._base = '%s://%s%s' % (parts.scheme, parts.netloc, parts.path.rstrip('/'))
        self._query = parts.query
        self._secrets = _secret_parts(parts.query)
        self._opener = opener_for(url)
        self.timeout = timeout

    def __repr__(self) -> str:
        return '<BlobStore>'

    def url(self, name: str) -> str:
        return '%s/%s?%s' % (self._base, urllib.parse.quote(name, safe='/'), self._query)

    def scrub(self, text: str) -> str:
        """Defence in depth: blank out the query string should it ever reach a message."""
        for secret in self._secrets:
            text = text.replace(secret, '***')
        return text

    def _request(self, name: str, headers: Dict[str, str]) -> urllib.request.Request:
        sent = {'User-Agent': USER_AGENT}
        sent.update(headers)
        return urllib.request.Request(self.url(name), headers=sent)

    def get(self, name: str, what: str, etag: Optional[str] = None, max_bytes: int = MAX_CATALOG_BYTES,
            timeout: Optional[float] = None) -> Tuple[int, Optional[str], bytes]:
        """(status, etag, body) of a small object; (304, etag, b'') while etag still matches."""
        try:
            with self._opener.open(self._request(name, {'If-None-Match': etag} if etag else {}),
                                   timeout=timeout or self.timeout) as response:
                body = response.read(max_bytes + 1)
                status, tag = response.status, response.headers.get('ETag')
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 304:
                return 304, etag, b''
            raise _http_problem(what, exc.code) from None
        except Exception as exc:
            raise CatalogError('unreachable', '%s: storage unreachable (%s)' % (what, describe(exc))) from None
        if len(body) > max_bytes:
            raise CatalogError('too_large', '%s is larger than %d bytes' % (what, max_bytes))
        return status, tag, body

    @contextlib.contextmanager
    def stream(self, name: str, what: str):
        try:
            response = self._opener.open(self._request(name, {}), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise _http_problem(what, exc.code) from None
        except Exception as exc:
            raise CatalogError('unreachable', '%s: storage unreachable (%s)' % (what, describe(exc))) from None
        try:
            yield response
        finally:
            response.close()


# ---------------------------------------------------------------- catalog

def _plain(value, limit: int) -> Optional[str]:
    """value when it is a non-empty string of at most limit characters without control characters."""
    if isinstance(value, str) and 0 < len(value) <= limit and not _CONTROL.search(value):
        return value
    return None


def _count(value) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _entry(item) -> Tuple[Optional[dict], str]:
    """A validated catalog entry, or (None, reason). Reasons never quote the entry."""
    if not isinstance(item, dict):
        return None, 'not an object'
    sha = item.get('sha256')
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        return None, 'sha256 is not 64 lowercase hex characters'
    if item.get('id') != sha[:16]:
        return None, 'id is not the first 16 characters of sha256'
    if item.get('blob') != 'exports/%s.zip' % sha:
        return None, 'blob is not exports/<sha256>.zip'
    size = item.get('size')
    if not isinstance(size, int) or isinstance(size, bool) or not MIN_ZIP_BYTES <= size <= MAX_EXPORT_BYTES:
        return None, 'size is not a byte count within the limit'
    name = _plain(item.get('name'), 255)
    if name is None or '/' in name or '\\' in name:
        return None, 'name is not a plain file name'
    label = item.get('label')
    if label == '':
        label = None
    if label is not None and _plain(label, 200) is None:
        return None, 'label is not a short text'
    published_at = item.get('published_at')
    if published_at is not None:
        published_at = iso_utc(parse_date(published_at)) if _plain(published_at, 64) else None
        if published_at is None:
            return None, 'published_at is not a date'
    published_by = item.get('published_by')
    if published_by == '':
        published_by = None
    if published_by is not None and _plain(published_by, 200) is None:
        return None, 'published_by is not a short text'
    raw_summary = item.get('summary')
    if raw_summary is None:
        raw_summary = {}
    if not isinstance(raw_summary, dict):
        return None, 'summary is not an object'
    summary = {}
    for key in SUMMARY_COUNTS:
        if not _count(raw_summary.get(key)):
            return None, 'summary.%s is not a count' % key
        summary[key] = raw_summary.get(key)
    for key in SUMMARY_TEXT:
        value = raw_summary.get(key)
        if value is not None and _plain(value, 64) is None:
            return None, 'summary.%s is not a short text' % key
        summary[key] = value
    export_time = raw_summary.get('export_time')
    summary['export_time'] = iso_utc(parse_date(export_time)) if isinstance(export_time, (str, int, float)) \
        and not isinstance(export_time, bool) else None
    return {'id': sha[:16], 'sha256': sha, 'blob': 'exports/%s.zip' % sha, 'size': size, 'name': name,
            'label': label, 'published_at': published_at, 'published_by': published_by, 'summary': summary}, ''


def _reject_constant(name: str):
    raise ValueError('%s is not valid JSON' % name)


def parse_catalog(raw: bytes) -> Tuple[List[dict], List[dict], Optional[str]]:
    """(entries, skipped, updated_at). Bad entries are skipped with a reason, never used."""
    try:
        data = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CatalogError('bad_catalog', 'catalog.json is not valid JSON') from None
    version = data.get('version') if isinstance(data, dict) else None
    if type(version) is not int or version != 1:
        raise CatalogError('bad_catalog', 'catalog.json is not a version 1 catalog')
    items = data.get('exports')
    if not isinstance(items, list):
        raise CatalogError('bad_catalog', 'catalog.json has no exports list')
    entries: List[dict] = []
    skipped: List[dict] = []
    seen: Set[str] = set()
    for n, item in enumerate(items[:MAX_ENTRIES]):
        try:
            entry, reason = _entry(item)
        except Exception as exc:  # whatever the entry holds, it only skips itself
            entry, reason = None, 'unreadable entry (%s)' % type(exc).__name__
        if entry is not None and entry['id'] in seen:
            entry, reason = None, 'duplicate id; the first entry is used'
        if entry is None:
            skipped.append({'index': n, 'reason': reason})
            continue
        seen.add(entry['id'])
        entries.append(entry)
    if len(items) > MAX_ENTRIES:
        skipped.append({'index': MAX_ENTRIES, 'reason': 'more than %d entries; the rest are ignored' % MAX_ENTRIES})
    updated = data.get('updated_at')
    return entries, skipped, iso_utc(parse_date(updated)) if _plain(updated, 64) else None


class Catalog:
    """catalog.json, refetched with If-None-Match once ttl_s has passed. Errors keep the last good copy."""

    def __init__(self, store: BlobStore, ttl_s: float = 60.0, log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.monotonic, timeout: float = 10.0):
        self.store = store
        self.ttl_s = ttl_s
        self.timeout = timeout
        self._log = log or (lambda message: None)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: List[dict] = []
        self._by_id: Dict[str, dict] = {}
        self._skipped: List[dict] = []
        self._updated_at: Optional[str] = None
        self._etag: Optional[str] = None
        self._fetched: Optional[float] = None  # clock time of the last good answer (200 or 304)
        self._checked: Optional[float] = None  # clock time of the last attempt
        self._ok = False
        self._error: Optional[str] = None
        self._inflight: Optional[threading.Event] = None

    def refresh(self, force: bool = False) -> None:
        """Fetch when stale, one request at a time. The caller that finds it stale fetches; others
        meanwhile keep the last good catalog, and wait only while there is none yet."""
        with self._lock:
            if not (force or self._checked is None or self._clock() - self._checked >= self.ttl_s):
                return
            event = self._inflight
            leader = event is None
            if leader:
                event = self._inflight = threading.Event()
            loaded = self._fetched is not None
        if not leader:
            if not loaded:
                event.wait(self.timeout + 5)
            return
        try:
            self._fetch()
        finally:
            with self._lock:
                self._inflight = None
            event.set()

    def _fetch(self) -> None:
        with self._lock:
            etag = self._etag
        try:
            status, tag, body = self.store.get(CATALOG_NAME, 'catalog.json', etag=etag, timeout=self.timeout)
            if status == 304:
                with self._lock:
                    self._fetched = self._checked = self._clock()
                    self._ok, self._error = True, None
                return
            if status != 200:
                raise CatalogError('storage_error', 'catalog.json: storage answered HTTP %d' % status)
            entries, skipped, updated = parse_catalog(body)
        except Exception as exc:  # CatalogError, or a bug: either way the last good catalog stays in use
            message = self.store.scrub(exc.message if isinstance(exc, CatalogError)
                                       else 'catalog.json could not be read (%s)' % type(exc).__name__)
            with self._lock:
                changed = message != self._error
                self._checked = self._clock()
                self._ok, self._error = False, message
            if changed:
                self._log('catalog: %s; keeping the last good catalog' % message)
            return
        with self._lock:
            self._entries, self._skipped, self._updated_at = entries, skipped, updated
            self._by_id = {entry['id']: entry for entry in entries}
            self._etag = tag
            self._fetched = self._checked = self._clock()
            self._ok, self._error = True, None
        self._log('catalog: %d export(s) listed%s' % (len(entries), ''.join(
            '; entry %d skipped (%s)' % (s['index'], s['reason']) for s in skipped[:20])))

    @property
    def loaded(self) -> bool:
        """True once a catalog has been read (deleting unlisted files needs one)."""
        with self._lock:
            return self._fetched is not None

    def entries(self) -> List[dict]:
        self.refresh()
        with self._lock:
            return list(self._entries)

    def entry(self, catalog_id: str) -> Optional[dict]:
        self.refresh()
        with self._lock:
            return self._by_id.get(catalog_id)

    def status(self) -> dict:
        with self._lock:
            age = None if self._fetched is None else round(self._clock() - self._fetched, 1)
            return {'ok': self._ok, 'updated_at': self._updated_at, 'age_s': age, 'error': self._error,
                    'skipped': [dict(s) for s in self._skipped], 'exports': len(self._entries)}

    def health(self) -> dict:
        status = self.status()
        return {'exports': status['exports'], 'ok': status['ok'], 'age_s': status['age_s']}


# ------------------------------------------------------------------ cache

def _size_text(size: int) -> str:
    return '%.1f GiB' % (size / 1024 ** 3) if size >= 1024 ** 3 else '%.1f MiB' % (size / 1024 ** 2)


class ZipCache:
    """Verified copies of published ZIPs in <cache>/zips, safe to share between processes."""

    def __init__(self, cache_dir: str, max_bytes: int, log: Optional[Callable[[str], None]] = None,
                 wall: Callable[[], float] = time.time):
        self.dir = os.path.join(cache_dir, 'zips')
        self.max_bytes = max_bytes
        self.owner = secrets.token_hex(8)  # tells this process's temporary files and locks apart
        self._log = log or (lambda message: None)
        self._wall = wall
        self._lock = threading.Lock()
        self._verified: Dict[str, tuple] = {}
        self._touched: Dict[str, float] = {}

    def path(self, sha: str) -> str:
        return os.path.join(self.dir, sha + '.zip')

    def _lock_path(self, sha: str) -> str:
        return os.path.join(self.dir, '.dl-%s.lock' % sha)

    @staticmethod
    def _signature(path: str) -> Optional[tuple]:
        """Size and file identity; a file replaced by another process gets a new identity. Anything
        but a regular file (a symbolic link, say) counts as absent and is never followed."""
        try:
            info = os.lstat(path)
        except OSError:
            return None
        return (info.st_size, info.st_ino, info.st_dev) if stat.S_ISREG(info.st_mode) else None

    def _remove(self, path: str) -> None:
        try:
            try:
                os.unlink(path)  # removes a link itself, never its target
            except PermissionError:
                # A shared cache may refuse to delete a read-only file: make a regular file writable, then retry.
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    raise
                os.chmod(path, 0o644)
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._log('cache: cannot delete %s (%s)' % (os.path.basename(path), exc.strerror or type(exc).__name__))

    def _forget(self, sha: str) -> None:
        with self._lock:
            self._verified.pop(sha, None)
            self._touched.pop(sha, None)

    def present(self, sha: str) -> bool:
        return self._signature(self.path(sha)) is not None

    def is_verified(self, sha: str, size: int) -> bool:
        signature = self._signature(self.path(sha))
        with self._lock:
            return signature is not None and signature[0] == size and self._verified.get(sha) == signature

    def verify(self, sha: str, size: int, progress: Callable[[int], None], cancel: threading.Event) -> bool:
        """Hash a present ZIP once per process. False when it is not the published file."""
        path = self.path(sha)
        signature = self._signature(path)
        if signature is None or signature[0] != size:
            return False
        digest = hashlib.sha256()
        done = 0
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                         | getattr(os, 'O_BINARY', 0))
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())  # the file actually read is the one marked verified
                if not stat.S_ISREG(info.st_mode):
                    return False
                signature = (info.st_size, info.st_ino, info.st_dev)
                while True:
                    if cancel.is_set():
                        raise CatalogError('cancelled', 'the server is stopping')
                    chunk = handle.read(VERIFY_CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    done += len(chunk)
                    progress(done)
        except OSError as exc:
            raise CatalogError('cache_unreadable', 'the cached ZIP cannot be read (%s)'
                               % (exc.strerror or type(exc).__name__)) from None
        if done != size or digest.hexdigest() != sha:
            return False
        with self._lock:
            self._verified[sha] = signature
        return True

    def discard(self, sha: str) -> None:
        self._remove(self.path(sha))
        self._forget(sha)

    def touch(self, sha: str, every_s: float = 0.0) -> None:
        """Mark a ZIP as used (its mtime orders the eviction), at most once per every_s."""
        now = self._wall()
        with self._lock:
            last = self._touched.get(sha)
            if last is not None and now - last < every_s:
                return
            self._touched[sha] = now
        try:
            os.utime(self.path(sha))
        except OSError:
            pass

    # Locks: one process downloads a ZIP; the others wait for the file and show its progress.

    def try_lock(self, sha: str) -> bool:
        path = self._lock_path(sha)
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:
            raise CatalogError('cache_unwritable', 'cannot write to the cache (%s)'
                               % (exc.strerror or type(exc).__name__)) from None
        for attempt in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except OSError as exc:
                if not isinstance(exc, FileExistsError):
                    raise CatalogError('cache_unwritable', 'cannot write to the cache (%s)'
                                       % (exc.strerror or type(exc).__name__)) from None
                try:
                    age = self._wall() - os.stat(path).st_mtime
                except FileNotFoundError:
                    continue  # released meanwhile
                except OSError:
                    return False
                if age <= LOCK_STALE_S or attempt:
                    return False
                self._remove(path)  # its owner stopped updating it
                continue
            try:
                with os.fdopen(fd, 'w') as handle:
                    handle.write(self.owner)
            except OSError as exc:
                self._remove(path)
                raise CatalogError('cache_unwritable', 'cannot write to the cache (%s)'
                                   % (exc.strerror or type(exc).__name__)) from None
            return True
        return False

    def unlock(self, sha: str) -> None:
        path = self._lock_path(sha)
        try:
            with open(path, 'r') as handle:
                owner = handle.read(64)
        except OSError:
            return
        if owner == self.owner:
            self._remove(path)

    def locked_elsewhere(self, sha: str) -> bool:
        try:
            info = os.stat(self._lock_path(sha))
        except OSError:
            return False
        return self._wall() - info.st_mtime <= LOCK_STALE_S

    def foreign_progress(self, sha: str) -> Optional[int]:
        """Bytes another process has downloaded so far, from its temporary file."""
        prefix, mine = '.dl-%s-' % sha, '-%s-' % self.owner
        best = None
        try:
            with os.scandir(self.dir) as it:
                for entry in it:
                    if not (entry.name.startswith(prefix) and entry.name.endswith('.part')) or mine in entry.name:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if self._wall() - info.st_mtime <= LOCK_STALE_S and (best is None or info.st_size > best):
                        best = info.st_size
        except OSError:
            return None
        return best

    def download(self, entry: dict, store: BlobStore, progress: Callable[[int], None],
                 cancel: threading.Event) -> None:
        """Stream the published ZIP into the cache, checking size and SHA-256 before it is visible."""
        sha, size = entry['sha256'], entry['size']
        final = self.path(sha)
        os.makedirs(self.dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix='.dl-%s-%s-' % (sha, self.owner), suffix='.part')
        try:
            digest = hashlib.sha256()
            done = 0
            beat = self._wall()
            with os.fdopen(fd, 'wb') as out:
                with store.stream(entry['blob'], 'the published ZIP') as response:
                    length = (response.headers.get('Content-Length') or '').strip()
                    if length and length != str(size):
                        raise CatalogError('size_mismatch', 'storage holds a different file size than the catalog says')
                    read = getattr(response, 'read1', response.read)  # whatever has arrived: steady progress
                    while True:
                        if cancel.is_set():
                            raise CatalogError('cancelled', 'the server is stopping')
                        try:
                            chunk = read(CHUNK_SIZE)
                        except Exception as exc:
                            raise CatalogError('download_failed', 'the download was interrupted (%s)'
                                               % describe(exc)) from None
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > size:
                            raise CatalogError('size_mismatch', 'storage sent more bytes than the catalog says')
                        out.write(chunk)
                        digest.update(chunk)
                        progress(done)
                        if self._wall() - beat >= HEARTBEAT_S:
                            beat = self._wall()
                            try:
                                os.utime(self._lock_path(sha))
                            except OSError:
                                pass
                if done != size:
                    raise CatalogError('truncated', 'the download stopped after %d of %d bytes' % (done, size))
                if digest.hexdigest() != sha:
                    raise CatalogError('sha_mismatch', 'the downloaded ZIP does not match the SHA-256 in the catalog')
                out.flush()
                os.fsync(out.fileno())
            try:
                os.chmod(tmp, 0o444)
            except OSError:
                pass
            try:
                os.replace(tmp, final)
            except OSError:
                if not self.present(sha):
                    raise
                return  # another process placed the same file first; it is verified before use
            signature = self._signature(final)
            if signature is not None:
                with self._lock:
                    self._verified[sha] = signature
        except OSError as exc:
            raise CatalogError('cache_unwritable', 'cannot write to the cache (%s)'
                               % (exc.strerror or type(exc).__name__)) from None
        finally:
            if os.path.lexists(tmp):
                self._remove(tmp)

    def _zips(self) -> List[Tuple[float, int, str, str]]:
        out = []
        try:
            with os.scandir(self.dir) as it:
                for entry in it:
                    match = ZIP_NAME_RE.fullmatch(entry.name)
                    if not match:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(info.st_mode):
                        out.append((info.st_mtime, info.st_size, entry.path, match.group(1)))
        except OSError:
            pass
        return out

    def make_room(self, need: int, keep: str, in_use: Set[str], reserved: int = 0) -> List[str]:
        """Evict least recently used ZIPs until need more bytes fit under the limit; returns the evicted shas.

        reserved counts downloads under way (any instance). A ZIP in use here, or used by any instance
        within PRUNE_MIN_AGE_S (its modification time), is never evicted: when that leaves too little
        room the download is refused with cache_full instead of going past the limit.
        """
        if need > self.max_bytes:
            raise CatalogError('too_large_for_cache', 'the ZIP (%s) is larger than the cache limit (%s)'
                               % (_size_text(need), _size_text(self.max_bytes)))
        now = self._wall()
        files = sorted(self._zips())
        total = sum(size for _, size, _, sha in files if sha != keep) + reserved
        evicted: List[str] = []
        for mtime, size, path, sha in files:
            if total + need <= self.max_bytes:
                break
            if sha == keep or sha in in_use or now - mtime < PRUNE_MIN_AGE_S:
                continue
            self._remove(path)
            self._forget(sha)
            total -= size
            evicted.append(sha)
            self._log('cache: evicted %s (least recently used)' % sha[:16])
        if total + need > self.max_bytes:
            self._log('cache: full; every other cached ZIP was used in the last %d minutes' % (PRUNE_MIN_AGE_S // 60))
            raise CatalogError('cache_full', 'the cache (%s) is full of exports in use; try again in a few minutes'
                               % _size_text(self.max_bytes))
        return evicted

    def active_downloads(self) -> Set[str]:
        """shas being downloaded right now, by any instance (fresh lock files)."""
        found = set()
        now = self._wall()
        try:
            with os.scandir(self.dir) as it:
                for entry in it:
                    match = LOCK_NAME_RE.fullmatch(entry.name)
                    try:
                        if match and now - entry.stat(follow_symlinks=False).st_mtime <= LOCK_STALE_S:
                            found.add(match.group(1))
                    except OSError:
                        continue
        except OSError:
            pass
        return found

    def prune(self, listed: Set[str], in_use: Set[str]) -> List[str]:
        """Delete ZIPs the catalog no longer lists, unless used recently or open here."""
        removed = []
        now = self._wall()
        for mtime, _, path, sha in self._zips():
            if sha in listed or sha in in_use or now - mtime < PRUNE_MIN_AGE_S:
                continue
            self._remove(path)
            self._forget(sha)
            removed.append(sha)
        return removed

    def cleanup(self) -> int:
        """Remove abandoned temporary downloads and stale locks (startup, and before downloads)."""
        removed = 0
        now = self._wall()
        try:
            with os.scandir(self.dir) as it:
                entries = [e for e in it if e.name.startswith('.dl-')]
        except OSError:
            return 0
        for entry in entries:
            limit = PART_STALE_S if entry.name.endswith('.part') else LOCK_STALE_S \
                if entry.name.endswith('.lock') else None
            try:
                if limit is not None and now - entry.stat(follow_symlinks=False).st_mtime > limit:
                    self._remove(entry.path)
                    removed += 1
            except OSError:
                continue
        return removed


# ------------------------------------------------------------ preparation

class _Prep:
    """One catalog entry being made ready in this process."""

    def __init__(self, entry: dict):
        self.entry = entry
        self.state = 'queued'  # queued, waiting, downloading, verifying, opening, ready, failed
        self.done = 0
        self.error: Optional[dict] = None
        self.export_ids: List[str] = []
        self.failed_roots: List[dict] = []  # export roots of the ZIP that could not be read (the others opened)
        self.finished = threading.Event()

    def stage(self, state: str, done: int = 0) -> None:
        self.state, self.done = state, done

    def progress(self, done: int) -> None:
        self.done = done


class Published:
    """Catalog entries, their state in this process, and the exports opened from them."""

    def __init__(self, catalog: Catalog, cache: ZipCache, registry, thumbs_dir: str,
                 log: Optional[Callable[[str], None]] = None, slots: int = 2):
        self.catalog = catalog
        self.store = catalog.store
        self.cache = cache
        self.registry = registry
        self.thumbs_dir = thumbs_dir
        self._log = log or (lambda message: None)
        self._lock = threading.Lock()
        self._preps: Dict[str, _Prep] = {}
        self._threads: Set[threading.Thread] = set()
        self._slots = threading.BoundedSemaphore(slots)
        self._stop = threading.Event()

    def close(self, timeout: float = CLOSE_WAIT_S) -> None:
        """Stop every preparation and wait for them (up to timeout), so that a download in progress
        removes its temporary file and its lock before the process exits."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _prep(self, catalog_id: str) -> Optional[_Prep]:
        with self._lock:
            return self._preps.get(catalog_id)

    def _in_flight(self) -> Set[str]:
        """shas this process is downloading, verifying or opening: never evicted or pruned."""
        with self._lock:
            return {p.entry['sha256'] for p in self._preps.values() if p.state in BUSY}

    def _forget(self, preps: List[_Prep]) -> None:
        """Drop finished preparations and close their exports (unpublished, or their ZIP was evicted)."""
        with self._lock:
            for prep in preps:
                if self._preps.get(prep.entry['id']) is prep:
                    del self._preps[prep.entry['id']]
        for prep in preps:
            self.registry.drop(prep.export_ids)

    # ------------------------------------------------------------ views

    def _item(self, entry: dict) -> dict:
        prep = self._prep(entry['id'])
        if prep is not None and prep.entry['sha256'] != entry['sha256']:
            prep = None
        sha, size = entry['sha256'], entry['size']
        state, done, error, exports, failed_roots = 'absent', 0, None, [], []
        if prep is not None:
            state, done = prep.state, prep.done
            if state == 'waiting':  # another process downloads it; done follows its file
                state = 'downloading'
            elif state == 'failed':
                error = dict(prep.error or {})
            elif state == 'ready':
                exports = [index.summary for index in map(self.registry.get, prep.export_ids) if index is not None]
                failed_roots = [dict(problem) for problem in prep.failed_roots]
        elif self.cache.present(sha):
            state = 'cached'
        elif self.cache.locked_elsewhere(sha):
            state, done = 'downloading', self.cache.foreign_progress(sha) or 0
        return {'id': entry['id'], 'name': entry['name'], 'label': entry['label'], 'size': size,
                'published_at': entry['published_at'], 'published_by': entry['published_by'],
                'summary': dict(entry['summary']), 'state': state,
                'progress': {'done': done, 'total': size} if state in ('downloading', 'verifying') else None,
                'error': error, 'exports': exports, 'failed_roots': failed_roots}

    def listing(self) -> Tuple[List[dict], dict]:
        entries = self.catalog.entries()
        return [self._item(entry) for entry in entries], self.catalog.status()

    def item(self, catalog_id: str) -> Optional[dict]:
        entry = self.catalog.entry(catalog_id)
        return self._item(entry) if entry else None

    # ------------------------------------------------------------ actions

    def open(self, catalog_id: str, retry: bool = False) -> Optional[dict]:
        """Start making an entry ready; returns its state at once while the work runs in the background.

        A failed preparation starts again only with retry (someone asked for it). Without it the
        failure is answered as it is, so that automatic calls (a UI polling several instances) can
        never restart a broken download over and over.
        """
        entry = self.catalog.entry(catalog_id)
        if entry is None:
            return None
        self._start(entry, retry=retry)
        return self._item(entry)

    def _start(self, entry: dict, retry: bool) -> _Prep:
        with self._lock:
            prep = self._preps.get(entry['id'])
            if prep is not None and prep.entry['sha256'] == entry['sha256'] and (prep.state != 'failed' or not retry):
                return prep
            prep = self._preps[entry['id']] = _Prep(entry)
            thread = threading.Thread(target=self._run, args=(prep,), name='mjv-prepare-%s' % entry['id'], daemon=True)
            self._threads.add(thread)
        thread.start()
        return prep

    def resolve(self, export_id: str, wait_s: float):
        """(index, None) when the export is open here; (None, item) while it is not ready; (None, None) if unknown.

        A ZIP already in the shared cache (another instance downloaded it) is verified and
        opened here, waiting up to wait_s. A request never waits for a download: while one
        runs, the item comes back at once (the UI shows its progress). Downloads start only
        through open().
        """
        match = EXPORT_ID_RE.fullmatch(export_id or '')
        entry = self.catalog.entry(match.group(1)) if match else None
        if entry is None:
            return None, None  # unknown, or no longer published
        index = self.registry.get(export_id)
        if index is None:
            prep = self._prep(entry['id'])
            present = self.cache.present(entry['sha256'])
            if prep is None and present:
                prep = self._start(entry, retry=False)
            if prep is not None and present and prep.state not in ('failed', 'waiting', 'downloading'):
                prep.finished.wait(wait_s)
            index = self.registry.get(export_id)
            if index is None:
                if prep is not None and prep.state == 'ready':
                    return None, None  # ready, but no export root has this id
                return None, self._item(entry)
        self.cache.touch(entry['sha256'], every_s=TOUCH_EVERY_S)
        return index, None

    def _run(self, prep: _Prep) -> None:
        entry = prep.entry
        try:
            with self._slots:
                path = self._local_copy(prep)
            prep.stage('opening')
            indexes = self.registry.open(path, catalog={'id': entry['id'], 'name': entry['name'],
                                                        'label': entry['label']}, failures=prep.failed_roots)
            prep.export_ids = [index.id for index in indexes]
            prep.stage('ready')
            self._log('opened %s: %d export root(s)%s' % (entry['id'], len(indexes), ''.join(
                '; root %s could not be read' % problem['root'] for problem in prep.failed_roots)))
        except CatalogError as exc:
            self._fail(prep, exc.code, exc.message)
        except SourceError as exc:
            path = self.cache.path(entry['sha256'])
            self._fail(prep, exc.code, exc.message.replace(path, entry['name'])
                       .replace(os.path.basename(path), entry['name']))
        except Exception as exc:  # never let one export take the server down
            self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
            self._fail(prep, 'internal', 'could not prepare this export (%s)' % type(exc).__name__)
        finally:
            prep.finished.set()
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _fail(self, prep: _Prep, code: str, message: str) -> None:
        prep.error = {'code': code, 'message': self.store.scrub(message)}
        prep.stage('failed')
        self._log('prepare %s failed: %s' % (prep.entry['id'], prep.error['message']))

    def _local_copy(self, prep: _Prep) -> str:
        """Path of a verified copy: present and checked, downloaded here, or waited for."""
        entry = prep.entry
        sha, size = entry['sha256'], entry['size']
        while True:
            if self._stop.is_set():
                raise CatalogError('cancelled', 'the server is stopping')
            if self.cache.present(sha):
                if not self.cache.is_verified(sha, size):
                    prep.stage('verifying')
                    if not self.cache.verify(sha, size, prep.progress, self._stop):
                        self._log('cache: %s did not match its SHA-256; downloading it again' % entry['id'])
                        self.cache.discard(sha)
                        continue
                self.cache.touch(sha)
                return self.cache.path(sha)
            if self.cache.try_lock(sha):
                try:
                    if self.cache.present(sha):
                        continue  # finished by another process between the check and the lock
                    self.cache.cleanup()
                    sizes = {e['sha256']: e['size'] for e in self.catalog.entries()}
                    reserved = sum(sizes.get(other, 0) for other in self.cache.active_downloads() if other != sha)
                    self._evicted(self.cache.make_room(size, keep=sha, in_use=self._in_flight(), reserved=reserved))
                    prep.stage('downloading')
                    self.cache.download(entry, self.store, prep.progress, self._stop)
                finally:
                    self.cache.unlock(sha)
                continue
            prep.stage('waiting', self.cache.foreign_progress(sha) or 0)
            self._stop.wait(POLL_S)

    def _evicted(self, shas: List[str]) -> None:
        if shas:
            with self._lock:
                gone = [p for p in self._preps.values() if p.entry['sha256'] in shas and p.state not in BUSY]
            self._forget(gone)

    # ------------------------------------------------------- maintenance

    def maintain(self) -> None:
        """Run every minute (HostedApp): remove abandoned downloads; then, on the latest good catalog, close
        the exports it no longer lists and delete their thumbnails, and their cached ZIPs once unused for
        PRUNE_MIN_AGE_S. No cached ZIP or thumbnail is deleted before a catalog has been read once."""
        removed = self.cache.cleanup()
        if removed:
            self._log('cache: removed %d abandoned download file(s)' % removed)
        entries = self.catalog.entries()
        if not self.catalog.loaded:
            return
        listed = {entry['id']: entry['sha256'] for entry in entries}
        with self._lock:
            gone = [p for cid, p in self._preps.items()
                    if listed.get(cid) != p.entry['sha256'] and p.state not in BUSY]
        self._forget(gone)
        for prep in gone:
            if prep.state == 'ready':
                self._log('closed %s, which is no longer published' % prep.entry['id'])
        for sha in self.cache.prune(set(listed.values()), self._in_flight()):
            self._log('cache: deleted %s, which is no longer published' % sha[:16])
        ids = set(listed)
        with self._lock:
            ids.update(self._preps)
        stale = []
        try:
            with os.scandir(self.thumbs_dir) as it:
                for entry in it:
                    match = EXPORT_ID_RE.fullmatch(entry.name)
                    if match and match.group(1) not in ids and entry.is_dir(follow_symlinks=False):
                        stale.append(entry.path)
        except OSError:
            stale = []
        for path in stale:
            shutil.rmtree(path, ignore_errors=True)
