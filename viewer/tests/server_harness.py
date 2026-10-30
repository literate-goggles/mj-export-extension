"""Start a real viewer server on an ephemeral port for HTTP tests."""
import http.client
import io
import json
import os
import tempfile
import threading
import unittest

from mjviewer.server import ViewerApp, make_server

INDEX_HTML = ('<!doctype html><html><head><meta charset="utf-8">'
              '<meta name="mjv-token" content="{{MJV_TOKEN}}">'
              '<script src="/static/app.js"></script></head><body>viewer</body></html>')


class LiveServer:
    def __init__(self, base: str, **app_kwargs):
        self.static = os.path.join(base, 'static')
        os.makedirs(self.static, exist_ok=True)
        with open(os.path.join(self.static, 'index.html'), 'w') as handle:
            handle.write(INDEX_HTML)
        with open(os.path.join(self.static, 'app.js'), 'w') as handle:
            handle.write('console.log("synthetic");\n')
        app_kwargs.setdefault('cache_dir', os.path.join(base, 'cache'))
        self.log = io.StringIO()
        self.app = ViewerApp(static_dir=self.static, log=self.log, **app_kwargs)
        self.server = make_server(self.app, '127.0.0.1', 0)
        self.port = self.server.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = '%s=%s' % ('mjv_%d' % self.port, self.app.token)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()

    def request(self, method, path, body=None, headers=None, host=None, cookie=True, token=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        sent = {'Host': host if host is not None else '127.0.0.1:%d' % self.port}
        if cookie:
            sent['Cookie'] = self.cookie if cookie is True else cookie
        if token:
            sent['X-MJV-Token'] = self.app.token if token is True else token
        sent.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            data = response.read()
            return response.status, response.headers, data
        finally:
            connection.close()

    def get_json(self, path, **kwargs):
        status, headers, data = self.request('GET', path, **kwargs)
        return status, json.loads(data)

    def post_json(self, path, payload, headers=None, **kwargs):
        kwargs.setdefault('token', True)
        body = json.dumps(payload).encode()
        sent = {'Content-Type': 'application/json'}
        sent.update(headers or {})
        status, headers, data = self.request('POST', path, body=body, headers=sent, **kwargs)
        return status, json.loads(data)


class ServerTestCase(unittest.TestCase):
    app_kwargs: dict = {}

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-http-')
        cls.base = cls._tmp.name
        cls.data = os.path.join(cls.base, 'data')
        os.makedirs(cls.data)
        cls.live = LiveServer(cls.base, **cls.app_kwargs)

    @classmethod
    def tearDownClass(cls):
        cls.live.stop()
        cls._tmp.cleanup()
