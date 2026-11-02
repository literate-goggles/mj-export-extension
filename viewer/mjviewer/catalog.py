"""Hosted mode: the published catalog, the verified ZIP cache and deleting exports.

A deployment publishes exports to a private blob container that holds
catalog.json and exports/<sha256>.zip. The server reads both with a read-only
query string (for example an Azure container SAS with sp=rl). Optionally a
second query string for the same container may also write and delete (sp=rwdl
at most): then the owner of an export, or an administrator, can delete it. Both
query strings are treated like passwords: no log line, API response, error
message or exception ever contains them, and redirects are never followed.

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

Deleting rewrites catalog.json from its raw JSON, so fields this version does
not know survive, with If-Match on the ETag just read: the entry leaves the
exports list and a tombstone joins the top-level deleted list. Then the ZIP is
deleted from storage. Every instance closes a deleted export and deletes its
cached ZIP and thumbnails as soon as its catalog shows the tombstone, used
recently or not.
"""
from __future__ import annotations

import contextlib
import email.utils
import hashlib
import http.client
import json
import math
import os
import random
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
MAX_JSON_DEPTH = 64  # catalog.json nested deeper than this is refused: it could be read but not written back
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
WRITE_ATTEMPTS = 5       # conditional writes of catalog.json before giving up (another writer kept getting there first)
BLOB_RETRY_S = 60.0      # a deleted export's ZIP still in storage is deleted again at most this often...
TOMBSTONE_RETRY_S = 30 * 86400.0  # ...and only while its tombstone is this recent
BUSY = ('queued', 'waiting', 'downloading', 'verifying', 'opening')
LOOPBACK = ('localhost', '127.0.0.1')
USER_AGENT = 'mjviewer/%s' % __version__
SUMMARY_COUNTS = ('jobs', 'moodboards', 'profiles', 'images')
SUMMARY_TEXT = ('extension_version', 'status')
EMAIL_RE = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                      r"[a-z][a-z0-9-]{0,61}[a-z0-9]")
NOT_LISTED = 'this export is not published any more; it may have been deleted already'
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]')


class CatalogError(Exception):
    """A catalog, storage or cache problem. The message is safe to show and to log. status: the HTTP status
    storage answered, when it answered one."""

    def __init__(self, code: str, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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


def _http_problem(what: str, status: int, access: str = 'read-only') -> CatalogError:
    if status == 403:
        return CatalogError('forbidden', '%s: storage refused the request (HTTP 403); '
                                         'the %s access may have expired' % (what, access), status)
    if status == 404:
        return CatalogError('not_found', '%s: not found in storage (HTTP 404)' % what, status)
    if 300 <= status < 400:
        return CatalogError('redirected', '%s: storage answered with a redirect (HTTP %d), '
                                          'which is not followed' % (what, status), status)
    return CatalogError('storage_error', '%s: storage answered HTTP %d' % (what, status), status)


def _secret_parts(*queries: str) -> List[str]:
    """The query strings and their long values, in the forms they could take inside a message."""
    found = set()
    for query in filter(None, queries):
        found.add(query)
        for _, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
            if len(value) >= 16:
                found.update({value, urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value)})
    return sorted((s for s in found if len(s) >= 8), key=len, reverse=True)


