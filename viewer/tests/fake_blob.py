"""A local stand-in for a private blob container, for hosted-mode tests.

FakeBlob serves the files under a folder at http://127.0.0.1:<port>/<container>/<name>,
the way a blob container answers requests signed with a read-only SAS:

* every request must carry exactly the configured query string, else 403;
* GET only; every object has an ETag, and a matching If-None-Match gets 304;
* every answer has a Content-Length;
* faults can be injected per object name: 403, 404, 500, a redirect (302 to
  another object of this container, which is never served to anyone who
  follows it), a truncated body (the full Content-Length, then the connection
  closes half way), a slow body (a pause before each chunk) and a body without
  Content-Length (it ends when the connection closes).

It records each request's object name, If-None-Match value and status, never
the query string. Everything it serves is test data written by the tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

DEFAULT_QUERY = 'sv=2099-01-01&ss=b&srt=co&sp=rl&se=2099-12-31T00%3A00%3A00Z&sig=TESTONLYsig0123456789abcdefSECRET%3D'
FAULTS = ('403', '404', '500', '302', 'truncate', 'slow', 'nolength')
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

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition('?')
        prefix = '/%s/' % self.blob.container
        name = urllib.parse.unquote(path[len(prefix):]) if path.startswith(prefix) else ''
        etag_in = self.headers.get('If-None-Match')
        self._serve(name, query, etag_in, lambda status: self.blob._record(name, etag_in, status))

    def _serve(self, name: str, query: str, etag_in: Optional[str], record) -> None:
        # Each request is recorded before its answer is sent, so a client never finishes first.
        if query != self.blob.query:
            record(403)
            return self._refuse(403, 'AuthenticationFailed')
        fault = self.blob.faults.get(name)
        if fault == '302':
            record(302)
            location = '%s/%s?%s' % (self.blob.base_url, REDIRECT_TARGET, query)
            return self._plain(302, b'', {'Location': location})
        if fault in ('403', '404', '500'):
            record(int(fault))
            return self._refuse(int(fault), {'403': 'AuthorizationPermissionMismatch', '404': 'BlobNotFound',
                                             '500': 'InternalError'}[fault])
        path = self.blob.file(name)
        if path is None:
            record(404)
            return self._refuse(404, 'BlobNotFound')
        info = os.stat(path)
        etag = '"0x%x%x"' % (info.st_size, info.st_mtime_ns)
        if etag_in and etag_in == etag:
            record(304)
            return self._plain(304, b'', {'ETag': etag})
        record(200)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if name.endswith('.json') else 'application/zip')
        if fault == 'nolength':
            self.close_connection = True  # the body ends when the connection does
        else:
            self.send_header('Content-Length', str(info.st_size))
        self.send_header('ETag', etag)
        self.end_headers()
        limit = info.st_size // 2 if fault == 'truncate' else None
        sent = 0
        with open(path, 'rb') as handle:
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

    def _method_not_allowed(self):
        self.close_connection = True
        self._refuse(405, 'UnsupportedHttpVerb')

    do_POST = do_PUT = do_DELETE = do_HEAD = _method_not_allowed


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # injected faults close connections on purpose


class FakeBlob:
    """Serve root/<name> as <base_url>/<name>?<query>. MJV_CATALOG_URL is .url."""

    def __init__(self, root: str, query: str = DEFAULT_QUERY, container: str = 'exports-test',
                 port: int = 0):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)
        self.query = query
        self.container = container
        self.faults: Dict[str, str] = {}
        self.chunk = 16 * 1024
        self.slow_delay = 0.02
        self.requests: List[dict] = []
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
    def secret(self) -> str:
        """The signature value, decoded: it must never show up anywhere in the viewer's output."""
        return dict(urllib.parse.parse_qsl(self.query))['sig']

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def file(self, name: str) -> Optional[str]:
        if not name or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/')):
            return None
        path = os.path.realpath(os.path.join(self.root, *name.split('/')))
        if not path.startswith(self.root + os.sep) or not os.path.isfile(path):
            return None
        return path

    def _record(self, name: str, etag: Optional[str], status: int) -> None:
        with self._lock:
            self.requests.append({'name': name, 'if_none_match': etag, 'status': status})

    def count(self, name: str, status: Optional[int] = None) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r['name'] == name and (status is None or r['status'] == status))

    def put(self, name: str, data: bytes) -> str:
        path = os.path.join(self.root, *name.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'wb') as handle:
            handle.write(data)
        os.replace(tmp, path)
        return path

    def put_export(self, data: bytes, name: str = 'mj_export_synthetic.zip', label: Optional[str] = None,
                   summary: Optional[dict] = None, published_by: str = 'publisher@example.com',
                   sha256: Optional[str] = None, size: Optional[int] = None) -> dict:
        """Store a ZIP as exports/<sha256>.zip and return its catalog entry (sha256/size may lie, for tests)."""
        sha = sha256 or hashlib.sha256(data).hexdigest()
        self.put('exports/%s.zip' % sha, data)
        return {'id': sha[:16], 'blob': 'exports/%s.zip' % sha, 'name': name, 'label': label, 'sha256': sha,
                'size': len(data) if size is None else size, 'published_at': '2026-04-01T10:00:00.000Z',
                'published_by': published_by,
                'summary': summary or {'jobs': None, 'moodboards': None, 'profiles': None, 'images': None,
                                       'export_time': None, 'extension_version': None, 'status': None}}

    def write_catalog(self, entries: List[dict], **extra) -> None:
        catalog = {'version': 1, 'updated_at': '2026-04-01T10:00:00.000Z', 'exports': entries}
        catalog.update(extra)
        self.put('catalog.json', json.dumps(catalog, indent=1).encode('utf-8'))
