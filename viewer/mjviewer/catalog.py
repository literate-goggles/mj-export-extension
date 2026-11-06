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
recently or not. An instance that cannot read catalog.json keeps its last good
copy, which may still list a deleted export, for a while only: after
max(UNREAD_LIMIT_S, UNREAD_TTLS x MJV_CATALOG_TTL_S) without a good read it
closes its exports and serves none until a read succeeds.
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
from collections import namedtuple
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import __version__
from .model import MAX_COUNT, hidden_closure, iso_utc, parse_date
from .sources import CHUNK_SIZE, SourceError

CATALOG_NAME = 'catalog.json'
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_HIDDEN_ITEMS = 1000  # jobs and moodboards one entry's hidden list may name, together
MAX_ANYONE_HIDDEN = MAX_HIDDEN_ITEMS - 100  # where hiding by anyone stops: the rest is kept for the export's managers
MAX_HIDDEN_ID = 200
MAX_WRITE_BYTES = 12 * 1024 * 1024  # this server never grows catalog.json past this (it reads up to 16 MiB)
HIDDEN_LISTS = ('jobs', 'moodboards')
HIDDEN_COUNTS = ('jobs', 'moodboards', 'images')
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
UNREAD_LIMIT_S = 300.0   # no good read of catalog.json for this long, or for UNREAD_TTLS times MJV_CATALOG_TTL_S
UNREAD_TTLS = 5          # when that is longer: no export is served until a read succeeds
BUSY = ('queued', 'waiting', 'downloading', 'verifying', 'opening')
LOOPBACK = ('localhost', '127.0.0.1')
USER_AGENT = 'mjviewer/%s' % __version__
SUMMARY_COUNTS = ('jobs', 'moodboards', 'profiles', 'images')
SUMMARY_TEXT = ('extension_version', 'status')
EMAIL_RE = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                      r"[a-z][a-z0-9-]{0,61}[a-z0-9]")
NOT_LISTED = 'this export is not published any more; it may have been deleted already'
CATALOG_UNAVAILABLE = "The viewer can't reach its list of exports right now. Try again in a few minutes."
UNCHANGED = object()  # what a change to catalog.json returns when there is nothing to change: nothing is written
TOO_MANY_HIDDEN = ('this export already hides %d generations and moodboards, the most it can; show some again first'
                   % MAX_HIDDEN_ITEMS)
HIDE_LIMIT = 'no more moodboards can be hidden in this export, except by its owner or an administrator'
UNREADABLE_CHANGE = 'this change would make the export unreadable for the viewer (%s); nothing was changed'
# How each change written to catalog.json words its errors (Published._rewrite); a delete's are the ones it always had.
ACTION_WORDS = {
    'delete': {'nothing': 'nothing was deleted', 'unconfirmed': 'delete_unconfirmed',
               'unclear': '%s; could not confirm whether the export was deleted (%s): reload the page to check',
               'not_shown': 'catalog.json read again does not show the deletion',
               'not_owner': 'only the owner of this export or an administrator can delete it',
               'no_owner': 'this export has no owner recorded, so only an administrator can delete it'},
    'hide': {'nothing': 'nothing was changed', 'unconfirmed': 'hide_unconfirmed',
             'unclear': '%s; could not confirm whether it changed (%s): reload the page to check',
             'not_shown': 'catalog.json read again does not show the change',
             'not_owner': 'only the owner of this export or an administrator can hide or show its generations and '
                          'moodboards',
             'no_owner': 'this export has no owner recorded, so only an administrator can hide or show its '
                         'generations and moodboards'},
}
STORAGE_CODE_RE = re.compile(r'[A-Za-z]{1,64}')  # storage's own name for an error (x-ms-error-code)
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]')


