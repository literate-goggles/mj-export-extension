"""A local stand-in for a private blob container, for hosted-mode tests.

FakeBlob serves the files under a folder at http://127.0.0.1:<port>/<container>/<name>,
the way a blob container answers requests signed with a SAS:

* every request must carry exactly one of two configured query strings, else
  403: a read-only one (as in MJV_CATALOG_URL) and one that may also write and
  delete (as in MJV_CATALOG_WRITE_URL). Each allows what its sp parameter says
  (r: GET, w: PUT, d: DELETE); anything else gets 403 as well, so a read-only
  query string can never write or delete;
* GET: every object has an ETag, and a matching If-None-Match gets 304;
* PUT (a whole block blob) honours If-Match and If-None-Match: *, and DELETE
  If-Unmodified-Since, answering 412 when the condition fails; every write gives
  the object a new ETag; DELETE answers 202, or 404 when the object is missing;
* every answer has a Content-Length;
* faults can be injected per object name: for GET 403, 404, 500, a redirect (302
  to another object of this container, which is never served to anyone who
  follows it), a truncated body (the full Content-Length, then the connection
  closes half way), a slow body (a pause before each chunk) and a body without
  Content-Length (it ends when the connection closes); for PUT and DELETE
  (write_faults) 403, 404, 409, 412, 500, a redirect, and 'drop': the write is
  done, then the connection closes without an answer. before_write(name,
  method), when set, runs before each PUT or DELETE is checked, to stand in for
  another writer that gets there first.

It records each request's method, object name, conditional headers, status and
which query string it carried ('read', 'write' or None), never the query string
itself. Everything it serves is test data written by the tests.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional

DEFAULT_QUERY = 'sv=2099-01-01&ss=b&srt=co&sp=rl&se=2099-12-31T00%3A00%3A00Z&sig=TESTONLYsig0123456789abcdefSECRET%3D'
DEFAULT_WRITE_QUERY = ('sv=2099-01-01&ss=b&srt=co&sp=rwdl&se=2099-12-31T00%3A00%3A00Z'
                       '&sig=TESTONLYwriteSig9876543210fedcbaSECRET%3D')
FAULTS = ('403', '404', '500', '302', 'truncate', 'slow', 'nolength')
WRITE_FAULTS = {'403': (403, 'AuthorizationPermissionMismatch'), '404': (404, 'BlobNotFound'),
                '409': (409, 'LeaseIdMissing'), '412': (412, 'ConditionNotMet'), '500': (500, 'InternalError')}
DROP = 'drop'  # a write fault: the write is done, but its answer never arrives
REDIRECT_TARGET = 'redirected-here.zip'


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'fake-blob'
    sys_version = ''

    def log_message(self, format, *args):  # noqa: A002 - quiet
        pass

    @property
    def blob(self) -> 'FakeBlob':
        return self.server.blob

    def _plain(self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _refuse(self, status: int, code: str) -> None:
        self._plain(status, ('<?xml version="1.0" encoding="utf-8"?><Error><Code>%s</Code></Error>' % code).encode())

    def _target(self):
        path, _, query = self.path.partition('?')
        prefix = '/%s/' % self.blob.container
        return (urllib.parse.unquote(path[len(prefix):]) if path.startswith(prefix) else ''), query

    def _allowed(self, query: str, letter: str, record) -> bool:
        """Whether the query string grants this permission; refuses the request when it does not."""
        permissions = self.blob.permissions(query)
        if permissions is None:
            record(403)
            self._refuse(403, 'AuthenticationFailed')
            return False
        if letter not in permissions:
            record(403)
            self._refuse(403, 'AuthorizationPermissionMismatch')
            return False
        return True

    def do_GET(self):  # noqa: N802
        name, query = self._target()
        etag_in = self.headers.get('If-None-Match')
        sas = self.blob.sas(query)
        self._serve(name, query, etag_in, lambda status: self.blob._record(name, status, 'GET', sas,
                                                                          if_none_match=etag_in))

    def _serve(self, name: str, query: str, etag_in: Optional[str], record) -> None:
        # Each request is recorded before its answer is sent, so a client never finishes first.
        if not self._allowed(query, 'r', record):
            return None
        fault = self.blob.faults.get(name)
        if fault == '302':
            record(302)
            location = '%s/%s?%s' % (self.blob.base_url, REDIRECT_TARGET, query)
            return self._plain(302, b'', {'Location': location})
        if fault in ('403', '404', '500'):
            record(int(fault))
            return self._refuse(int(fault), {'403': 'AuthorizationPermissionMismatch', '404': 'BlobNotFound',
                                             '500': 'InternalError'}[fault])
        with self.blob.write_lock:  # the ETag of exactly the bytes that are sent, even while another request writes
            path = self.blob.file(name)
            handle = open(path, 'rb') if path is not None else None
            etag = self.blob.etag(name)
        if handle is None:
            record(404)
            return self._refuse(404, 'BlobNotFound')
        with handle:
            size = os.fstat(handle.fileno()).st_size
            if etag_in and etag_in == etag:
                record(304)
                return self._plain(304, b'', {'ETag': etag})
            record(200)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if name.endswith('.json') else 'application/zip')
            if fault == 'nolength':
                self.close_connection = True  # the body ends when the connection does
            else:
                self.send_header('Content-Length', str(size))
            self.send_header('ETag', etag)
            self.end_headers()
            limit = size // 2 if fault == 'truncate' else None
            sent = 0
            while True:
                if fault == 'slow':
                    time.sleep(self.blob.slow_delay)
                chunk = handle.read(self.blob.chunk if fault == 'slow' else 256 * 1024)
                if limit is not None:
                    chunk = chunk[:max(0, limit - sent)]
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return None
                sent += len(chunk)
        if fault == 'truncate':
            self.close_connection = True
        return None

    def _write_fault(self, name: str, query: str, record) -> bool:
        """Run before_write, then answer an injected fault; True when one was answered."""
        if self.blob.before_write is not None:
            self.blob.before_write(name, self.command)
        fault = self.blob.write_faults.get(name)
        if fault == '302':
            record(302)
            self._plain(302, b'', {'Location': '%s/%s?%s' % (self.blob.base_url, REDIRECT_TARGET, query)})
            return True
        if fault in WRITE_FAULTS:
            status, code = WRITE_FAULTS[fault]
            record(status)
            self._refuse(status, code)
            return True
        return False

    def _answer_written(self, name: str, status: int, headers: Dict[str, str]) -> None:
        """Answer a write that was done, unless the 'drop' fault says to hang up instead."""
        if self.blob.write_faults.get(name) == DROP:
            self.close_connection = True
            return None
        return self._plain(status, b'', headers)

    def do_PUT(self):  # noqa: N802
        name, query = self._target()
        length = (self.headers.get('Content-Length') or '').strip()
        body = self.rfile.read(int(length)) if length.isdigit() else b''
        if_match, if_none = self.headers.get('If-Match'), self.headers.get('If-None-Match')

        def record(status):
            self.blob._record(name, status, 'PUT', self.blob.sas(query), if_match=if_match, if_none_match=if_none,
                              content_type=self.headers.get('Content-Type'))

        if not self._allowed(query, 'w', record):
            return None
        if self.headers.get('x-ms-blob-type') != 'BlockBlob' or self.blob.file_path(name) is None:
            record(400)
            return self._refuse(400, 'InvalidHeaderValue')
        if self._write_fault(name, query, record):
            return None
        with self.blob.write_lock:
            current = self.blob.etag(name)
            if (if_none == '*' and current is not None) or (if_match is not None and if_match != current):
                record(412)
                return self._refuse(412, 'ConditionNotMet')
            self.blob.put(name, body)
            etag = self.blob.etag(name)
            record(201)
        return self._answer_written(name, 201, {'ETag': etag})

    def do_DELETE(self):  # noqa: N802
        name, query = self._target()
        since = self.headers.get('If-Unmodified-Since')

        def record(status):
            self.blob._record(name, status, 'DELETE', self.blob.sas(query), if_unmodified_since=since,
                              delete_snapshots=self.headers.get('x-ms-delete-snapshots'))

        if not self._allowed(query, 'd', record):
            return None
        if self._write_fault(name, query, record):
            return None
        with self.blob.write_lock:
            path = self.blob.file(name)
            if path is None:
                record(404)
                return self._refuse(404, 'BlobNotFound')
            if since and int(os.stat(path).st_mtime) > email.utils.parsedate_to_datetime(since).timestamp():
                record(412)
                return self._refuse(412, 'ConditionNotMet')
            self.blob.remove(name)
            record(202)
        return self._answer_written(name, 202, {})

    def _method_not_allowed(self):
        self.close_connection = True
        self._refuse(405, 'UnsupportedHttpVerb')

    do_POST = do_HEAD = _method_not_allowed


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # injected faults close connections on purpose


class FakeBlob:
    """Serve root/<name> as <base_url>/<name>?<query>. MJV_CATALOG_URL is .url, MJV_CATALOG_WRITE_URL .write_url."""

    def __init__(self, root: str, query: str = DEFAULT_QUERY, container: str = 'exports-test',
                 port: int = 0, write_query: str = DEFAULT_WRITE_QUERY):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)
        self.query = query
        self.write_query = write_query
        self.container = container
        self.faults: Dict[str, str] = {}
        self.write_faults: Dict[str, str] = {}
        self.before_write: Optional[Callable[[str, str], None]] = None
        self.chunk = 16 * 1024
        self.slow_delay = 0.02
        self.requests: List[dict] = []
        self.write_lock = threading.RLock()  # PUT checks and writes in one step, as storage does
        self._generation: Dict[str, int] = {}
        self._lock = threading.Lock()
        self.server = _Server(('127.0.0.1', port), _Handler)
        self.server.blob = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return 'http://127.0.0.1:%d/%s' % (self.server.server_address[1], self.container)

    @property
    def url(self) -> str:
        """The container URL plus its read-only query string, as MJV_CATALOG_URL expects."""
        return '%s?%s' % (self.base_url, self.query)

    @property
    def write_url(self) -> str:
        """The container URL plus the query string that may also write and delete, as MJV_CATALOG_WRITE_URL expects."""
        return '%s?%s' % (self.base_url, self.write_query)

    @property
    def secret(self) -> str:
        """The signature value, decoded: it must never show up anywhere in the viewer's output."""
        return dict(urllib.parse.parse_qsl(self.query))['sig']

    @property
    def write_secret(self) -> str:
        """The write query string's signature, decoded: it must never show up anywhere either."""
        return dict(urllib.parse.parse_qsl(self.write_query))['sig']

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def sas(self, query: str) -> Optional[str]:
        """Which configured query string this is: 'read', 'write' or None."""
        if query == self.query:
            return 'read'
        return 'write' if self.write_query and query == self.write_query else None

    def permissions(self, query: str) -> Optional[str]:
        """The sp letters of a configured query string, or None for any other query string."""
        which = self.sas(query)
        if which is None:
            return None
        return dict(urllib.parse.parse_qsl(self.query if which == 'read' else self.write_query)).get('sp', '')

    def file_path(self, name: str) -> Optional[str]:
        """Where an object of this name is stored (it may not exist), or None for a name that is not valid."""
        if not name or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/')):
            return None
        path = os.path.realpath(os.path.join(self.root, *name.split('/')))
        return path if path.startswith(self.root + os.sep) else None

    def file(self, name: str) -> Optional[str]:
        path = self.file_path(name)
        return path if path is not None and os.path.isfile(path) else None

    def etag(self, name: str) -> Optional[str]:
        """A new value after every write of the object, whatever its content; None when it does not exist."""
        path = self.file(name)
        if path is None:
            return None
        info = os.stat(path)
        return '"0x%x-%x-%x"' % (self._generation.get(name, 0), info.st_size, info.st_mtime_ns)

    def _record(self, name: str, status: int, method: str = 'GET', sas: Optional[str] = None, **headers) -> None:
        with self._lock:
            self.requests.append(dict({'name': name, 'method': method, 'sas': sas, 'status': status,
                                       'if_none_match': None}, **headers))

    def count(self, name: str, status: Optional[int] = None, method: Optional[str] = None) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r['name'] == name and (status is None or r['status'] == status)
                       and (method is None or r['method'] == method))

    def put(self, name: str, data: bytes) -> str:
        path = self.file_path(name)
        with self.write_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'wb') as handle:
                handle.write(data)
            os.replace(tmp, path)
            self._generation[name] = self._generation.get(name, 0) + 1
        return path

    def remove(self, name: str) -> bool:
        """Delete an object as storage would; False when there was none."""
        with self.write_lock:
            path = self.file(name)
            if path is None:
                return False
            os.unlink(path)
            self._generation[name] = self._generation.get(name, 0) + 1
            return True

    def read_json(self, name: str):
        with open(self.file(name), 'rb') as handle:
            return json.loads(handle.read().decode('utf-8'))

    def put_export(self, data: bytes, name: str = 'mj_export_synthetic.zip', label: Optional[str] = None,
                   summary: Optional[dict] = None, published_by: str = 'publisher@example.com',
                   sha256: Optional[str] = None, size: Optional[int] = None, owner: Optional[str] = None) -> dict:
        """Store a ZIP as exports/<sha256>.zip and return its catalog entry (sha256/size may lie, for tests).
        owner is only written when given, as in a catalog from before owners were recorded."""
        sha = sha256 or hashlib.sha256(data).hexdigest()
        self.put('exports/%s.zip' % sha, data)
        entry = {'id': sha[:16], 'blob': 'exports/%s.zip' % sha, 'name': name, 'label': label, 'sha256': sha,
                 'size': len(data) if size is None else size, 'published_at': '2026-04-01T10:00:00.000Z',
                 'published_by': published_by,
                 'summary': summary or {'jobs': None, 'moodboards': None, 'profiles': None, 'images': None,
                                        'export_time': None, 'extension_version': None, 'status': None}}
        if owner is not None:
            entry['owner'] = owner
        return entry

    def write_catalog(self, entries: List[dict], **extra) -> None:
        catalog = {'version': 1, 'updated_at': '2026-04-01T10:00:00.000Z', 'exports': entries}
        catalog.update(extra)
        self.put('catalog.json', json.dumps(catalog, indent=1).encode('utf-8'))
