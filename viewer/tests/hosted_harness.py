"""Start a hosted-mode viewer against a fake blob container and fake Google sign-in, for tests.

Everything is local (127.0.0.1) and invented: the secret, the domain, the accounts and
the exports (tests/fixtures.py).
"""
import http.client
import io
import os
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

from mjviewer.hosted import OAUTH_COOKIE, SESSION_COOKIE, HostedApp, load_config
from mjviewer.server import make_server

SECRET = 'TESTONLY-auth-secret-0123456789-abcdefghijkl'
DOMAIN = 'example.com'


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def set_cookies(headers) -> Dict[str, str]:
    """name -> value for every Set-Cookie header ('' when it clears the cookie)."""
    out = {}
    for header in headers.get_all('Set-Cookie') or []:
        name, _, rest = header.partition('=')
        out[name] = rest.split(';', 1)[0]
    return out


def raw_get(url: str, headers: Optional[dict] = None):
    """GET without following redirects: (status, headers, body)."""
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
    try:
        connection.request('GET', parts.path + ('?' + parts.query if parts.query else ''), headers=headers or {})
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


class Session:
    def __init__(self, cookie: str, token: str):
        self.cookie = cookie
        self.token = token

    @property
    def cookies(self) -> Dict[str, str]:
        return {SESSION_COOKIE: self.cookie}


def hosted_env(port: int, base: str, blob, google, ttl: str = '0') -> Dict[str, str]:
    """The settings of a hosted viewer on 127.0.0.1:<port> against the fakes (all invented)."""
    env = {'MJV_PUBLIC_URL': 'http://127.0.0.1:%d' % port, 'MJV_AUTH_SECRET': SECRET,
           'MJV_ALLOWED_DOMAIN': DOMAIN, 'MJV_CATALOG_URL': blob.url,
           'MJV_CACHE_DIR': os.path.join(base, 'cache'), 'MJV_CATALOG_TTL_S': ttl}
    env.update(google.env())
    return env


class Client:
    """Requests to a hosted viewer on 127.0.0.1:<port>, signing in through the fake Google."""

    def __init__(self, port: int, origin: str, google):
        self.port = port
        self.origin = origin
        self.google = google

    def request(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[dict] = None,
                cookies=None, host: Optional[str] = None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=60)
        sent = {'Host': host if host is not None else '127.0.0.1:%d' % self.port}
        if cookies:
            sent['Cookie'] = cookies if isinstance(cookies, str) else '; '.join('%s=%s' % kv for kv in cookies.items())
        sent.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def get(self, path: str, session: Optional[Session] = None, **kwargs):
        return self.request('GET', path, cookies=session.cookies if session else None, **kwargs)

    def post(self, path: str, session: Session, headers: Optional[dict] = None, body: bytes = b'{}'):
        sent = {'X-MJV-Token': session.token, 'Origin': self.origin, 'Content-Type': 'application/json'}
        sent.update(headers or {})
        return self.request('POST', path, body=body, headers=sent, cookies=session.cookies)

    def sign_in(self, next_path: str = '/'):
        """Run the whole OAuth flow; returns the callback's (status, headers, body)."""
        status, headers, _ = self.request('GET', '/auth/login?next=' + urllib.parse.quote(next_path, safe=''))
        assert status == 302, status
        oauth = set_cookies(headers)[OAUTH_COOKIE]
        status, answer, _ = raw_get(headers['Location'])
        assert status == 302, status
        callback = urllib.parse.urlsplit(answer['Location'])
        assert '%s://%s' % (callback.scheme, callback.netloc) == self.origin and callback.path == '/auth/callback'
        return self.request('GET', '/auth/callback?' + callback.query, cookies={OAUTH_COOKIE: oauth})

    def session(self, email: Optional[str] = None) -> Session:
        if email:
            self.google.email = email
        status, headers, body = self.sign_in()
        assert status == 302, body
        cookie = set_cookies(headers)[SESSION_COOKIE]
        status, _, page = self.request('GET', '/', cookies={SESSION_COOKIE: cookie})
        assert status == 200, status
        token = re.search(rb'name="mjv-token" content="([^"]+)"', page).group(1).decode()
        return Session(cookie, token)


class Hosted(Client):
    """A hosted viewer on 127.0.0.1 with MJV_PUBLIC_URL = http://127.0.0.1:<port>."""

    def __init__(self, base: str, blob, google, env: Optional[dict] = None, ttl: str = '0'):
        port = free_port()
        self.blob = blob
        self.env = hosted_env(port, base, blob, google, ttl)
        self.env.update(env or {})
        self.config = load_config(self.env)
        super().__init__(port, self.config.public_url, google)
        self.log = io.StringIO()
        self.app = HostedApp(self.config, log=self.log)
        self.server = make_server(self.app, '127.0.0.1', self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()


HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'te', 'trailer', 'upgrade'}


class _FrontEndHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):  # noqa: A002 - quiet
        pass

    def _forward(self) -> None:
        front = self.server.front
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None
        port = front.pick(self.command, self.path)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_BY_HOP and key.lower() != 'content-length':
                    self.send_header(key, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            connection.close()

    do_GET = do_POST = do_HEAD = _forward


class RoundRobin:
    """A front end on 127.0.0.1 that sends each request to the next instance in turn, with no affinity and the
    Host header kept, the way a strict load balancer treats a single browser. Records (method, path, instance)."""

    def __init__(self):
        self.ports: List[int] = []
        self.requests: List[Tuple[str, str, int]] = []
        self._next = 0
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _FrontEndHandler)
        self.server.daemon_threads = True
        self.server.front = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return 'http://127.0.0.1:%d' % self.server.server_address[1]

    def pick(self, method: str, path: str) -> int:
        with self._lock:
            n = self._next % len(self.ports)
            self._next += 1
            self.requests.append((method, path, n))
            return self.ports[n]

    def count(self, method: str, prefix: str) -> int:
        with self._lock:
            return sum(1 for m, p, _ in self.requests if m == method and p.startswith(prefix))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
