"""Local HTTP server for the MJ Export viewer.

Security model (see API.md): binds to 127.0.0.1 by default, rejects foreign
Host headers (DNS rebinding), requires a per-start secret as an HttpOnly
SameSite=Strict cookie for /api/* and /files/*, plus an X-MJV-Token header for
every non-GET request. Every response carries a strict CSP. Files are served
by opaque ids from the server's own file table, never by path, and only when
their magic bytes are a supported raster type. Prompts are never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import threading
import traceback
import urllib.parse
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Optional

from . import API_VERSION, __version__
from .model import ExportIndex, QueryError
from .sources import (CHUNK_SIZE, IMAGE_MIME, FolderSource, Limits, MemberError, SourceError,
                      detect_image_type, display_name, open_source, unsafe_reason)

try:  # optional: thumbnails only
    from PIL import Image as PIL_IMAGE
    from PIL import ImageOps as PIL_IMAGEOPS
    from PIL import features as PIL_FEATURES
except Exception:  # pragma: no cover - Pillow missing or broken
    PIL_IMAGE = None
    PIL_IMAGEOPS = None
    PIL_FEATURES = None

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
LOCAL_HOSTS = {'localhost', '127.0.0.1', '::1'}
DEFAULT_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
THUMB_SIZE = 512
THUMB_MAX_PIXELS = 80_000_000
FILE_CACHE = 'private, max-age=86400'
STATIC_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.webp': 'image/webp',
    '.woff2': 'font/woff2',
    '.woff': 'font/woff',
    '.txt': 'text/plain; charset=utf-8',
    '.map': 'application/json; charset=utf-8',
}
ERROR_STATUS = {'not_found': 404, 'unreadable': 403}


def default_cache_dir() -> str:
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'mj-export-viewer')


def dump_json(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    except UnicodeEncodeError:  # lone surrogates from hostile JSON: escape everything
        return json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _looks_like_export(path: str) -> bool:
    markers = ('export_meta.json', 'jobs.jsonl')
    try:
        if any(os.path.isfile(os.path.join(path, m)) for m in markers):
            return True
        with os.scandir(path) as it:
            for n, entry in enumerate(it):
                if n >= 200:
                    break
                if entry.is_dir(follow_symlinks=False) and any(
                        os.path.isfile(os.path.join(entry.path, m)) for m in markers):
                    return True
    except OSError:
        return False
    return False


# ----------------------------------------------------------------- registry

class Registry:
    """Open exports by id. Opening the same root twice returns the same index."""

    def __init__(self, cache_dir: str, limits: Limits = Limits(), library_dirs: Iterable[str] = ()):
        self.cache_dir = os.path.realpath(cache_dir)
        self.limits = limits
        self.library_dirs = [os.path.realpath(os.path.expanduser(d)) for d in library_dirs]
        self.failed: List[dict] = []
        self._lock = threading.Lock()
        self._exports: Dict[str, ExportIndex] = {}
        self._sources: List[object] = []

    @staticmethod
    def _export_id(source, root: str) -> str:
        if source.kind == 'zip':
            ident = 'zip\0%s\0%d\0%d\0%s' % (source.real_path, source.size, source.mtime_ns, root)
        else:
            ident = 'folder\0%s\0%s' % (source.real_path, root)
        return hashlib.sha256(ident.encode('utf-8', 'surrogatepass')).hexdigest()[:12]

    def open(self, path: str, source_type: Optional[str] = None, display: Optional[str] = None,
             catalog: Optional[dict] = None, failures: Optional[List[dict]] = None) -> List[ExportIndex]:
        """catalog ({id, name, label}) marks a ZIP published to the hosted catalog: its exports are
        named by the catalog id (plus -<n> per root when it holds several) and show no file path.

        A root that fails to load does not stop the others: it is reported in failures (or, when
        none is given, in self.failed) as {path, root, code, message}. Only when no root loads
        does open() raise."""
        source = open_source(path, self.limits)
        created = False
        name = catalog['name'] if catalog else source.name
        try:
            if isinstance(source, FolderSource) and _inside(self.cache_dir, source.real_path):
                raise SourceError('cache_inside_export',
                                  'the viewer cache directory is inside this folder; start the viewer with another --cache-dir')
            roots = source.roots()
            if not roots:
                raise SourceError('no_export_root', 'no export_meta.json or jobs.jsonl found in %s' % display_name(name))
            results = []
            problems = []
            for n, root in enumerate(roots, 1):
                if catalog:
                    export_id = catalog['id'] if len(roots) == 1 else '%s-%d' % (catalog['id'], n)
                else:
                    export_id = self._export_id(source, root)
                with self._lock:
                    existing = self._exports.get(export_id)
                if existing is not None:
                    results.append(existing)
                    continue
                if catalog:
                    info = {'type': 'catalog', 'name': name, 'path': None}
                else:
                    info = {'type': source_type or source.kind, 'name': display or source.name, 'path': source.path}
                try:
                    index = ExportIndex(source.view(root), export_id, info, self.limits)
                except Exception as exc:  # never let one bad export take the server (or the other roots) down
                    traceback.print_exc(limit=3, file=sys.stderr)
                    problems.append({'path': name if catalog else display or path, 'root': root, 'code': 'load_failed',
                                     'message': 'could not read export %s (%s)' % (display_name(root or name),
                                                                                    type(exc).__name__)})
                    continue
                if catalog and catalog.get('label'):
                    index.summary['label'] = catalog['label'] if len(roots) == 1 else '%s › %s' % (
                        catalog['label'], index.summary['label'])
                index.source_real = source.real_path
                with self._lock:
                    if export_id not in self._exports:
                        self._exports[export_id] = index
                        if not created:
                            self._sources.append(source)
                        created = True
                    results.append(self._exports[export_id])
            if not results:
                raise SourceError(problems[0]['code'], problems[0]['message'])
            if failures is None:
                self.note_failed(problems)
            else:
                failures.extend(problems)
            return results
        finally:
            if not created:
                source.close()

    def note_failed(self, problems: List[dict]) -> None:
        """Add paths or export roots that could not be opened to the list /api/exports reports (once each)."""
        with self._lock:
            self.failed.extend(p for p in problems if p not in self.failed)

    def drop(self, export_ids: Iterable[str]) -> None:
        """Forget these exports, and close each source that no remaining export reads from."""
        with self._lock:
            gone = [self._exports.pop(i) for i in export_ids if i in self._exports]
            used = {id(index.view.source) for index in self._exports.values()}
            closing = {id(index.view.source): index.view.source for index in gone
                       if id(index.view.source) not in used}
            self._sources = [s for s in self._sources if id(s) not in closing]
        for source in closing.values():
            try:
                source.close()
            except Exception:  # pragma: no cover
                pass

    def get(self, export_id: str) -> Optional[ExportIndex]:
        with self._lock:
            return self._exports.get(export_id)

    def all(self) -> List[ExportIndex]:
        with self._lock:
            return list(self._exports.values())

    def library(self) -> List[dict]:
        opened = {getattr(index, 'source_real', None) for index in self.all()}
        out = []
        for folder in self.library_dirs:
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)[:1000]
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith('.'):
                    continue
                try:
                    if entry.is_file() and entry.name.lower().endswith('.zip'):
                        kind = 'zip'
                    elif entry.is_dir(follow_symlinks=False) and _looks_like_export(entry.path):
                        kind = 'folder'
                    else:
                        continue
                except OSError:
                    continue
                out.append({'path': entry.path, 'name': entry.name, 'kind': kind,
                            'opened': os.path.realpath(entry.path) in opened})
        return out

    def close(self) -> None:
        with self._lock:
            sources, self._sources = self._sources, []
        for source in sources:
            try:
                source.close()
            except Exception:  # pragma: no cover
                pass


# --------------------------------------------------------------- thumbnails

class Thumbnailer:
    """Pillow thumbnails cached under <cache>/thumbs/<export_id>/. Optional."""

    def __init__(self, cache_dir: str, enabled: bool = True, pil=PIL_IMAGE, size: int = THUMB_SIZE,
                 max_pixels: int = THUMB_MAX_PIXELS, concurrency: int = 4):
        self.dir = os.path.join(cache_dir, 'thumbs')
        self.pil = pil if enabled else None
        self.size = size
        self.max_pixels = max_pixels
        self.renders = 0
        self._sem = threading.BoundedSemaphore(concurrency)
        self._guard = threading.Lock()
        self._inflight: Dict[str, threading.Lock] = {}  # one render per key, however many requests wait
        self._webp = bool(PIL_FEATURES and pil is PIL_IMAGE and PIL_FEATURES.check('webp'))

    @property
    def available(self) -> bool:
        return self.pil is not None

    def get(self, index: ExportIndex, rec) -> Optional[str]:
        """Path of a cached thumbnail, or None to serve the original bytes."""
        if self.pil is None or rec.stat is None:
            return None
        key = hashlib.sha256(('%s\0%s\0%d\0%s\0%d\0v1' % (index.id, rec.rel, rec.stat.size, rec.stat.version, self.size))
                             .encode('utf-8', 'surrogatepass')).hexdigest()
        folder = os.path.join(self.dir, index.id, key[:2])
        path = os.path.join(folder, key)
        if os.path.isfile(path):
            return path
        if os.path.exists(path + '.orig'):
            return None
        with self._guard:
            key_lock = self._inflight.setdefault(key, threading.Lock())
        try:
            with key_lock:
                if os.path.isfile(path):
                    return path
                if os.path.exists(path + '.orig'):
                    return None
                with self._sem:
                    return self._render_to(index, rec, folder, path)
        finally:
            with self._guard:
                self._inflight.pop(key, None)

    def _render_to(self, index: ExportIndex, rec, folder: str, path: str) -> Optional[str]:
        try:
            data = index.view.read_capped(rec.rel, index.limits.max_image_bytes)
            out = self._render(data)
        except Exception:  # MemberError, DecompressionBombError, any decoder error
            out = None
        try:
            os.makedirs(folder, exist_ok=True)
            # Even the empty marker is a temporary file plus a rename: several servers may share the cache.
            target = path + '.orig' if out is None else path
            fd, tmp = tempfile.mkstemp(dir=folder, prefix='.tmp-')
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(out or b'')
                os.replace(tmp, target)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        except OSError:
            return None
        return None if out is None else path

    def _render(self, data: bytes) -> Optional[bytes]:
        self.renders += 1
        with self.pil.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > self.max_pixels or max(width, height) <= self.size:
                return None  # too large to decode safely, or already small: serve the original
            image.draft('RGB', (self.size, self.size))
            frame = PIL_IMAGEOPS.exif_transpose(image) if PIL_IMAGEOPS else image
            frame.thumbnail((self.size, self.size))
            alpha = frame.mode in ('RGBA', 'LA', 'PA') or (frame.mode == 'P' and 'transparency' in frame.info)
            buffer = io.BytesIO()
            if alpha:
                frame = frame.convert('RGBA')
                if self._webp:
                    frame.save(buffer, 'WEBP', quality=82)
                else:
                    frame.save(buffer, 'PNG', optimize=True)
            else:
                frame.convert('RGB').save(buffer, 'JPEG', quality=85, optimize=True)
            return buffer.getvalue()


# ----------------------------------------------------------------------- app

class ViewerApp:
    def __init__(self, static_dir: str = DEFAULT_STATIC, cache_dir: Optional[str] = None,
                 thumbnails: bool = True, allow_hosts: Iterable[str] = (), max_upload_bytes: int = 4096 * 1024 * 1024,
                 library_dirs: Iterable[str] = (), limits: Limits = Limits(), pil=PIL_IMAGE,
                 log=None, token: Optional[str] = None):
        self.static_dir = os.path.realpath(static_dir)
        self.cache_dir = os.path.realpath(os.path.expanduser(cache_dir or default_cache_dir()))
        self.allow_hosts = {h.strip().lower() for h in allow_hosts if h.strip()}
        self.max_upload_bytes = max_upload_bytes
        self.limits = limits
        self.token = token or secrets.token_urlsafe(32)
        self.registry = Registry(self.cache_dir, limits, library_dirs)
        self.thumbs = Thumbnailer(self.cache_dir, enabled=thumbnails, pil=pil)
        self.log_stream = log

    def log(self, message: str) -> None:
        if self.log_stream is not None:
            try:
                self.log_stream.write(message + '\n')
                self.log_stream.flush()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        self.registry.close()


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app: ViewerApp, handler=None):
        self.app = app
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler or Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def handle_error(self, request, client_address):
        """A browser closing or resetting a connection (tab closed, keep-alive dropped) is normal."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def make_server(app: ViewerApp, host: str = '127.0.0.1', port: int = 8765) -> ViewerServer:
    return ViewerServer((host, port), app, getattr(app, 'handler_class', None))


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith('['):
        end = value.find(']')
        rest = value[end + 1:] if end > 0 else 'x'
        if end < 0 or (rest and not re.fullmatch(r':\d{1,5}', rest)):
            return ''
        return value[1:end]
    if value.count(':') == 1:
        name, _, port = value.partition(':')
        return name if port.isdigit() else ''
    return value