class BlobStore:
    """Objects of one private container, read with a fixed read-only query string and, when write_url gives
    a second one for the same container, written and deleted with that one."""

    def __init__(self, url: str, timeout: float = 30.0, write_url: Optional[str] = None):
        parts = urllib.parse.urlsplit(url)
        self._base = '%s://%s%s' % (parts.scheme, parts.netloc, parts.path.rstrip('/'))
        self._query = parts.query
        self._write_query = urllib.parse.urlsplit(write_url).query if write_url else None
        self._secrets = _secret_parts(parts.query, self._write_query or '')
        self._opener = opener_for(url)
        self.timeout = timeout

    def __repr__(self) -> str:
        return '<BlobStore>'

    @property
    def writable(self) -> bool:
        return self._write_query is not None

    def url(self, name: str, write: bool = False) -> str:
        query = self._write_query if write else self._query
        return '%s/%s?%s' % (self._base, urllib.parse.quote(name, safe='/'), query)

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

    def _write(self, method: str, name: str, what: str, headers: Dict[str, str], data: Optional[bytes] = None):
        """The answer's headers to a PUT or DELETE sent with the write query string; HTTPError as raised."""
        if self._write_query is None:
            raise CatalogError('read_only', '%s: this server has no write access to the storage' % what)
        sent = {'User-Agent': USER_AGENT}
        sent.update(headers)
        request = urllib.request.Request(self.url(name, write=True), data=data, headers=sent, method=method)
        with self._opener.open(request, timeout=self.timeout) as response:
            response.read(65536)
            return response.headers

    def put(self, name: str, data: bytes, what: str, if_match: Optional[str] = None,
            if_none_match: Optional[str] = None) -> Optional[str]:
        """Replace a whole JSON object (a block blob) on a condition; returns its new ETag. CatalogError
        'changed' when the condition failed (HTTP 412, or 409): another writer got there first."""
        headers = {'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'application/json'}
        if if_match:
            headers['If-Match'] = if_match
        if if_none_match:
            headers['If-None-Match'] = if_none_match
        try:
            return self._write('PUT', name, what, headers, data).get('ETag')
        except CatalogError:
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (409, 412):
                raise CatalogError('changed', '%s changed while it was being written' % what) from None
            raise _http_problem(what, exc.code, 'write') from None
        except Exception as exc:
            raise CatalogError('unreachable', '%s: storage unreachable (%s)' % (what, describe(exc))) from None

    def delete(self, name: str, what: str, unmodified_since: Optional[float] = None) -> bool:
        """Delete an object and its snapshots; False when it was already gone. With unmodified_since (epoch
        seconds) only when it was not written after that: CatalogError 'changed' otherwise."""
        headers = {'x-ms-delete-snapshots': 'include'}
        if unmodified_since is not None:
            headers['If-Unmodified-Since'] = email.utils.formatdate(unmodified_since, usegmt=True)
        try:
            self._write('DELETE', name, what, headers)
        except CatalogError:
            raise
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return False
            if exc.code == 412:
                raise CatalogError('changed', '%s was written again since' % what) from None
            raise _http_problem(what, exc.code, 'write') from None
        except Exception as exc:
            raise CatalogError('unreachable', '%s: storage unreachable (%s)' % (what, describe(exc))) from None
        return True


# ---------------------------------------------------------------- catalog

def _plain(value, limit: int) -> Optional[str]:
    """value when it is a non-empty string of at most limit characters without control characters."""
    if isinstance(value, str) and 0 < len(value) <= limit and not _CONTROL.search(value):
        return value
    return None


def _count(value) -> bool:
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def email_address(value) -> Optional[str]:
    """value in lower case when it is a plausible email address (ASCII, one @, a dotted domain), else None."""
    if not isinstance(value, str) or len(value) > 254 or not value.isascii():
        return None
    value = value.lower()
    return value if EMAIL_RE.fullmatch(value) else None


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
    owner = item.get('owner')
    if owner == '':
        owner = None
    if owner is not None:
        owner = email_address(owner)
        if owner is None:
            return None, 'owner is not an email address'
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
            'label': label, 'published_at': published_at, 'published_by': published_by, 'owner': owner,
            'summary': summary}, ''


def _tombstone(item) -> Optional[dict]:
    """What the server uses of one entry of the top-level deleted list ({id, sha256, deleted_ts}), or None.
    It needs a valid sha256, the id that goes with it and a deleted_at date; the rest is kept as written."""
    if not isinstance(item, dict):
        return None
    sha = item.get('sha256')
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or item.get('id') != sha[:16]:
        return None
    deleted_at = item.get('deleted_at')
    moment = parse_date(deleted_at) if _plain(deleted_at, 64) else None
    if moment is None:
        return None
    return {'id': sha[:16], 'sha256': sha, 'deleted_ts': moment.timestamp()}


def _reject_constant(name: str):
    raise ValueError('%s is not valid JSON' % name)


class _TooLarge(ValueError):
    """A number that JSON can hold but that does not fit a float (1e400): it could not be written back."""


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _TooLarge(text)
    return value