class CatalogError(Exception):
    """A catalog, storage or cache problem. The message is safe to show and to log. status: the HTTP status
    storage answered, when it answered one."""

    def __init__(self, code: str, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class CatalogUnavailable(CatalogError):
    """catalog.json has not been read successfully for too long (Catalog.outdated): no export is served."""

    def __init__(self):
        super().__init__('catalog_unavailable', CATALOG_UNAVAILABLE)


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


def _storage_code(exc: urllib.error.HTTPError) -> Optional[str]:
    """Storage's own name for the error of a refused request (the x-ms-error-code header), when it is one word."""
    value = exc.headers.get('x-ms-error-code') if exc.headers is not None else None
    return value if isinstance(value, str) and STORAGE_CODE_RE.fullmatch(value) else None


def _http_problem(what: str, status: int, access: str = 'read-only', reason: Optional[str] = None) -> CatalogError:
    if status == 403:
        return CatalogError('forbidden', '%s: storage refused the request (HTTP 403); '
                                         'the %s access may have expired' % (what, access), status)
    if status == 404:
        return CatalogError('not_found', '%s: not found in storage (HTTP 404)' % what, status)
    if 300 <= status < 400:
        return CatalogError('redirected', '%s: storage answered with a redirect (HTTP %d), '
                                          'which is not followed' % (what, status), status)
    return CatalogError('storage_error', '%s: storage answered HTTP %d%s' % (what, status, ' (%s)' % reason
                                                                           if reason else ''), status)


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
        seconds) only when it was not written after that: CatalogError 'changed' otherwise. A 412 means that
        only when the condition was sent and storage does not name another reason (a lease, say)."""
        headers = {'x-ms-delete-snapshots': 'include'}
        if unmodified_since is not None:
            headers['If-Unmodified-Since'] = email.utils.formatdate(unmodified_since, usegmt=True)
        try:
            self._write('DELETE', name, what, headers)
        except CatalogError:
            raise
        except urllib.error.HTTPError as exc:
            reason = _storage_code(exc)
            exc.close()
            if exc.code == 404:
                return False
            if exc.code == 412 and unmodified_since is not None and reason in (None, 'ConditionNotMet'):
                raise CatalogError('changed', '%s was written again since' % what, 412) from None
            raise _http_problem(what, exc.code, 'write', reason) from None
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


def hidden_id(value) -> Optional[str]:
    """value when it can name a hidden job or moodboard: 1-200 characters, no control characters, not only
    whitespace (the viewer reads an integer id as its decimal string, so a record holds strings only)."""
    value = _plain(value, MAX_HIDDEN_ID)
    return value if value is not None and value.strip() else None


def _stamp(value) -> Optional[str]:
    """A date as YYYY-MM-DDTHH:MM:SSZ (UTC), or None when it is not one."""
    moment = parse_date(value) if _plain(value, 64) else None
    return None if moment is None else moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _hidden_counts(value) -> Optional[dict]:
    """{jobs, moodboards, images} when all three are whole numbers from 0 to 2^53, else None (not counted)."""
    if not isinstance(value, dict):
        return None
    counts = {key: value.get(key) for key in HIDDEN_COUNTS}
    if all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= MAX_COUNT for n in counts.values()):
        return counts
    return None


def _hidden_record(raw) -> Tuple[Optional[dict], str]:
    """The hidden list of one catalog entry, normalized, or (None, '') when it hides nothing; (None, reason) when
    which ids it hides cannot be read with certainty (the entry is then not valid). Reasons never quote a value.
    What only describes an item is tolerated: a hidden_at that is not a date or a hidden_by that is not an
    address becomes None, and counts that are not three counts become None (the next write fixes them); an id
    listed twice counts once (its first who and when). key changes exactly when the listed ids change."""
    if raw is None:
        return None, ''
    if not isinstance(raw, dict):
        return None, 'hidden is not an object'
    lists = {}
    for name in HIDDEN_LISTS:
        items = raw.get(name)
        if items is not None and not isinstance(items, list):
            return None, 'hidden.%s is not a list' % name
        lists[name] = items or []
    if sum(len(items) for items in lists.values()) > MAX_HIDDEN_ITEMS:
        return None, 'hidden lists more than %d items' % MAX_HIDDEN_ITEMS
    found: Dict[str, dict] = {}
    for name, items in lists.items():
        found[name] = {}
        for n, item in enumerate(items):
            item_id = hidden_id(item.get('id')) if isinstance(item, dict) else None
            if item_id is None:
                return None, 'hidden.%s[%d] has no usable id' % (name, n)
            if item_id not in found[name]:
                found[name][item_id] = {'hidden_at': _stamp(item.get('hidden_at')),
                                        'hidden_by': email_address(item.get('hidden_by'))}
    if not found['jobs'] and not found['moodboards']:
        return None, ''
    key = hashlib.sha256(json.dumps([sorted(found['jobs']), sorted(found['moodboards'])]).encode('ascii')).hexdigest()
    return {'jobs': found['jobs'], 'moodboards': found['moodboards'], 'counts': _hidden_counts(raw.get('counts')),
            'key': key}, ''


def record_key(entry: Optional[dict]) -> str:
    """Which hidden list an entry applies: '' when it hides nothing (or there is no entry)."""
    record = entry.get('hidden') if entry else None
    return record['key'] if record else ''


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
    hidden, reason = _hidden_record(item.get('hidden'))
    if reason:  # never served unfiltered: an entry whose hidden ids cannot be read is not valid at all
        return None, reason
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
            'summary': summary, 'hidden': hidden}, ''


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


def _encode(data: dict, grow_from: Optional[int] = None, nothing: str = 'nothing was deleted') -> bytes:
    """data as the catalog.json to write, read back first as every instance will read it (this one included).
    CatalogError storage_error when it cannot be written or read back, or (with grow_from, the size read) when it
    would grow past MAX_WRITE_BYTES: then nothing is written. nothing ends the message."""
    try:
        body = dump_catalog(data)
        too_large = grow_from is not None and len(body) > max(grow_from, MAX_WRITE_BYTES)
        if not too_large:
            parse_catalog(body)
    except Exception as exc:  # whatever the reason, a catalog no instance could read is never written
        reason = exc.message if isinstance(exc, CatalogError) else type(exc).__name__
        raise CatalogError('storage_error', 'catalog.json could not be written back (%s); %s'
                           % (reason, nothing)) from None
    if too_large:
        raise CatalogError('storage_error', 'catalog.json would be %.1f MiB, more than this viewer writes (%d MiB); %s'
                           % (len(body) / 1024 ** 2, MAX_WRITE_BYTES // 1024 ** 2, nothing))
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


def _reserved_id(item) -> Optional[str]:
    """The id a skipped entry keeps for itself because it carries a hidden list: a later entry with that id (a
    hand edit, a bad merge) must not be served without it. Only an id that goes with a valid sha256 counts."""
    if not isinstance(item, dict) or item.get('hidden') is None:
        return None
    sha = item.get('sha256')
    if isinstance(sha, str) and SHA_RE.fullmatch(sha) and item.get('id') == sha[:16]:
        return sha[:16]
    return None


def _usable(items: list) -> Tuple[List[Tuple[int, dict]], List[dict]]:
    """(position, entry) of the usable entries of an exports list, and the skipped ones as {index, reason}."""
    usable: List[Tuple[int, dict]] = []
    skipped: List[dict] = []
    seen: Set[str] = set()
    reserved: Set[str] = set()
    for n, item in enumerate(items[:MAX_ENTRIES]):
        try:
            entry, reason = _entry(item)
        except Exception as exc:  # whatever the entry holds, it only skips itself
            entry, reason = None, 'unreadable entry (%s)' % type(exc).__name__
        if entry is not None and entry['id'] in seen:
            entry, reason = None, 'duplicate id; the first entry is used'
        elif entry is not None and entry['id'] in reserved:
            entry, reason = None, 'duplicate id; an earlier entry with this id is not valid'
        if entry is None:
            skipped.append({'index': n, 'reason': reason})
            claimed = _reserved_id(item)
            if claimed is not None:
                reserved.add(claimed)
            continue
        seen.add(entry['id'])
        usable.append((n, entry))
    if len(items) > MAX_ENTRIES:
        skipped.append({'index': MAX_ENTRIES, 'reason': 'more than %d entries; the rest are ignored' % MAX_ENTRIES})
    return usable, skipped


def _entries(items: list) -> Tuple[List[dict], List[dict]]:
    """The usable entries of an exports list, and the skipped ones as {index, reason}."""
    usable, skipped = _usable(items)
    return [entry for _, entry in usable], skipped


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
        self._started = clock()                # until a first good answer, outdated() counts from here
        self._ok = False
        self._error: Optional[str] = None
        self._inflight: Optional[threading.Event] = None
        self._generation = 0  # bumped by every catalog installed here (read or written) and by expire()
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
            etag, generation = self._etag, self._generation
        try:
            status, tag, body = self.store.get(CATALOG_NAME, 'catalog.json', etag=etag, timeout=self.timeout)
            if status == 304:
                with self._lock:
                    if self._generation == generation:  # else dropped, like a 200 below
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
                if self._generation == generation:  # else a read asked for meanwhile is still due
                    self._checked = self._clock()
                self._ok, self._error = False, message
            if changed:
                self._log('catalog: %s; keeping the last good catalog' % message)
            return
        with self._lock:
            if self._generation != generation:
                # While this answer was on its way, this instance served a catalog it wrote, or a newer read was asked
                # for (expire, or an adopt that declined): keep what is in use; a read asked for is made at next use.
                return
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
        self._generation += 1
        self._entries, self._skipped, self._updated_at, self._deleted = entries, skipped, updated, deleted
        self._by_id = {entry['id']: entry for entry in entries}
        self._etag = etag
        self._fetched = self._checked = self._clock()
        self._ok, self._error = True, None

    @property
    def generation(self) -> int:
        """Changes whenever another catalog is installed here (read or written) and at expire(). A write reads it
        before it reads catalog.json, and passes it to adopt()."""
        with self._lock:
            return self._generation

    def adopt(self, raw: bytes, etag: Optional[str], since: Optional[int] = None) -> bool:
        """Serve a catalog this instance has just written (with the ETag storage gave it), or has just read for a
        write, at once instead of after the next refresh; a refresh already on its way is dropped. since is the
        generation from before that read of catalog.json: when another catalog was installed after it (a refresh,
        which may be newer or older than this one) this one never replaces it, and catalog.json is read again at the
        next use instead. Returns whether it was installed."""
        deleted: List[dict] = []
        entries, skipped, updated = parse_catalog(raw, deleted)
        with self._lock:
            if since is not None and since != self._generation:
                self._checked = None
                self._generation += 1
                return False
            self._install(entries, skipped, updated, deleted, etag)
            return True

    def expire(self) -> None:
        """Read catalog.json again at the next use, whatever MJV_CATALOG_TTL_S says (a refresh already on its way
        is dropped: its answer may be older than what asked for this)."""
        with self._lock:
            self._checked = None
            self._generation += 1

    @property
    def unread_limit_s(self) -> float:
        return max(UNREAD_LIMIT_S, UNREAD_TTLS * self.ttl_s)

    def outdated(self) -> bool:
        """True once no read of catalog.json has succeeded (a 200 that parsed, or a 304) for unread_limit_s: the
        copy in use may then list an export deleted meanwhile."""
        with self._lock:
            last = self._fetched if self._fetched is not None else self._started
            return self._clock() - last >= self.unread_limit_s

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

    def peek(self, catalog_id: str) -> Optional[dict]:
        """The entry in use, without refreshing. Safe under Published's lock: this class never calls into
        Published while it holds its own lock (the lock order is always Published's, then this one)."""
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


# How a ready export applies its entry's hidden list in this process: the record key it was built for, the closure,
# and the visible index of every root ({export id: index}); or, when building failed, error instead of views.
_Variant = namedtuple('_Variant', 'prep key closure views error')
# What an export route serves (Published.resolve): the entry as read, the full index of the requested root, the index
# everyone but its managers sees (the full one while nothing is hidden), the closure and the record (None while
# nothing is hidden), and the full index of every root of the ZIP.
Resolved = namedtuple('Resolved', 'entry full visible closure record roots')


def _visible_summary(entry: dict) -> dict:
    """The published summary less what is hidden: unknown (null) counts while the hidden list was not counted."""
    summary = dict(entry['summary'])
    record = entry.get('hidden')
    if record:
        counts = record['counts']
        for key in HIDDEN_COUNTS:
            if counts is None:
                summary[key] = None
            elif summary.get(key) is not None:
                summary[key] = max(0, summary[key] - counts[key])
    return summary


def _shown(variant: Optional[_Variant]) -> Tuple[int, int]:
    """How many generations and moodboards an applied hidden list leaves out, over every root."""
    if variant is None or variant.views is None:
        return 0, 0
    present = [index.hidden.present for index in variant.views.values()]
    return sum(p['jobs'] for p in present), sum(p['moodboards'] for p in present)


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
        self._unavailable = False                # catalog.outdated() when it was last checked
        self._variants: Dict[str, _Variant] = {}         # catalog id -> its hidden list as applied here
        self._rebuilds: Dict[str, threading.Event] = {}  # catalog id -> set when its running rebuild ends
        # Building an index pauses the cyclic GC for the whole process (model._gc_paused): one visible build at a time.
        self._view_builds = threading.BoundedSemaphore(1)
        # This instance's writes of catalog.json, one at a time from the fresh read to serving what was written, so
        # that it serves them in the order storage took them (never the older of two after the newer).
        self._writes = threading.Lock()
        catalog.on_change = self._on_catalog_change

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
        """Drop finished preparations and close their exports (unpublished, or their ZIP was evicted), visible
        indexes included."""
        with self._lock:
            for prep in preps:
                catalog_id = prep.entry['id']
                if self._preps.get(catalog_id) is prep:
                    del self._preps[catalog_id]
                variant = self._variants.get(catalog_id)
                if variant is not None and variant.prep is prep:
                    del self._variants[catalog_id]
        for prep in preps:
            self.registry.drop(prep.export_ids)

    def _roots(self, prep: _Prep) -> list:
        """The full index of every root a preparation opened, in root order."""
        return [index for index in map(self.registry.get, prep.export_ids) if index is not None]

    def _variant(self, entry: dict, prep: _Prep) -> Optional[_Variant]:
        """The hidden list of an entry as applied for this preparation, built or failed; None while it is not."""
        key = record_key(entry)
        with self._lock:
            variant = self._variants.get(entry['id'])
        return variant if variant is not None and variant.prep is prep and variant.key == key else None

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
                indexes = self._roots(prep)
                if record_key(entry):  # only what everyone sees, once the hidden list is applied
                    variant = self._variant(entry, prep)
                    if variant is None:
                        state = 'opening'
                    elif variant.error is not None:
                        state, error = 'failed', dict(variant.error)
                    else:
                        indexes = [variant.views[index.id] for index in indexes if index.id in variant.views]
                if state == 'ready':
                    exports = [index.summary for index in indexes]
                    failed_roots = [dict(problem) for problem in prep.failed_roots]
        elif self.cache.present(sha):
            state = 'cached'
        elif self.cache.locked_elsewhere(sha):
            state, done = 'downloading', self.cache.foreign_progress(sha) or 0
        return {'id': entry['id'], 'name': entry['name'], 'label': entry['label'], 'size': size,
                'published_at': entry['published_at'], 'published_by': entry['published_by'],
                'owner': entry['owner'], 'summary': _visible_summary(entry), 'state': state,
                'progress': {'done': done, 'total': size} if state in ('downloading', 'verifying') else None,
                'error': error, 'exports': exports, 'failed_roots': failed_roots}

    def listing(self) -> Tuple[List[dict], dict]:
        entries = self.catalog.entries()
        self._require_catalog()
        return [self._item(entry) for entry in entries], self.catalog.status()

    def item(self, catalog_id: str) -> Optional[dict]:
        entry = self.catalog.entry(catalog_id)
        self._require_catalog()
        if entry is None:
            return None
        self._ensure_variant(entry['id'])  # an open export applies a changed hidden list now (never a failed one)
        return self._item(entry)

    def entry(self, catalog_id: str) -> Optional[dict]:
        """The catalog entry in use (read again when stale), or None; CatalogUnavailable (503) while catalog.json
        has not been read successfully for too long."""
        entry = self.catalog.entry(catalog_id)
        self._require_catalog()
        return entry

    def hidden_counts(self, catalog_id: str) -> Optional[dict]:
        """For an export's managers: {jobs, moodboards, images} hidden from everyone else (each null while not
        counted), or None while it hides nothing. The entry in use, without refreshing."""
        record = (self.catalog.peek(catalog_id) or {}).get('hidden')
        if not record:
            return None
        return dict(record['counts']) if record['counts'] else dict.fromkeys(HIDDEN_COUNTS)

    def _available(self) -> bool:
        """False while catalog.json has not been read successfully for too long (Catalog.outdated); every export
        open here is then closed, so that one deleted meanwhile is never served from a copy that still lists it.
        A change either way is logged."""
        outdated = self.catalog.outdated()
        with self._lock:
            changed, self._unavailable = outdated != self._unavailable, outdated
            ready = [p for p in self._preps.values() if p.state == 'ready'] if outdated else []
        self._forget(ready)
        closed = ''.join('; closed %s' % prep.entry['id'] for prep in ready)
        if changed and outdated:
            self._log('catalog: no successful read for %d minutes; no export is served until one succeeds%s'
                      % (self.catalog.unread_limit_s // 60, closed))
        elif changed:
            self._log('catalog: read successfully again; exports are served again')
        elif closed:
            self._log('catalog: still no successful read%s' % closed)
        return not outdated

    def _require_catalog(self) -> None:
        """CatalogUnavailable (503) while catalog.json has not been read successfully for too long."""
        if not self._available():
            raise CatalogUnavailable()

    # ------------------------------------------------------------ actions

    def open(self, catalog_id: str, retry: bool = False) -> Optional[dict]:
        """Start making an entry ready; returns its state at once while the work runs in the background.

        A failed preparation starts again only with retry (someone asked for it). Without it the
        failure is answered as it is, so that automatic calls (a UI polling several instances) can
        never restart a broken download over and over.
        """
        entry = self.catalog.entry(catalog_id)
        self._require_catalog()
        if entry is None:
            return None
        self._start(entry, retry=retry)
        # An open export applies a changed hidden list, and with retry builds a failed one again.
        self._ensure_variant(entry['id'], retry=retry)
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
        """(Resolved, None) when the export is open here and its hidden list applied; (None, item) while it is not
        ready; (None, None) if unknown.

        A ZIP already in the shared cache (another instance downloaded it) is verified and
        opened here, waiting up to wait_s. A request never waits for a download: while one
        runs, the item comes back at once (the UI shows its progress). Downloads start only
        through open().

        Strict switch-over: an entry with a hidden list is served only through the visible indexes built for that
        very list. While they are being built (a list this instance just learned of) the request waits, up to
        wait_s in all, then gets the item (state opening); after a failed build, the item (state failed).
        """
        match = EXPORT_ID_RE.fullmatch(export_id or '')
        entry = self.catalog.entry(match.group(1)) if match else None
        self._require_catalog()
        if entry is None:
            return None, None  # unknown, or no longer published
        deadline = time.monotonic() + wait_s
        prep = self._ready_prep(entry, wait_s)
        if prep is not None:  # it may have waited for the opening: decide by the entry in use now, not the one before
            entry = self.catalog.peek(entry['id'])
            if entry is None:
                return None, None
            if not self._is_ready(prep, entry):
                prep = None
        if prep is None:
            return None, self._item(entry)
        full = self.registry.get(export_id)
        if full is None or export_id not in prep.export_ids:
            return None, None  # ready, but no export root has this id
        self.cache.touch(entry['sha256'], every_s=TOUCH_EVERY_S)
        roots = self._roots(prep)
        if not record_key(entry):
            return Resolved(entry, full, full, None, None, roots), None
        variant = self._variant(entry, prep)
        if variant is None:
            self._ensure_variant(entry['id']).wait(max(0.0, deadline - time.monotonic()))
            entry = self.catalog.peek(entry['id'])
            if entry is None:
                return None, None
            if not record_key(entry):
                return Resolved(entry, full, full, None, None, roots), None
            variant = self._variant(entry, prep)
        if variant is None or variant.error is not None or export_id not in variant.views:
            return None, self._item(entry)
        return Resolved(entry, full, variant.views[export_id], variant.closure, entry['hidden'], roots), None

    @staticmethod
    def _is_ready(prep: Optional[_Prep], entry: dict) -> bool:
        return prep is not None and prep.state == 'ready' and prep.entry['sha256'] == entry['sha256']

    def _ready_prep(self, entry: dict, wait_s: float) -> Optional[_Prep]:
        """The ready preparation of an entry, or None while it is not ready. A ZIP already in the shared cache is
        opened here (waiting up to wait_s); a download is never started or waited for."""
        prep = self._prep(entry['id'])
        if not self._is_ready(prep, entry):
            present = self.cache.present(entry['sha256'])
            if prep is None and present:
                prep = self._start(entry, retry=False)
            if prep is not None and present and prep.state not in ('failed', 'waiting', 'downloading'):
                prep.finished.wait(wait_s)
            prep = self._prep(entry['id'])
        return prep if self._is_ready(prep, entry) else None

    def full_indexes(self, catalog_id: str, wait_s: float):
        """(the full index of every root, None) of an open entry, also while its hidden list is not applied or
        failed to apply (hiding, showing again and the Hidden listing need only these); (None, item) while it is
        not open; (None, None) for an unknown entry."""
        entry = self.catalog.entry(catalog_id)
        self._require_catalog()
        if entry is None:
            return None, None
        prep = self._ready_prep(entry, wait_s)
        roots = self._roots(prep) if prep is not None else []
        if prep is None or len(roots) != len(prep.export_ids):
            return None, self._item(entry)
        return roots, None

    def _run(self, prep: _Prep) -> None:
        entry = prep.entry
        try:
            with self._slots:
                path = self._local_copy(prep)
            prep.stage('opening')
            indexes = self.registry.open(path, catalog={'id': entry['id'], 'name': entry['name'],
                                                        'label': entry['label']}, failures=prep.failed_roots)
            prep.export_ids = [index.id for index in indexes]
            # The hidden list this instance knows now (and any newer one meanwhile) is applied before anyone is
            # served; a build that fails leaves the export ready for its managers only.
            variant = self._install_views(entry['id'], prep, indexes, [], quiet=True)
            prep.stage('ready')
            jobs, boards = _shown(variant)
            self._log('opened %s: %d export root(s)%s%s' % (entry['id'], len(indexes), ''.join(
                '; root %s could not be read' % problem['root'] for problem in prep.failed_roots),
                '; hiding %d generations and %d moodboards' % (jobs, boards) if variant and variant.views else ''))
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

    # ------------------------------------------------------- hidden lists

    def _install_views(self, catalog_id: str, prep: _Prep, indexes: list, reuse: list,
                       quiet: bool) -> Optional[_Variant]:
        """Apply the hidden list of the entry in use to an opened export: build the visible index of every root
        (one build at a time in this process) and install them in one assignment, for exactly that list. When the
        list changes meanwhile it goes on with the newer one; it never installs anything for a preparation that
        was closed or replaced. reuse is [closure, views] of the list applied before, or []: those views serve the
        new list as they are when its closure is the same (the same items left out); otherwise they are dropped
        (reuse is emptied) before the new ones are built. A build that raises installs a failed variant: the
        export is then served to its managers only (the full indexes stay). Returns what it installed."""
        while True:
            entry = self.catalog.peek(catalog_id)
            key = record_key(entry)
            if not key:  # nothing hidden (or no entry any more): the full indexes are served as they are
                with self._lock:
                    variant = self._variants.get(catalog_id)
                    if variant is not None and variant.prep is prep:
                        del self._variants[catalog_id]
                return None
            record, closure = entry['hidden'], None
            try:
                closure = hidden_closure(indexes, record['jobs'], record['moodboards'])
                if reuse and reuse[0].key == closure.key:
                    views = reuse[1]
                else:
                    reuse.clear()
                    with self._view_builds:
                        views = {index.id: index.without(index.hidden_view(closure)) for index in indexes}
                    reuse[:] = [closure, views]
            except Exception as exc:
                name = type(exc).__name__
                self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
                self._log('could not apply the hidden list of %s (%s); it is not served on this instance until it '
                          'is applied' % (catalog_id, name))
                failed = _Variant(prep, key, closure, None, {
                    'code': 'hidden_apply_failed', 'message': 'could not apply the list of hidden items (%s)' % name})
                with self._lock:
                    if self._preps.get(catalog_id) is not prep or prep.state not in ('opening', 'ready'):
                        return None
                    if record_key(self.catalog.peek(catalog_id)) == key:
                        self._variants[catalog_id] = failed
                        return failed
                continue  # the list changed meanwhile: try the newer one
            variant = _Variant(prep, key, closure, views, None)
            with self._lock:
                if self._preps.get(catalog_id) is not prep or prep.state not in ('opening', 'ready'):
                    return None  # closed or replaced meanwhile: never resurrect it, never touch its successor
                if record_key(self.catalog.peek(catalog_id)) == key:
                    self._variants[catalog_id] = variant
                    break
        if not quiet:
            self._log('applied the hidden list of %s (%d generations, %d moodboards hidden)'
                      % ((catalog_id,) + _shown(variant)))
        return variant

    def _ensure_variant(self, catalog_id: str, retry: bool = False, quiet: bool = False) -> threading.Event:
        """Make an open export apply the hidden list of its entry in use. Returns an event that is set once that
        is done: the running rebuild's, or one already set when there is nothing to build (not open here, the list
        applied already, nothing hidden, or a build that failed for this very list, which is built again only
        with retry). Otherwise the variant in use stops being served at once and a rebuild starts."""
        with self._lock:
            running = self._rebuilds.get(catalog_id)
            if running is not None:
                return running
            prep = self._preps.get(catalog_id)
            entry = self.catalog.peek(catalog_id)
            key = record_key(entry)
            variant = self._variants.get(catalog_id)
            if variant is not None and (variant.prep is not prep or not key):
                del self._variants[catalog_id]  # of a closed preparation, or of a list removed since
                variant = None
            applied = variant is not None and variant.key == key and (variant.error is None or not retry)
            if entry is None or not self._is_ready(prep, entry) or not key or applied or self._stop.is_set():
                done = threading.Event()
                done.set()
                return done
            if variant is not None:
                del self._variants[catalog_id]  # another list, or a failed build: never served from now on
            reuse = [variant.closure, variant.views] if variant is not None and variant.views is not None else []
            event = self._rebuilds[catalog_id] = threading.Event()
            thread = threading.Thread(target=self._rebuild, args=(catalog_id, prep, reuse, event, quiet),
                                      name='mjv-hide-%s' % catalog_id, daemon=True)
            self._threads.add(thread)
        thread.start()
        return event

    def _rebuild(self, catalog_id: str, prep: _Prep, reuse: list, event: threading.Event, quiet: bool) -> None:
        try:
            indexes = [self.registry.get(export_id) for export_id in prep.export_ids]
            if indexes and all(index is not None for index in indexes):  # else closed meanwhile: nothing to do
                self._install_views(catalog_id, prep, indexes, reuse, quiet)
        except Exception:  # never let a rebuild take anything else down
            self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
        finally:
            with self._lock:
                if self._rebuilds.get(catalog_id) is event:
                    del self._rebuilds[catalog_id]
                self._threads.discard(threading.current_thread())
            event.set()

    def _follow_hidden(self) -> None:
        """Every open export whose entry in use names another hidden list than the one applied here (or a list
        where none is applied, or none where one is) applies it now."""
        entries, _ = self.catalog.snapshot()
        keys = {entry['id']: record_key(entry) for entry in entries}
        with self._lock:
            due = [catalog_id for catalog_id, prep in self._preps.items() if prep.state == 'ready'
                   and catalog_id in keys and keys[catalog_id] != (
                       self._variants[catalog_id].key if catalog_id in self._variants else '')]
        for catalog_id in due:
            self._ensure_variant(catalog_id)

    def _on_catalog_change(self) -> None:
        """A changed catalog.json was read: follow its deletions and its hidden lists."""
        try:
            self.purge_deleted()
        finally:
            self._follow_hidden()

    def change_hidden(self, catalog_id: str, user: str, admin: bool, kind: str, item_id: str, show: bool,
                      roots: Optional[list], wait_s: float, member: bool = False) -> dict:
        """Hide one job or moodboard ('job' or 'moodboard') of a published export from everyone but its managers,
        or (show) show it again, for its owner or an administrator. member: a moodboard, which anyone signed in
        may hide (MJV_MEMBERS_HIDE_MOODBOARDS). Then the owner is not checked, and on catalog.json as storage holds
        it a user who is neither owner nor administrator acts as anyone: hiding a moodboard listed already (theirs
        included) answers unknown_moodboard, like an id the export does not have; hiding stops at
        MAX_ANYONE_HIDDEN items (hide_limit, which counts nothing); showing again changes only a moodboard every
        item of which names them as hidden_by (else nothing changes, as for one not hidden at all). roots are the
        full indexes of every root of the ZIP, which count what the list hides; None writes the list without counts
        (a show again of an export that cannot be opened). The entry is rewritten through _rewrite like a deletion;
        a request that changes nothing writes nothing. Then this instance serves the new catalog and applies the
        list before it answers (waiting up to wait_s). Returns {changed, counts}. CatalogError codes:
        hide_disabled, unknown_catalog_entry, not_owner, unknown_moodboard, too_many_hidden, hide_limit,
        catalog_busy, storage_error, hide_unconfirmed."""
        if not self.store.writable:
            raise CatalogError('hide_disabled', 'hiding is not turned on for this viewer')
        if member and kind != 'moodboard':
            raise CatalogError('not_owner', ACTION_WORDS['hide']['not_owner'])
        name = 'moodboards' if kind == 'moodboard' else 'jobs'
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._wall()))

        def this(item) -> bool:
            return isinstance(item, dict) and item.get('id') == item_id

        def change(data: dict, raw_entry: dict, entry: dict):
            anyone = member and not admin and entry['owner'] != user  # who they are, as storage holds it
            record = raw_entry.get('hidden')
            record = record if isinstance(record, dict) else {}  # the raw record, edited in place: unknown keys stay
            items = record.get(name) or []
            if show:
                listed = [item for item in items if this(item)]
                if not listed or (anyone and any(str(item.get('hidden_by') or '').lower() != user for item in listed)):
                    return UNCHANGED
                items = [item for item in items if not this(item)]
            else:
                if any(this(item) for item in items):
                    if anyone:  # hidden already: to someone who cannot see it, like an id the export does not have
                        raise CatalogError('unknown_moodboard', 'no moodboard with this id in the export')
                    return UNCHANGED
                total = sum(len(record.get(key) or []) for key in HIDDEN_LISTS)
                if anyone and total >= MAX_ANYONE_HIDDEN:
                    raise CatalogError('hide_limit', HIDE_LIMIT)
                if total >= MAX_HIDDEN_ITEMS:
                    raise CatalogError('too_many_hidden', TOO_MANY_HIDDEN)
                items = items + [{'id': item_id, 'hidden_at': now, 'hidden_by': user}]
            record[name] = items
            parsed, reason = _hidden_record(record)
            if reason:
                raise CatalogError('storage_error', UNREADABLE_CHANGE % reason)
            if parsed is None:  # the last item shown again: nothing hidden, and nothing to count
                counts = dict.fromkeys(HIDDEN_COUNTS, 0)
                for key in HIDDEN_LISTS + ('counts',):
                    record.pop(key, None)
                if record:
                    raw_entry['hidden'] = record
                else:
                    raw_entry.pop('hidden', None)
            else:
                counts = self._count_hidden(catalog_id, roots, parsed) if roots is not None else None
                record.pop('counts', None)
                record['counts'] = counts  # last, after the lists
                raw_entry['hidden'] = record
            checked, reason = _entry(raw_entry)
            if checked is None:  # never write what every instance would then skip
                raise CatalogError('storage_error', UNREADABLE_CHANGE % reason)
            data['updated_at'] = now
            return counts

        def landed(data: dict, counts) -> bool:
            found = next((n for n, entry in _usable(data['exports'])[0] if entry['id'] == catalog_id), None)
            if found is None:
                return False
            record = data['exports'][found].get('hidden')
            items = (record.get(name) or []) if isinstance(record, dict) else []
            if show:
                return not any(this(item) for item in items)
            return any(this(item) and item.get('hidden_at') == now and item.get('hidden_by') == user for item in items)

        with self._writes:  # not held while the list is applied below
            result, body, tag, entry, since = self._rewrite(catalog_id, user, admin, 'hide', change, landed,
                                                            member=member)
            if result is not UNCHANGED:
                what = 'generation' if kind == 'job' else 'moodboard'
                self._log('%s in %s by %s; hidden now: %s' % (
                    ('showed a %s again' if show else 'hid a %s') % what, catalog_id, user,
                    '%d generations, %d moodboards' % (result['jobs'], result['moodboards']) if result
                    else 'not counted'))
                self._serve(body, tag, 'after changing what %s hides' % catalog_id, since)
        if result is UNCHANGED:  # the fresh copy is served already: apply its list here too
            record = entry['hidden']
            if record is None:
                counts = dict.fromkeys(HIDDEN_COUNTS, 0)
            else:
                counts = dict(record['counts']) if record['counts'] else None
            self._ensure_variant(catalog_id, quiet=True).wait(wait_s)
            return {'changed': False, 'counts': counts}
        self._ensure_variant(catalog_id, quiet=True).wait(wait_s)
        return {'changed': True, 'counts': result}

    def _count_hidden(self, catalog_id: str, roots: list, record: dict) -> Optional[dict]:
        """What a hidden list leaves out of a ZIP, summed over its roots like the entry's summary: generations
        (on their own or made with a hidden moodboard) and moodboards present, and image files only they use.
        None (not counted) when counting fails: the counts only describe the list, so they never stop a change,
        least of all a show again that undoes a list this instance cannot apply."""
        try:
            closure = hidden_closure(roots, record['jobs'], record['moodboards'])
            totals = dict.fromkeys(HIDDEN_COUNTS, 0)
            for index in roots:
                present = index.hidden_view(closure).present
                for key in HIDDEN_COUNTS:
                    totals[key] += present[key]
            return totals
        except Exception as exc:
            self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
            self._log('could not count what %s hides (%s); written without counts' % (catalog_id, type(exc).__name__))
            return None

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

        def change(data: dict, raw_entry: dict, entry: dict) -> dict:
            stones = data.get('deleted')
            if stones is None:
                stones = []
            if not isinstance(stones, list):
                raise CatalogError('storage_error', 'catalog.json has a deleted field that is not a list; '
                                                    'nothing was deleted')
            now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self._wall()))
            stone = {'id': catalog_id, 'sha256': entry['sha256'], 'size': entry['size'], 'owner': entry['owner'],
                     'deleted_at': now, 'deleted_by': user}
            data['exports'] = [item for item in data['exports'] if not (
                isinstance(item, dict) and (item.get('id') == catalog_id or item.get('sha256') == entry['sha256']))]
            data['deleted'] = stones + [stone]
            data['updated_at'] = now
            return stone

        def landed(data: dict, stone: dict) -> bool:  # this request's tombstone: the same id, deleted_at, deleted_by
            stones = data.get('deleted')
            return isinstance(stones, list) and any(isinstance(item, dict) and all(
                item.get(key) == stone[key] for key in ('id', 'deleted_at', 'deleted_by')) for item in stones)

        # What follows the write: serve the new catalog.json here at once (under _writes, like the write), delete
        # the ZIP from storage, close the export here.
        with self._writes:
            _, body, tag, entry, since = self._rewrite(catalog_id, user, admin, 'delete', change, landed)
            self._log('deleted %s by %s' % (catalog_id, user))
            if not self._after_delete(catalog_id, 'serving the new catalog.json',
                                      lambda: self.catalog.adopt(body, tag, since)):
                self.catalog.expire()  # the copy in use still lists it: read the new one at the next request
        sha = entry['sha256']
        self._after_delete(catalog_id, 'deleting its ZIP from storage', lambda: self._delete_blob(catalog_id, sha))
        self._after_delete(catalog_id, 'closing it on this instance',
                           lambda: self._purge({catalog_id: sha}, wait_s=CLOSE_WAIT_S, quiet=True))
        return entry

    def _rewrite(self, catalog_id: str, user: str, admin: bool, action: str,
                 change: Callable[[dict, dict, dict], object],
                 landed: Callable[[dict, object], bool],
                 member: bool = False) -> Tuple[object, Optional[bytes], Optional[str], dict, int]:
        """The one way this server writes catalog.json (deleting, hiding and showing again): read it fresh with its
        ETag (never the copy in use), find the entry, check the owner or an administrator on that copy, let change
        edit the raw JSON of that copy in place (every field this server does not know survives), check that every
        instance can read the result, and write it with If-Match on that ETag. When another writer got there
        first, all of it starts over, WRITE_ATTEMPTS times at most. change(data, raw entry, entry) returns a result
        for landed, or UNCHANGED: then nothing is written and the fresh copy is served at once instead. A write
        that got no answer, or a 5xx, may have been stored all the same: catalog.json is read again and
        landed(data, result) tells. Returns (result, body written, its ETag, the entry as read, the catalog's
        generation from before that read, for adopt). action ('delete' or 'hide') words the errors; a delete's are the
        ones it always had. member (hiding a moodboard as anyone signed in may): the owner is not checked, and change
        decides what such a user may do. Called holding _writes, which the caller keeps until it has served what was
        written."""
        words = ACTION_WORDS[action]
        match = EXPORT_ID_RE.fullmatch(catalog_id or '')
        if not match or match.group(2):
            raise CatalogError('unknown_catalog_entry', NOT_LISTED)
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            since = self.catalog.generation
            data, etag, raw = self._fresh_catalog(words['nothing'])
            found = next(((n, entry) for n, entry in _usable(data['exports'])[0] if entry['id'] == catalog_id), None)
            if found is None:
                raise CatalogError('unknown_catalog_entry', NOT_LISTED)
            position, entry = found
            owner = entry['owner']
            if not admin and not member and (owner is None or owner != user):
                raise CatalogError('not_owner', words['not_owner'] if owner else words['no_owner'])
            result = change(data, data['exports'][position], entry)
            if result is UNCHANGED:
                self._serve(raw, etag, 'after an unchanged request on %s' % catalog_id, since)
                return result, None, None, entry, since
            body = _encode(data, grow_from=None if action == 'delete' else len(raw), nothing=words['nothing'])
            try:
                tag = self.store.put(CATALOG_NAME, body, 'catalog.json', if_match=etag)
            except CatalogError as exc:
                if exc.code == 'changed':
                    if attempt < WRITE_ATTEMPTS:
                        time.sleep(random.uniform(0.05, 0.25) * attempt)
                    continue
                if exc.code != 'unreachable' and (exc.status or 0) < 500:  # refused: certainly not stored
                    raise CatalogError('storage_error', '%s; %s' % (exc.message, words['nothing'])) from None
                body, tag = self._stored_anyway(lambda stored: landed(stored, result), exc, words)
            return result, body, tag, entry, since
        raise CatalogError('catalog_busy', 'the list of exports kept changing while it was being updated; '
                                           '%s, try again' % words['nothing'])

    def _serve(self, body: bytes, tag: Optional[str], when: str, since: int) -> None:
        """Serve a catalog.json just written or read fresh at once, unless a newer one may be in use (Catalog.adopt);
        when that fails, read it again at the next request instead (the copy in use may be older)."""
        try:
            self.catalog.adopt(body, tag, since)
        except Exception as exc:
            self.catalog.expire()
            self._log('catalog: %s, serving the new catalog.json failed (%s)' % (when, type(exc).__name__))
            self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())

    def _stored_anyway(self, landed: Callable[[dict], bool], problem: CatalogError,
                       words: dict) -> Tuple[bytes, Optional[str]]:
        """After a write of catalog.json that got no answer or a 5xx: catalog.json as storage holds it now, when
        landed finds this request's change in it, so the write was stored. Otherwise CatalogError
        delete_unconfirmed (hide_unconfirmed): from here, whether it changed cannot be told."""
        self._log('catalog: no clear answer to the write of catalog.json (%s); reading it again'
                  % self.store.scrub(problem.message))
        try:
            status, etag, raw = self.store.get(CATALOG_NAME, 'catalog.json', timeout=self.catalog.timeout)
            stored = _load(raw) if status == 200 else None
            unknown = words['not_shown']
        except CatalogError as exc:
            stored, unknown = None, 'reading catalog.json again failed: %s' % exc.message
        if stored is not None and landed(stored):
            self._log('catalog: the write of catalog.json was stored')
            return raw, etag
        raise CatalogError(words['unconfirmed'], words['unclear'] % (problem.message, unknown))

    def _after_delete(self, catalog_id: str, what: str, step: Callable[[], object]) -> bool:
        """One step that follows a catalog.json that no longer lists the export. The export is deleted by then, so
        a step that fails is logged, never answered, and the steps after it still run. Returns whether it worked."""
        try:
            step()
            return True
        except Exception as exc:
            self._log('catalog: after deleting %s, %s failed (%s)' % (catalog_id, what, type(exc).__name__))
            self._log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
            return False

    def _fresh_catalog(self, nothing: str = 'nothing was deleted') -> Tuple[dict, str, bytes]:
        """catalog.json as storage holds it right now (never the copy in use): (data, ETag, raw bytes)."""
        try:
            status, etag, raw = self.store.get(CATALOG_NAME, 'catalog.json', timeout=self.catalog.timeout)
            if status != 200 or not etag:
                raise CatalogError('storage_error', 'catalog.json: storage answered HTTP %d without an ETag' % status)
            return _load(raw), etag, raw
        except CatalogError as exc:
            if exc.code == 'not_found':
                raise CatalogError('unknown_catalog_entry', NOT_LISTED) from None
            raise CatalogError('storage_error', '%s; %s' % (exc.message, nothing)) from None

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

    def _waiting_blobs(self) -> List[dict]:
        """The newest tombstone of each ZIP that storage may still hold and that this instance deletes (with
        MJV_CATALOG_WRITE_URL): deleted in the last TOMBSTONE_RETRY_S, not listed again, and not yet confirmed
        gone (or kept, as uploaded again after the deletion)."""
        if not self.store.writable:
            return []
        entries, stones = self.catalog.snapshot()
        listed = {entry['sha256'] for entry in entries}
        latest: Dict[str, dict] = {}
        for stone in stones:
            known = latest.get(stone['sha256'])
            if known is None or stone['deleted_ts'] > known['deleted_ts']:
                latest[stone['sha256']] = stone
        wall = self._wall()
        with self._lock:
            return [stone for sha, stone in latest.items() if sha not in listed and sha not in self._blobs_done
                    and wall - stone['deleted_ts'] <= TOMBSTONE_RETRY_S]

    def zip_deletes_pending(self) -> int:
        """How many ZIPs of deleted exports this instance still has to delete from storage (/healthz)."""
        return len(self._waiting_blobs())

    def _retry_blob_deletes(self) -> None:
        """Delete the ZIPs of exports deleted in the last TOMBSTONE_RETRY_S that storage may still hold (an
        earlier DELETE failed, or another instance wrote the tombstone), each at most every BLOB_RETRY_S and
        once confirmed gone never again; never the ZIP of an export that is listed again, nor one uploaded
        again after its latest tombstone."""
        now = self._clock()
        waiting = self._waiting_blobs()
        with self._lock:
            due = [stone for stone in waiting if stone['sha256'] not in self._blob_tries
                   or now - self._blob_tries[stone['sha256']] >= BLOB_RETRY_S]
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
        self._available()  # closes the exports open here once catalog.json has not been read for too long
        if not self.catalog.loaded:
            return
        self.purge_deleted()
        self._follow_hidden()
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