def _clean_filename(value: Optional[str]) -> str:
    name = urllib.parse.unquote(value or '', errors='replace')
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(ch for ch in name if ch.isprintable()).strip()[:200]
    return name or 'upload.zip'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'mjviewer'
    sys_version = ''
    timeout = 120
    file_cache = FILE_CACHE  # Cache-Control of /files answers

    # ------------------------------------------------------------ plumbing

    @property
    def app(self) -> ViewerApp:
        return self.server.app

    def log_message(self, format, *args):  # noqa: A002 - silence the default access log
        pass

    def end_headers(self):
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        super().end_headers()
        self._sent_headers = True

    def _route(self) -> str:
        return (getattr(self, 'path', '') or '').partition('?')[0]

    def _target(self):
        """(path, query) for an origin-form target, else None. No urlsplit: '//x/...' must not become a host."""
        raw = self.path or ''
        if not raw.startswith('/') or raw.startswith('//'):
            return None
        path, _, query = raw.partition('?')
        return path, query.partition('#')[0]

    def _note(self, status: int) -> None:
        if status >= 400 or self.command not in ('GET', 'HEAD'):
            # Paths only, never query strings: those may hold prompt search terms.
            self.app.log('%s %s %d' % (self.command, display_name(self._route(), 160), status))

    def _send_bytes(self, status: int, body: bytes, content_type: str, cache: str, extra=None):
        """extra: more headers, as a dict or as (name, value) pairs (several Set-Cookie headers)."""
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        for key, value in (extra.items() if isinstance(extra, dict) else extra or ()):
            self.send_header(key, value)
        if self.close_connection:
            self.send_header('Connection', 'close')
        # Log before the body goes out, so the line exists by the time the client has the answer.
        self._note(status)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send_bytes(status, dump_json(value), 'application/json; charset=utf-8', 'no-store')

    def _error(self, status: int, code: str, message: str, close: bool = False) -> None:
        if close:
            self.close_connection = True
        self._json(status, {'error': {'code': code, 'message': message}})

    def send_error(self, code, message=None, explain=None):
        """Malformed requests and unsupported methods also get JSON and the security headers."""
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = 'Error'
        self._error(code, 'http_%d' % code, message or phrase, close=True)

    # ------------------------------------------------------------ dispatch

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def _method_not_allowed(self):
        self._error(405, 'method_not_allowed', 'method not allowed', close=True)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _method_not_allowed

    def _has_body(self) -> bool:
        """True when the request declares a body: Transfer-Encoding, or a Content-Length other than 0."""
        lengths = self.headers.get_all('Content-Length') or []
        return bool(self.headers.get_all('Transfer-Encoding')) or any(v.strip() != '0' for v in lengths)

    def _dispatch(self, method: str) -> None:
        self._sent_headers = False
        if method != 'GET' or self._has_body():
            # An unread request body (a GET may declare one too) must never be parsed as the next request.
            self.close_connection = True
        try:
            if not self._host_allowed():
                return self._error(403, 'forbidden_host', 'requests must use localhost, 127.0.0.1, [::1] or an --allow-host name', close=True)
            target = self._target()
            if target is None:
                return self._error(400, 'bad_request', 'invalid request target', close=True)
            path, query = target
            if path in ('/', '/index.html'):
                return self._index() if method == 'GET' else self._method_not_allowed()
            if path.startswith('/static/'):
                return self._static(path[len('/static/'):]) if method == 'GET' else self._method_not_allowed()
            if path == '/favicon.ico':
                return self._send_bytes(204, b'', 'text/plain', 'no-store')
            if path.startswith('/api/') or path.startswith('/files/'):
                if not self._cookie_ok():
                    return self._error(403, 'forbidden', 'missing or invalid session cookie: open the viewer from its start page')
                if method != 'GET':
                    if not self._token_ok():
                        return self._error(403, 'forbidden', 'missing or invalid X-MJV-Token header', close=True)
                    if not self._origin_ok():
                        return self._error(403, 'forbidden_origin', 'cross-origin request refused', close=True)
                if path.startswith('/files/'):
                    return self._files(path, query) if method == 'GET' else self._method_not_allowed()
                return self._api(method, path, query)
            if method == 'GET' and '.' not in path.rsplit('/', 1)[-1]:
                return self._index()  # client-side routes
            return self._error(404, 'not_found', 'not found')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:
            traceback.print_exc(limit=5, file=sys.stderr)
            if self._sent_headers:
                self.close_connection = True
            else:
                self._error(500, 'internal', 'internal error; see the server log', close=True)

    # ------------------------------------------------------------- security

    def _host_allowed(self) -> bool:
        host = self.headers.get('Host')
        if not host:
            return False
        name = _host_name(host)
        return bool(name) and (name in LOCAL_HOSTS or name in self.app.allow_hosts)

    def _cookie_name(self) -> str:
        return 'mjv_%d' % self.server.port

    def _cookie_ok(self) -> bool:
        name = self._cookie_name()
        expected = self.app.token.encode()
        for header in self.headers.get_all('Cookie') or []:
            for part in header.split(';'):
                key, sep, value = part.strip().partition('=')
                if sep and key == name and hmac.compare_digest(value.strip().encode('utf-8', 'replace'), expected):
                    return True
        return False

    def _token_ok(self) -> bool:
        given = (self.headers.get('X-MJV-Token') or '').strip().encode('utf-8', 'replace')
        return hmac.compare_digest(given, self.app.token.encode())

    def _origin_ok(self) -> bool:
        origin = self.headers.get('Origin')
        if origin is None:
            return True  # non-browser client; the token header already proves the caller read the page
        parts = urllib.parse.urlsplit(origin)
        return parts.scheme == 'http' and parts.netloc.lower() == (self.headers.get('Host') or '').strip().lower()

    # ---------------------------------------------------------------- pages

    def _index(self) -> None:
        try:
            with open(os.path.join(self.app.static_dir, 'index.html'), 'r', encoding='utf-8') as handle:
                html = handle.read()
        except OSError:
            return self._error(503, 'ui_missing', 'static/index.html is missing')
        body = html.replace('{{MJV_TOKEN}}', self.app.token).replace('{{MJV_MODE}}', 'local').encode('utf-8')
        cookie = '%s=%s; HttpOnly; SameSite=Strict; Path=/' % (self._cookie_name(), self.app.token)
        self._send_bytes(200, body, 'text/html; charset=utf-8', 'no-store', {'Set-Cookie': cookie})

    def _static(self, rel: str) -> None:
        rel = urllib.parse.unquote(rel)
        if unsafe_reason(rel):
            return self._error(404, 'not_found', 'not found')
        base = self.app.static_dir
        real = os.path.realpath(os.path.join(base, *rel.split('/')))
        if not _inside(real, base) or real == base or not os.path.isfile(real):
            return self._error(404, 'not_found', 'not found')
        content_type = STATIC_TYPES.get(os.path.splitext(real)[1].lower(), 'application/octet-stream')
        with open(real, 'rb') as handle:
            body = handle.read()
        self._send_bytes(200, body, content_type, 'no-cache')

    # ------------------------------------------------------------------ api

    def _query(self, query: str) -> Dict[str, str]:
        try:
            parsed = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=64)
        except ValueError:
            raise QueryError('bad_parameter', 'too many query parameters') from None
        return {key: values[0] for key, values in parsed.items()}

    def _export_or_404(self, export_id: str) -> Optional[ExportIndex]:
        index = self.app.registry.get(export_id)
        if index is None:
            self._error(404, 'unknown_export', 'no open export with this id')
        return index

    def _api(self, method: str, path: str, query: str) -> None:
        parts = [urllib.parse.unquote(p, errors='replace') for p in path.split('/')[2:]]
        if parts and parts[-1] == '' and len(parts) > 1:
            parts = parts[:-1]
        try:
            if parts == ['health']:
                if method != 'GET':
                    return self._method_not_allowed()
                return self._json(200, {'ok': True, 'api': API_VERSION, 'version': __version__,
                                        'thumbnails': self.app.thumbs.available,
                                        'max_upload_bytes': self.app.max_upload_bytes})
            if parts == ['exports']:
                if method != 'GET':
                    return self._method_not_allowed()
                return self._json(200, {'exports': [i.summary for i in self.app.registry.all()],
                                        'library': self.app.registry.library(),
                                        'failed': self.app.registry.failed})
            if parts == ['exports', 'open']:
                return self._open() if method == 'POST' else self._method_not_allowed()
            if parts == ['exports', 'upload']:
                return self._upload() if method == 'POST' else self._method_not_allowed()
            if len(parts) >= 3 and parts[0] == 'exports':
                if method != 'GET':
                    return self._method_not_allowed()
                index = self._export_or_404(parts[1])
                if index is None:
                    return None
                rest = parts[2:]
                if rest == ['overview']:
                    return self._json(200, index.overview())
                if rest == ['jobs']:
                    return self._json(200, index.query_jobs(self._query(query)))
                if len(rest) == 2 and rest[0] == 'jobs':
                    detail = index.job_detail(rest[1])
                    return self._json(200, detail) if detail else self._error(404, 'unknown_job', 'no job with this id in the export')
                if rest == ['moodboards']:
                    return self._json(200, index.moodboards())
                if len(rest) == 2 and rest[0] == 'moodboards':
                    detail = index.moodboard_detail(rest[1])
                    return self._json(200, detail) if detail else self._error(404, 'unknown_moodboard', 'no moodboard with this id in the export')
                if rest == ['profiles']:
                    return self._json(200, index.profiles_list())
                if rest == ['likes']:
                    return self._json(200, index.likes_list())
            return self._error(404, 'not_found', 'unknown API route')
        except QueryError as exc:
            return self._error(400, exc.code, exc.message)

    def _read_body(self, limit: int) -> Optional[bytes]:
        length = self.headers.get('Content-Length')
        if length is None or not re.fullmatch(r'\s*\d{1,12}\s*', length):
            self._error(411, 'length_required', 'a Content-Length header is required', close=True)
            return None
        size = int(length)
        if size > limit:
            self._error(413, 'too_large', 'request body is too large', close=True)
            return None
        return self.rfile.read(size)

    def _open(self) -> None:
        body = self._read_body(64 * 1024)
        if body is None:
            return None
        try:
            payload = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return self._error(400, 'bad_request', 'body must be JSON: {"path": "..."}')
        path = payload.get('path') if isinstance(payload, dict) else None
        if not isinstance(path, str) or not path.strip() or '\0' in path:
            return self._error(400, 'bad_request', 'body must be JSON: {"path": "..."}')
        problems: List[dict] = []
        try:
            indexes = self.app.registry.open(path.strip(), failures=problems)
        except SourceError as exc:
            return self._error(ERROR_STATUS.get(exc.code, 400), exc.code, exc.message)
        self.app.registry.note_failed(problems)
        return self._json(200, {'exports': [i.summary for i in indexes], 'failed': problems})

    def _upload(self) -> None:
        length = self.headers.get('Content-Length')
        if length is None or not re.fullmatch(r'\s*\d{1,15}\s*', length):
            return self._error(411, 'length_required', 'a Content-Length header is required', close=True)
        size = int(length)
        if size > self.app.max_upload_bytes:
            if size <= self.app.max_upload_bytes + 64 * 1024 * 1024:
                self._drain(size)  # let the client finish sending so it can read the answer
            return self._error(413, 'too_large', 'upload exceeds the %d MB limit' % (self.app.max_upload_bytes // (1024 * 1024) or 1), close=True)
        if size == 0:
            return self._error(400, 'empty_upload', 'the upload is empty')
        uploads = os.path.join(self.app.cache_dir, 'uploads')
        name = _clean_filename(self.headers.get('X-Filename'))
        try:
            os.makedirs(uploads, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=uploads, prefix='.incoming-', suffix='.zip')
        except OSError as exc:
            return self._error(500, 'cache_unwritable', 'cannot write to the cache directory: %s' % (exc.strerror or exc))
        final = None
        created = False
        try:
            digest = hashlib.sha256()
            remaining = size
            with os.fdopen(fd, 'wb') as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        self.close_connection = True
                        return None  # client went away; nothing to answer
                    out.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            if not zipfile.is_zipfile(tmp):
                return self._error(400, 'not_a_zip', 'the upload is not a ZIP archive')
            final = os.path.join(uploads, digest.hexdigest() + '.zip')
            if os.path.exists(final):
                os.unlink(tmp)
            else:
                os.replace(tmp, final)
                created = True
            problems: List[dict] = []
            try:
                indexes = self.app.registry.open(final, source_type='upload', display=name, failures=problems)
            except SourceError as exc:
                if created and not any(getattr(i, 'source_real', None) == os.path.realpath(final) for i in self.app.registry.all()):
                    os.unlink(final)
                return self._error(ERROR_STATUS.get(exc.code, 400), exc.code, exc.message)
            self.app.registry.note_failed(problems)
            return self._json(200, {'exports': [i.summary for i in indexes], 'failed': problems})
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _drain(self, size: int) -> None:
        remaining = size
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass

    # ---------------------------------------------------------------- files

    def _files(self, path: str, query: str) -> None:
        parts = path.split('/')
        if len(parts) != 4 or not parts[2] or not parts[3]:
            return self._error(404, 'not_found', 'not found')
        index = self._export_or_404(parts[2])
        if index is None:
            return None
        rec = index.file(parts[3])
        if rec is None:
            return self._error(404, 'unknown_file', 'no file with this id in the export')
        if rec.status == 'unsupported':
            return self._error(415, 'unsupported_media', rec.reason or 'not a supported image type')
        if rec.status != 'present':
            return self._error(404, 'missing_file', rec.reason or 'file is missing')
        try:
            size = self._query(query).get('size', '')
        except QueryError as exc:
            return self._error(400, exc.code, exc.message)
        if size not in ('', 'thumb', 'original'):
            return self._error(400, 'bad_parameter', "size must be 'thumb' or 'original'")
        if size == 'thumb' and self.app.thumbs.available:
            thumb = self.app.thumbs.get(index, rec)
            if thumb:
                try:
                    with open(thumb, 'rb') as handle:
                        body = handle.read()
                except OSError:
                    body = b''
                kind = detect_image_type(body[:64])
                if kind:
                    return self._send_bytes(200, body, IMAGE_MIME[kind], self.file_cache)
        mime = IMAGE_MIME.get(rec.kind or '')
        if not mime:
            return self._error(415, 'unsupported_media', 'not a supported image type')
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(rec.stat.size))
        self.send_header('Cache-Control', self.file_cache)
        self.end_headers()
        sent = 0
        try:
            for chunk in index.view.iter_chunks(rec.rel, cap=rec.stat.size):
                self.wfile.write(chunk)
                sent += len(chunk)
        except MemberError as exc:
            self.close_connection = True
            index.mark_unreadable(rec, exc.message)  # e.g. a CRC error at its end: reported, no longer served
            self.app.log('GET /files/%s/%s stream error' % (index.id, parts[3]))
            return None
        if sent != rec.stat.size:
            self.close_connection = True