def _nested_within(value, limit: int) -> bool:
    """Whether no object or list in value is nested more than limit levels deep (the outermost is level 1)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return False
        children = item.values() if isinstance(item, dict) else item
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return True


def _load(raw: bytes) -> dict:
    """catalog.json as its whole JSON object (every field kept), checked to be a version 1 catalog. It must also
    be one that dump_catalog can write back: no number too large for a float, no nesting past MAX_JSON_DEPTH."""
    try:
        data = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant, parse_float=_finite)
    except _TooLarge:
        raise CatalogError('bad_catalog', 'catalog.json holds a number too large to be written back') from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CatalogError('bad_catalog', 'catalog.json is not valid JSON') from None
    if isinstance(data, (dict, list)) and not _nested_within(data, MAX_JSON_DEPTH):
        raise CatalogError('bad_catalog', 'catalog.json is nested more than %d levels deep' % MAX_JSON_DEPTH)
    version = data.get('version') if isinstance(data, dict) else None
    if type(version) is not int or version != 1:
        raise CatalogError('bad_catalog', 'catalog.json is not a version 1 catalog')
    if not isinstance(data.get('exports'), list):
        raise CatalogError('bad_catalog', 'catalog.json has no exports list')
    return data


def dump_catalog(data: dict) -> bytes:
    """catalog.json as indented UTF-8 JSON with a final newline. ValueError for a number JSON cannot hold."""
    try:
        return (json.dumps(data, indent=1, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    except UnicodeEncodeError:  # a lone surrogate somewhere in the catalog: escape everything
        return (json.dumps(data, indent=1, ensure_ascii=True, allow_nan=False) + '\n').encode('ascii')


def _encode(data: dict) -> bytes:
    """data as the catalog.json to write, read back first as every instance will read it (this one included).
    CatalogError storage_error when it cannot be written or read back: then nothing is written."""
    try:
        body = dump_catalog(data)
        parse_catalog(body)
    except Exception as exc:  # whatever the reason, a catalog no instance could read is never written
        reason = exc.message if isinstance(exc, CatalogError) else type(exc).__name__
        raise CatalogError('storage_error', 'catalog.json could not be written back (%s); nothing was deleted'
                           % reason) from None
    return body


def parse_catalog(raw: bytes, deleted: Optional[List[dict]] = None) -> Tuple[List[dict], List[dict], Optional[str]]:
    """(entries, skipped, updated_at). Bad entries are skipped with a reason, never used. deleted, when given,
    receives the usable tombstones of the top-level deleted list (the newest MAX_ENTRIES)."""
    data = _load(raw)
    entries, skipped = _entries(data['exports'])
    stones = data.get('deleted')
    if deleted is not None and isinstance(stones, list):
        for item in stones[-MAX_ENTRIES:]:
            try:
                stone = _tombstone(item)
            except Exception:  # like a bad entry, a bad tombstone only skips itself
                stone = None
            if stone is not None:
                deleted.append(stone)
    updated = data.get('updated_at')
    return entries, skipped, iso_utc(parse_date(updated)) if _plain(updated, 64) else None


def _entries(items: list) -> Tuple[List[dict], List[dict]]:
    """The usable entries of an exports list, and the skipped ones as {index, reason}."""
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
    return entries, skipped


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
        self._deleted: List[dict] = []  # tombstones: {id, sha256, deleted_ts}
        self._updated_at: Optional[str] = None
        self._etag: Optional[str] = None
        self._fetched: Optional[float] = None  # clock time of the last good answer (200 or 304)
        self._checked: Optional[float] = None  # clock time of the last attempt
        self._ok = False
        self._error: Optional[str] = None
        self._inflight: Optional[threading.Event] = None
        self._version = 0  # counts catalogs this instance wrote itself (adopt)
        self.on_change: Optional[Callable[[], None]] = None  # called after a changed catalog was read

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
            etag, version = self._etag, self._version
        try:
            status, tag, body = self.store.get(CATALOG_NAME, 'catalog.json', etag=etag, timeout=self.timeout)
            if status == 304:
                with self._lock:
                    self._fetched = self._checked = self._clock()
                    self._ok, self._error = True, None
                return
            if status != 200:
                raise CatalogError('storage_error', 'catalog.json: storage answered HTTP %d' % status)
            deleted: List[dict] = []
            entries, skipped, updated = parse_catalog(body, deleted)
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
            if self._version != version:
                return  # this instance wrote a newer catalog while this answer was on its way: keep that one
            self._install(entries, skipped, updated, deleted, tag)
        self._log('catalog: %d export(s) listed%s' % (len(entries), ''.join(
            '; entry %d skipped (%s)' % (s['index'], s['reason']) for s in skipped[:20])))
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # cleaning up after deleted exports must never break reading the catalog
                self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())

    def _install(self, entries: List[dict], skipped: List[dict], updated: Optional[str], deleted: List[dict],
                 etag: Optional[str]) -> None:
        """Under the lock: serve this catalog from now on."""
        self._entries, self._skipped, self._updated_at, self._deleted = entries, skipped, updated, deleted
        self._by_id = {entry['id']: entry for entry in entries}
        self._etag = etag
        self._fetched = self._checked = self._clock()
        self._ok, self._error = True, None

    def adopt(self, raw: bytes, etag: Optional[str]) -> None:
        """Serve a catalog this instance has just written (with the ETag storage gave it) at once, instead of
        after the next refresh; a refresh already on its way with the older one is dropped."""
        deleted: List[dict] = []
        entries, skipped, updated = parse_catalog(raw, deleted)
        with self._lock:
            self._version += 1
            self._install(entries, skipped, updated, deleted, etag)

    def expire(self) -> None:
        """Read catalog.json again at the next use, whatever MJV_CATALOG_TTL_S says."""
        with self._lock:
            self._checked = None

    def snapshot(self) -> Tuple[List[dict], List[dict]]:
        """The entries and tombstones in use, without refreshing."""
        with self._lock:
            return list(self._entries), list(self._deleted)

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

    def remove(self, shas: Set[str]) -> List[str]:
        """Delete these ZIPs at once, however recently used (their exports were deleted); returns those removed."""
        removed = []
        for _, _, path, sha in self._zips():
            if sha in shas:
                self._remove(path)
                self._forget(sha)
                removed.append(sha)
        return removed

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
        self.cancel = threading.Event()  # the server stops, or the export was deleted
        self.deleted = False

    def stage(self, state: str, done: int = 0) -> None:
        self.state, self.done = state, done

    def progress(self, done: int) -> None:
        self.done = done


class Published:
    """Catalog entries, their state in this process, and the exports opened from them."""

    def __init__(self, catalog: Catalog, cache: ZipCache, registry, thumbs_dir: str,
                 log: Optional[Callable[[str], None]] = None, slots: int = 2,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.catalog = catalog
        self.store = catalog.store
        self.cache = cache
        self.registry = registry
        self.thumbs_dir = thumbs_dir
        self._log = log or (lambda message: None)
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._preps: Dict[str, _Prep] = {}
        self._threads: Set[threading.Thread] = set()
        self._slots = threading.BoundedSemaphore(slots)
        self._stop = threading.Event()
        self._blob_tries: Dict[str, float] = {}  # sha -> clock time of the last DELETE of a deleted export's ZIP
        self._blob_errors: Dict[str, str] = {}   # sha -> the failure last logged for it
        self._blobs_done: Set[str] = set()       # ZIPs storage no longer holds (or that were uploaded again)
        catalog.on_change = self.purge_deleted

    def close(self, timeout: float = CLOSE_WAIT_S) -> None:
        """Stop every preparation and wait for them (up to timeout), so that a download in progress
        removes its temporary file and its lock before the process exits."""
        self._stop.set()
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
            for prep in self._preps.values():
                prep.cancel.set()
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
                'owner': entry['owner'], 'summary': dict(entry['summary']), 'state': state,
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
            if self._stop.is_set():
                prep.cancel.set()
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
                orphan = prep.deleted and self._preps.get(prep.entry['id']) is prep
            if orphan:  # deleted while it ran: what it opened or wrote goes now
                self._purge({prep.entry['id']: prep.entry['sha256']}, quiet=True)

    def _fail(self, prep: _Prep, code: str, message: str) -> None:
        if prep.deleted:
            prep.error = {'code': 'deleted', 'message': 'this export was deleted'}
            prep.stage('failed')
            return
        prep.error = {'code': code, 'message': self.store.scrub(message)}
        prep.stage('failed')
        self._log('prepare %s failed: %s' % (prep.entry['id'], prep.error['message']))

    def _local_copy(self, prep: _Prep) -> str:
        """Path of a verified copy: present and checked, downloaded here, or waited for."""
        entry = prep.entry
        sha, size = entry['sha256'], entry['size']
        while True:
            if prep.cancel.is_set():
                raise CatalogError('cancelled', 'the server is stopping')
            if self.cache.present(sha):
                if not self.cache.is_verified(sha, size):
                    prep.stage('verifying')
                    if not self.cache.verify(sha, size, prep.progress, prep.cancel):
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
                    self.cache.download(entry, self.store, prep.progress, prep.cancel)
                finally:
                    self.cache.unlock(sha)
                continue
            prep.stage('waiting', self.cache.foreign_progress(sha) or 0)
            prep.cancel.wait(POLL_S)

    def _evicted(self, shas: List[str]) -> None:
        if shas:
            with self._lock:
                gone = [p for p in self._preps.values() if p.entry['sha256'] in shas and p.state not in BUSY]
            self._forget(gone)

    # ---------------------------------------------------------- deleting

    def delete(self, catalog_id: str, user: str, admin: bool = False) -> dict:
        """Delete a published export for user, its owner or an administrator; returns its entry.

        catalog.json is read fresh with its ETag, the request is checked against that copy, and the file is
        written back from its raw JSON without the entry and with a tombstone, on that ETag only. When
        another writer got there first, all of it starts over, WRITE_ATTEMPTS times at most. Then the ZIP
        is deleted from storage (already gone is fine; a failure is retried by maintain()), and this instance
        closes the export and deletes its cached ZIP and thumbnails at once. A write that got no answer, or a
        5xx, may have been stored all the same: catalog.json is read again to tell. CatalogError codes:
        delete_disabled, unknown_catalog_entry, not_owner, catalog_busy, storage_error, delete_unconfirmed."""
        if not self.store.writable:
            raise CatalogError('delete_disabled', 'deleting exports is not turned on for this viewer')
        match = EXPORT_ID_RE.fullmatch(catalog_id or '')
        if not match or match.group(2):
            raise CatalogError('unknown_catalog_entry', NOT_LISTED)
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            data, etag = self._fresh_catalog()
            entry = next((e for e in _entries(data['exports'])[0] if e['id'] == catalog_id), None)
            if entry is None:
                raise CatalogError('unknown_catalog_entry', NOT_LISTED)
            owner, sha = entry['owner'], entry['sha256']
            if not admin and (owner is None or owner != user):
                raise CatalogError('not_owner', 'only the owner of this export or an administrator can delete it'
                                   if owner else 'this export has no owner recorded, so only an administrator '
                                                 'can delete it')
            stones = data.get('deleted')
            if stones is None:
                stones = []
            if not isinstance(stones, list):
                raise CatalogError('storage_error', 'catalog.json has a deleted field that is not a list; '
                                                    'nothing was deleted')
            now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._wall()))
            stone = {'id': catalog_id, 'sha256': sha, 'size': entry['size'], 'owner': owner, 'deleted_at': now,
                     'deleted_by': user}
            data['exports'] = [item for item in data['exports'] if not (
                isinstance(item, dict) and (item.get('id') == catalog_id or item.get('sha256') == sha))]
            data['deleted'] = stones + [stone]
            data['updated_at'] = now
            body = _encode(data)
            try:
                tag = self.store.put(CATALOG_NAME, body, 'catalog.json', if_match=etag)
            except CatalogError as exc:
                if exc.code == 'changed':
                    if attempt < WRITE_ATTEMPTS:
                        time.sleep(random.uniform(0.05, 0.25) * attempt)
                    continue
                if exc.code != 'unreachable' and (exc.status or 0) < 500:  # refused: certainly not stored
                    raise CatalogError('storage_error', '%s; nothing was deleted' % exc.message) from None
                body, tag = self._stored_anyway(stone, exc)
            self._finish(catalog_id, sha, user, body, tag)
            return entry
        raise CatalogError('catalog_busy', 'the list of exports kept changing while it was being updated; '
                                           'nothing was deleted, try again')

    def _stored_anyway(self, stone: dict, problem: CatalogError) -> Tuple[bytes, Optional[str]]:
        """After a write of catalog.json that got no answer or a 5xx: catalog.json as storage holds it now, when
        it has this request's tombstone (the same id, deleted_at and deleted_by), so the write was stored.
        Otherwise CatalogError delete_unconfirmed: from here, whether the export was deleted cannot be told."""
        self._log('catalog: no clear answer to the write of catalog.json (%s); reading it again'
                  % self.store.scrub(problem.message))
        try:
            status, etag, raw = self.store.get(CATALOG_NAME, 'catalog.json', timeout=self.catalog.timeout)
            stones = _load(raw).get('deleted') if status == 200 else None
            unknown = 'catalog.json read again does not show the deletion'
        except CatalogError as exc:
            stones, unknown = None, 'reading catalog.json again failed: %s' % exc.message
        if isinstance(stones, list) and any(isinstance(item, dict) and all(
                item.get(key) == stone[key] for key in ('id', 'deleted_at', 'deleted_by')) for item in stones):
            self._log('catalog: the write of catalog.json was stored')
            return raw, etag
        raise CatalogError('delete_unconfirmed', '%s; could not confirm whether the export was deleted (%s): '
                                                 'reload the page to check' % (problem.message, unknown))

    def _finish(self, catalog_id: str, sha: str, user: str, body: bytes, tag: Optional[str]) -> None:
        """What follows a catalog.json that no longer lists the export: serve it here at once, delete the ZIP from
        storage, close the export here. The export is deleted by then, so a step that fails is logged, never
        answered, and the steps after it still run."""
        self._log('deleted %s by %s' % (catalog_id, user))
        steps = (('serving the new catalog.json', lambda: self.catalog.adopt(body, tag)),
                 ('deleting its ZIP from storage', lambda: self._delete_blob(catalog_id, sha)),
                 ('closing it on this instance', lambda: self._purge({catalog_id: sha}, wait_s=CLOSE_WAIT_S,
                                                                     quiet=True)))
        for n, (what, step) in enumerate(steps):
            try:
                step()
            except Exception as exc:
                self._log('catalog: after deleting %s, %s failed (%s)' % (catalog_id, what, type(exc).__name__))
                self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
                if n == 0:
                    self.catalog.expire()  # the copy in use still lists it: read the new one at the next request

    def _fresh_catalog(self) -> Tuple[dict, str]:
        """catalog.json as storage holds it right now (never the copy in use), with its ETag."""
        try:
            status, etag, raw = self.store.get(CATALOG_NAME, 'catalog.json', timeout=self.catalog.timeout)
            if status != 200 or not etag:
                raise CatalogError('storage_error', 'catalog.json: storage answered HTTP %d without an ETag' % status)
            return _load(raw), etag
        except CatalogError as exc:
            if exc.code == 'not_found':
                raise CatalogError('unknown_catalog_entry', NOT_LISTED) from None
            raise CatalogError('storage_error', '%s; nothing was deleted' % exc.message) from None

    def _delete_blob(self, catalog_id: str, sha: str, since: Optional[float] = None) -> None:
        """DELETE the ZIP of a deleted export, unless (with since) it was uploaded again after that time. A
        failure is logged once per message; maintain() tries again every BLOB_RETRY_S."""
        with self._lock:
            self._blob_tries[sha] = self._clock()
        try:
            existed = self.store.delete('exports/%s.zip' % sha, 'the published ZIP', unmodified_since=since)
        except CatalogError as exc:
            if exc.code != 'changed':
                message = self.store.scrub(exc.message)
                with self._lock:
                    repeated = self._blob_errors.get(sha) == message
                    self._blob_errors[sha] = message
                if not repeated:
                    self._log('catalog: the ZIP of %s is still in storage (%s); trying again every minute'
                              % (catalog_id, message))
                return
            existed = False
            self._log('catalog: kept the ZIP of %s, which was uploaded again after it was deleted' % catalog_id)
        with self._lock:
            self._blobs_done.add(sha)
            self._blob_errors.pop(sha, None)
        if existed and since is not None:
            self._log('catalog: deleted the ZIP of %s from storage' % catalog_id)

    def _purge(self, gone: Dict[str, str], wait_s: float = 0.0, quiet: bool = False) -> None:
        """Close the exports of deleted entries ({catalog id: sha256}) and delete their cached ZIPs and
        thumbnails at once, however recently used. A preparation still running is stopped (and waited for up
        to wait_s); one that has not ended by then does this itself when it ends."""
        if not gone:
            return
        with self._lock:
            preps = [p for p in self._preps.values() if p.entry['id'] in gone]
            for prep in preps:
                prep.deleted = True
                prep.cancel.set()
        deadline = time.monotonic() + wait_s
        for prep in preps:
            prep.finished.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            idle = [p for p in preps if p.state not in BUSY]
        self._forget(idle)
        touched = {p.entry['id'] for p in idle if p.state == 'ready'}
        touched.update(sha[:16] for sha in self.cache.remove(set(gone.values())))
        folders = []
        try:
            with os.scandir(self.thumbs_dir) as it:
                for entry in it:
                    match = EXPORT_ID_RE.fullmatch(entry.name)
                    if match and match.group(1) in gone and entry.is_dir(follow_symlinks=False):
                        folders.append((entry.path, match.group(1)))
        except OSError:
            folders = []
        for path, catalog_id in folders:
            shutil.rmtree(path, ignore_errors=True)
            touched.add(catalog_id)
        if not quiet:
            for catalog_id in sorted(touched):
                self._log('removed %s from this instance: it was deleted' % catalog_id)

    def purge_deleted(self) -> None:
        """When a changed catalog arrives, and every minute: the exports its tombstones name, unless it lists
        them again, are closed here and their cached ZIPs and thumbnails deleted at once."""
        entries, stones = self.catalog.snapshot()
        listed = {entry['id'] for entry in entries}
        self._purge({stone['id']: stone['sha256'] for stone in stones if stone['id'] not in listed})

    def _retry_blob_deletes(self) -> None:
        """Delete the ZIPs of exports deleted in the last TOMBSTONE_RETRY_S that storage may still hold (an
        earlier DELETE failed, or another instance wrote the tombstone), each at most every BLOB_RETRY_S and
        once confirmed gone never again; never the ZIP of an export that is listed again, nor one uploaded
        again after its latest tombstone."""
        if not self.store.writable:
            return
        entries, stones = self.catalog.snapshot()
        listed = {entry['sha256'] for entry in entries}
        latest: Dict[str, dict] = {}
        for stone in stones:
            known = latest.get(stone['sha256'])
            if known is None or stone['deleted_ts'] > known['deleted_ts']:
                latest[stone['sha256']] = stone
        now, wall = self._clock(), self._wall()
        with self._lock:
            due = [stone for sha, stone in latest.items()
                   if sha not in listed and sha not in self._blobs_done
                   and wall - stone['deleted_ts'] <= TOMBSTONE_RETRY_S
                   and (sha not in self._blob_tries or now - self._blob_tries[sha] >= BLOB_RETRY_S)]
        for stone in due:
            self._delete_blob(stone['id'], stone['sha256'], since=stone['deleted_ts'])

    # ------------------------------------------------------- maintenance

    def maintain(self) -> None:
        """Run every minute (HostedApp): remove abandoned downloads; then, on the latest good catalog, close
        the exports it no longer lists and delete their thumbnails, and their cached ZIPs once unused for
        PRUNE_MIN_AGE_S (deleted exports at once), and delete again the ZIPs of deleted exports that storage
        may still hold. No cached ZIP or thumbnail is deleted before a catalog has been read once."""
        removed = self.cache.cleanup()
        if removed:
            self._log('cache: removed %d abandoned download file(s)' % removed)
        entries = self.catalog.entries()
        if not self.catalog.loaded:
            return
        self.purge_deleted()
        self._retry_blob_deletes()
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
