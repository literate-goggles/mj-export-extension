"""Hosted mode: configuration, signing, Google sign-in, sessions, routes, Host/Origin/CSRF.

Everything runs locally against tests/fake_google.py and tests/fake_blob.py with
invented accounts, secrets and exports.
"""
import base64
import contextlib
import http.client
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

from mjviewer.__main__ import main
from mjviewer.hosted import (OAUTH_COOKIE, SESSION_COOKIE, ConfigError, HostedConfig, Secret, check_claims,
                             cookie_values, csrf_token, hosted_cache_dir, issue_session, load_config,
                             read_session, run, safe_next, sign, unsign)
from mjviewer.model import hidden_closure
from mjviewer.server import CSP
from tests import fixtures as fx
from tests.fake_blob import FakeBlob
from tests.fake_google import REMOVE, FakeGoogle
from tests.hosted_harness import DOMAIN, SECRET, Client, Hosted, free_port, hosted_env, raw_get, set_cookies

VIEWER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def raw_post(hosted, path: str, session, length: str, body: bytes):
    """A POST sent as raw bytes with this Content-Length header, whatever it says: (status, error code)."""
    head = ('POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nCookie: %s=%s\r\nX-MJV-Token: %s\r\nOrigin: %s\r\n'
            'Content-Type: application/json\r\nContent-Length: %s\r\n\r\n'
            % (path, hosted.port, SESSION_COOKIE, session.cookie, session.token, hosted.origin, length))
    with socket.create_connection(('127.0.0.1', hosted.port), timeout=10) as sock:
        sock.sendall(head.encode('utf-8') + body)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status, json.loads(response.read())['error']['code']


def smuggle(port: int, host: str, path: str, cookie: str = ''):
    """Send a GET whose Content-Length body holds a second request, in one write.
    Returns (number of responses, whether the server closed the connection)."""
    extra = 'Cookie: %s\r\n' % cookie if cookie else ''
    inner = 'GET /favicon.ico HTTP/1.1\r\nHost: %s\r\n\r\n' % host
    outer = 'GET %s HTTP/1.1\r\nHost: %s\r\n%sContent-Length: %d\r\n\r\n%s' % (path, host, extra, len(inner), inner)
    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
        sock.sendall(outer.encode())
        sock.settimeout(3)
        data, closed = b'', False
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    closed = True
                    break
                data += chunk
        except socket.timeout:
            pass
    return data.count(b'HTTP/1.1 '), closed

BASE_ENV = {
    'MJV_PUBLIC_URL': 'https://viewer.example.com',
    'MJV_AUTH_SECRET': SECRET,
    'MJV_ALLOWED_DOMAIN': DOMAIN,
    'GOOGLE_CLIENT_ID': 'test-client.apps.example.com',
    'GOOGLE_CLIENT_SECRET': 'TESTONLY-client-secret-0123',
    'MJV_CATALOG_URL': 'https://storage.example.com/exports?sv=2099-01-01&sp=rl&sig=TESTONLYsignature0123456789%3D',
    'MJV_CACHE_DIR': '/tmp/mjv-test-unused-cache',
}
SIG = 'TESTONLYsignature0123456789'
WRITE_SIG = 'TESTONLYwritesignature9876543210'


def env(**changes):
    out = dict(BASE_ENV)
    for key, value in changes.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def problems(environ) -> list:
    try:
        load_config(environ)
    except ConfigError as exc:
        return exc.problems
    raise AssertionError('ConfigError not raised')


class ConfigTests(unittest.TestCase):
    def test_complete_environment_loads_with_defaults(self):
        config = load_config(env())
        self.assertEqual(config.public_url, 'https://viewer.example.com')
        self.assertEqual(config.hosts, frozenset({'viewer.example.com', 'viewer.example.com:443'}))
        self.assertEqual(config.redirect_uri, 'https://viewer.example.com/auth/callback')
        self.assertEqual((config.port, config.session_days, config.catalog_ttl_s), (8000, 30, 60))
        self.assertEqual(config.cache_max_bytes, 50 * 1024 ** 3)
        self.assertEqual(config.commit, 'unknown')
        self.assertIsNone(config.soft_delete_days)  # the dialog then leaves it to the storage's own settings
        self.assertTrue(config.google_token_url.startswith('https://oauth2.googleapis.com/'))
        # Secrets never show in repr(), and so never in a traceback's message or a log line.
        text = repr(config) + str(config)
        for secret in (SECRET, 'TESTONLY-client-secret-0123', SIG):
            self.assertNotIn(secret, text)

    def test_public_url_forms(self):
        config = load_config(env(MJV_PUBLIC_URL='https://Viewer.Example.com:8443/'))
        self.assertEqual(config.public_url, 'https://viewer.example.com:8443')
        self.assertEqual(config.hosts, frozenset({'viewer.example.com:8443'}))
        config = load_config(env(MJV_PUBLIC_URL='http://localhost:8830'))
        self.assertEqual((config.public_url, config.hosts), ('http://localhost:8830', frozenset({'localhost:8830'})))
        for bad in ('http://viewer.example.com', 'ftp://viewer.example.com', 'https://viewer.example.com/app',
                    'https://user:pw@viewer.example.com', 'https://viewer.example.com?x=1', 'https://:443',
                    'https://viewer.example.com:99999', 'viewer.example.com'):
            with self.subTest(url=bad):
                self.assertTrue(any(p.startswith('MJV_PUBLIC_URL') for p in problems(env(MJV_PUBLIC_URL=bad))))

    def test_every_missing_setting_is_listed(self):
        found = problems({})
        for name in ('MJV_PUBLIC_URL', 'MJV_AUTH_SECRET', 'MJV_ALLOWED_DOMAIN', 'GOOGLE_CLIENT_ID',
                     'GOOGLE_CLIENT_SECRET', 'MJV_CATALOG_URL'):
            self.assertIn('%s is not set' % name, found)
        self.assertEqual(problems(env(GOOGLE_CLIENT_SECRET='  ')), ['GOOGLE_CLIENT_SECRET is not set'])

    def test_weak_or_invalid_values_are_refused(self):
        cases = {
            'MJV_AUTH_SECRET': ['short', 'a' * 64, 'abababababababababababababababab12'],
            'MJV_ALLOWED_DOMAIN': ['example', 'exa mple.com', '@example.com', '-x.example.com'],
            'GOOGLE_CLIENT_ID': ['id with spaces'],
            'PORT': ['abc', '0', '70000'],
            'MJV_CACHE_MAX_GB': ['0', '-1', 'lots'],
            'MJV_SESSION_DAYS': ['0', '999', 'x'],
            'MJV_CATALOG_TTL_S': ['-1', 'soon'],
            'MJV_SOFT_DELETE_DAYS': ['0', '366', '-7', '7.5', '1e1', 'week'],
            'MJV_GOOGLE_TOKEN_URL': ['https://oauth2.example.com/token', 'http://10.0.0.1/token'],
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    found = problems(env(**{name: value}))
                    self.assertTrue(any(p.startswith(name) for p in found), found)
                    self.assertFalse(any(value in p for p in found if len(value) > 6 and name.endswith('SECRET')))

    def test_catalog_url_must_be_https_read_only_and_is_never_quoted(self):
        query = '?sv=2099-01-01&sp=rl&sig=' + SIG
        for bad, expect in [('http://storage.example.com/c' + query, 'https'),
                            ('https://storage.example.com/c', 'query string'),
                            ('https://storage.example.com/c?sv=1&sp=rwdl&sig=' + SIG, 'read-only'),
                            ('https://storage.example.com/c?sp=racwdl&sig=' + SIG, 'read-only'),
                            ('https://storage.example.com/c' + query + '#frag', 'fragment'),
                            ('https://storage.example.com/c?sig=a b', 'spaces')]:
            with self.subTest(url=bad):
                found = problems(env(MJV_CATALOG_URL=bad))
                self.assertTrue(any(expect in p for p in found), found)
                self.assertFalse(any(SIG in p or 'storage.example.com' in p for p in found), found)
        self.assertEqual(load_config(env(MJV_CATALOG_URL='http://127.0.0.1:9/c?sp=r&sig=x')).catalog_ttl_s, 60)

    def test_write_url_names_the_same_container_and_may_only_write_and_delete(self):
        container = 'https://storage.example.com/exports'
        self.assertIsNone(load_config(env()).catalog_write_url)  # deleting is off unless it is set
        for good in (container + '?sv=2099-01-01&sp=rwdl&sig=' + WRITE_SIG, container + '/?sp=dw&sig=' + WRITE_SIG,
                     'https://STORAGE.example.com:443/exports?sp=wd&sig=' + WRITE_SIG):
            with self.subTest(url=good[-30:]):
                config = load_config(env(MJV_CATALOG_WRITE_URL=good))
                self.assertEqual(config.catalog_write_url.reveal(), good)
                self.assertNotIn(WRITE_SIG, repr(config) + str(config))
        for bad, expect in [('https://other.example.com/exports?sp=rwdl&sig=' + WRITE_SIG, 'same container'),
                            (container + '-two?sp=rwdl&sig=' + WRITE_SIG, 'same container'),
                            ('https://storage.example.com:8443/exports?sp=rwdl&sig=' + WRITE_SIG, 'same container'),
                            ('http://storage.example.com/exports?sp=rwdl&sig=' + WRITE_SIG, 'https'),
                            (container + '?sig=' + WRITE_SIG, 'w and d'),
                            (container + '?sp=rwl&sig=' + WRITE_SIG, 'w and d'),
                            (container + '?sp=rdl&sig=' + WRITE_SIG, 'w and d'),
                            (container + '?sp=racwdl&sig=' + WRITE_SIG, 'nothing but r, w, d and l'),
                            (container + '?sp=rwdlx&sig=' + WRITE_SIG, 'nothing but r, w, d and l'),
                            (container + '?sp=rwdl&sp=rwdl&sig=' + WRITE_SIG, 'w and d'),
                            (container, 'w and d'),
                            (container + '?sp=rwdl&sig=a b', 'spaces'),
                            (container + '?sp=rwdl&sig=' + WRITE_SIG + '#frag', 'fragment'),
                            ('https://user:pw@storage.example.com/exports?sp=rwdl&sig=' + WRITE_SIG, 'user name')]:
            with self.subTest(url=bad[-30:]):
                found = problems(env(MJV_CATALOG_WRITE_URL=bad))
                self.assertTrue(any(p.startswith('MJV_CATALOG_WRITE_URL') and expect in p for p in found), found)
                for secret in (WRITE_SIG, SIG, 'storage.example.com', 'pw'):
                    self.assertFalse(any(secret in p for p in found), found)
        local = load_config(env(MJV_CATALOG_URL='http://127.0.0.1:9/c?sp=rl&sig=x',
                                MJV_CATALOG_WRITE_URL='http://127.0.0.1:9/c?sp=rwdl&sig=y'))
        self.assertIsNotNone(local.catalog_write_url)  # tests on this machine may use http, like MJV_CATALOG_URL

    def test_admin_emails(self):
        config = load_config(env(MJV_ADMIN_EMAILS=' Carol@Example.com, dave@example.com\terin@example.com,,'))
        self.assertEqual(config.admin_emails, frozenset({'carol@example.com', 'dave@example.com', 'erin@example.com'}))
        for empty in (None, '', ' , '):
            with self.subTest(value=empty):
                self.assertEqual(load_config(env(MJV_ADMIN_EMAILS=empty)).admin_emails, frozenset())
        found = problems(env(MJV_ADMIN_EMAILS='carol@example.com, mallory@other.example not-an-address,'
                                              'a@b@example.com x@sub.example.com'))
        self.assertEqual(found, ['MJV_ADMIN_EMAILS: entry %d is not an email address at example.com' % n
                                 for n in (2, 3, 4, 5)])
        self.assertFalse(any('mallory' in p or 'not-an-address' in p for p in found))

    def test_numbers_commit_and_test_endpoints(self):
        config = load_config(env(PORT='8080', MJV_CACHE_MAX_GB='0.5', MJV_SESSION_DAYS='7', MJV_CATALOG_TTL_S='0',
                                 MJV_SOFT_DELETE_DAYS=' 14 ', MJV_COMMIT='abc1234',
                                 MJV_GOOGLE_AUTH_URL='http://127.0.0.1:9/auth',
                                 MJV_GOOGLE_TOKEN_URL='http://localhost:9/token',
                                 MJV_GOOGLE_TOKENINFO_URL='https://127.0.0.1:9/info'))
        self.assertEqual((config.port, config.session_days, config.catalog_ttl_s, config.soft_delete_days), (8080, 7, 0, 14))
        self.assertEqual([load_config(env(MJV_SOFT_DELETE_DAYS=days)).soft_delete_days for days in ('1', '365', '')],
                         [1, 365, None])
        self.assertEqual(config.cache_max_bytes, 512 * 1024 ** 2)
        self.assertEqual(config.commit, 'abc1234')
        self.assertEqual(config.google_auth_url, 'http://127.0.0.1:9/auth')
        self.assertEqual(load_config(env(MJV_COMMIT='<script>')).commit, 'unknown')

    def test_cache_dir_defaults_to_a_writable_shared_folder_else_falls_back(self):
        with tempfile.TemporaryDirectory() as base:
            path, warning = hosted_cache_dir(base)
            self.assertEqual((path, warning), (os.path.join(base, 'mjv-cache'), ''))
            self.assertTrue(os.path.isdir(path))
            self.assertEqual(os.listdir(path), [])  # the write test leaves nothing behind
            config = load_config(env(MJV_CACHE_DIR=None), cache_base=base)
            self.assertEqual(config.cache_dir, os.path.join(base, 'mjv-cache'))
        if os.geteuid() == 0:
            self.skipTest('root can write anywhere')
        with tempfile.TemporaryDirectory() as base:
            locked = os.path.join(base, 'home')
            os.mkdir(locked)
            os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
            try:
                path, warning = hosted_cache_dir(locked)
            finally:
                os.chmod(locked, stat.S_IRWXU)
            self.assertTrue(path.endswith(os.path.join('', 'mj-export-viewer')))
            self.assertIn('not writable', warning)

    def test_refuses_to_start_with_a_clear_message_and_no_secrets(self):
        out, err = io.StringIO(), io.StringIO()
        status = run(env=env(MJV_ALLOWED_DOMAIN=None, MJV_AUTH_SECRET='short-secret-TESTONLY'), out=out, err=err)
        self.assertEqual(status, 2)
        text = err.getvalue()
        self.assertIn('Hosted mode cannot start', text)
        self.assertIn('MJV_ALLOWED_DOMAIN is not set', text)
        self.assertIn('MJV_AUTH_SECRET is too weak', text)
        for secret in ('short-secret-TESTONLY', 'TESTONLY-client-secret-0123', SIG):
            self.assertNotIn(secret, text)
        self.assertEqual(out.getvalue(), '')

    def test_command_line_refuses_local_options_in_hosted_mode(self):
        for argv in (['--hosted', 'export.zip'], ['--hosted', '--library', '.'], ['--hosted', '--port', '1'],
                     ['--hosted', '--allow-host', 'x'], ['--hosted', '--cache-dir', '/tmp/x'], ['--hosted', '--open']):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(main(argv), 2)
                self.assertIn('--hosted takes its settings from the environment', err.getvalue())


class SigningTests(unittest.TestCase):
    config = load_config(env())

    def test_sign_and_verify(self):
        now = 1_800_000_000
        key = Secret(SECRET)
        token = sign(key, 'session', {'email': 'a@example.com', 'exp': now + 60})
        self.assertEqual(unsign(key, 'session', token, now)['email'], 'a@example.com')
        body, mac = token.split('.')
        other_body = sign(key, 'session', {'email': 'b@example.com', 'exp': now + 60}).split('.')[0]
        flipped = mac[:-1] + ('A' if mac[-1] != 'A' else 'B')
        for bad in (body + '.' + flipped, other_body + '.' + mac, body, '', token + '.x', '.' + mac, None, 42,
                    'a' * 5000 + '.' + mac, body + '.' + mac + '='):
            with self.subTest(token=str(bad)[:40]):
                self.assertIsNone(unsign(key, 'session', bad, now))
        self.assertIsNone(unsign(Secret(SECRET + 'x'), 'session', token, now))  # another secret
        self.assertIsNone(unsign(key, 'oauth', token, now))                     # another purpose
        self.assertIsNone(unsign(key, 'session', token, now + 60))              # expired
        for payload in ({'email': 'a@example.com'}, {'exp': True}, {'exp': 'later'}):
            with self.subTest(payload=payload):
                self.assertIsNone(unsign(key, 'session', sign(key, 'session', payload), now))

    def test_sessions(self):
        now = time.time()
        cookie = issue_session(self.config, 'Alice@Example.com', now)
        self.assertEqual(read_session(self.config, cookie, now + 10), 'alice@example.com')
        self.assertIsNone(read_session(self.config, cookie, now + 31 * 86400))
        foreign = issue_session(self.config, 'alice@other.example', now)
        self.assertIsNone(read_session(self.config, foreign, now))
        future = sign(self.config.auth_secret, 'session', {'email': 'a@example.com', 'iat': int(now) + 3600,
                                                           'exp': int(now) + 7200})
        self.assertIsNone(read_session(self.config, future, now))
        # Shortening MJV_SESSION_DAYS ends older sessions at once.
        shorter = HostedConfig(**dict(self.config.__dict__, session_days=1))
        self.assertIsNone(read_session(shorter, cookie, now + 2 * 86400))
        other_secret = HostedConfig(**dict(self.config.__dict__, auth_secret=Secret(SECRET[::-1])))
        self.assertIsNone(read_session(other_secret, cookie, now))
        self.assertNotEqual(csrf_token(self.config, cookie), csrf_token(self.config, issue_session(
            self.config, 'bob@example.com', now)))
        self.assertEqual(csrf_token(self.config, cookie), csrf_token(self.config, cookie))

    def test_safe_next(self):
        self.assertEqual(safe_next('/a\\b'), '/')  # no backslash anywhere, not only after the first slash
        for good in ('/', '/#/x/abc/gallery?q=red%20fox&kind=grid', '/index.html', '/x/y?z=1', '/%0d%0aSet-Cookie:x',
                     '/%2F%2Fevil.example'):
            self.assertEqual(safe_next(good), good)
        for bad in ('//evil.example', '/\\evil.example', '/\\/evil.example', 'https://evil.example', 'evil.example',
                    '/\t/evil.example', '/ /evil', '/\r\nX: y', '/\u00e9', '', None, 3, '/' + 'a' * 2000,
                    'javascript:alert(1)'):
            with self.subTest(next=bad):
                self.assertEqual(safe_next(bad), '/')

    def test_claims(self):
        now = time.time()
        good = {'aud': self.config.client_id, 'iss': 'https://accounts.google.com', 'email_verified': 'true',
                'hd': DOMAIN, 'email': 'Carol@example.com', 'exp': str(int(now) + 600)}
        self.assertEqual(check_claims(good, self.config, now), ('carol@example.com', ''))
        self.assertEqual(check_claims(dict(good, iss='accounts.google.com', email_verified=True, exp=int(now) + 9),
                                      self.config, now)[0], 'carol@example.com')
        for change, reason in [({'aud': 'other-client'}, 'another application'),
                               ({'iss': 'https://evil.example'}, 'not issued by Google'),
                               ({'email_verified': 'false'}, 'not verified'), ({'email_verified': None}, 'not verified'),
                               ({'hd': 'other.example'}, 'not part of'), ({'hd': None}, 'not part of'),
                               ({'email': 'carol@other.example'}, 'not at example.com'),
                               ({'email': 'carol@sub.example.com'}, 'not at example.com'),
                               ({'email': 'a@b@example.com'}, 'not at example.com'),
                               ({'exp': str(int(now) - 1)}, 'expired'), ({'exp': 'soon'}, 'expired')]:
            with self.subTest(change=change):
                email, why = check_claims(dict(good, **change), self.config, now)
                self.assertIsNone(email)
                self.assertIn(reason, why)

    def test_cookie_values_finds_duplicates_across_headers(self):
        class Headers:
            def get_all(self, name):
                return ['a=1; %s=x; b=2' % SESSION_COOKIE, '%s=y' % SESSION_COOKIE]
        self.assertEqual(cookie_values(Headers(), SESSION_COOKIE), ['x', 'y'])


class HostedTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-hosted-')
        cls.base = cls._tmp.name
        cls.blob = FakeBlob(os.path.join(cls.base, 'blob'))
        cls.google = FakeGoogle()
        cls.entry = cls.blob.put_export(fx.export_zip_bytes(fx.standard_export()), name='mj_export_synthetic.zip',
                                        label='Synthetic team export')
        cls.blob.write_catalog([cls.entry])
        cls.hosted = Hosted(cls.base, cls.blob, cls.google)

    @classmethod
    def tearDownClass(cls):
        cls.hosted.stop()
        cls.blob.stop()
        cls.google.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.google.reset()
        self.google.email, self.google.hd = 'alice@example.com', DOMAIN

    def json_of(self, body: bytes):
        return json.loads(body.decode('utf-8'))

    def wait_ready(self, session, catalog_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self.json_of(self.hosted.get('/api/catalog/%s' % catalog_id, session)[2])
            if item['state'] in ('ready', 'failed'):
                return item
            time.sleep(0.05)
        raise AssertionError('export did not become ready')


class ProbeAndHeaderTests(HostedTestCase):
    def assert_healthz_names_nothing(self, catalog_ok: bool):
        for host in ('127.0.0.1:%d' % self.hosted.port, 'internal-probe', ''):
            status, headers, body = self.hosted.request('GET', '/healthz', host=host)
            self.assertEqual(status, 200)
            data = self.json_of(body)
            self.assertEqual(set(data), {'ok', 'mode', 'commit', 'features', 'catalog', 'cache'})
            self.assertEqual((data['ok'], data['mode'], data['cache']), (True, 'hosted', {'ok': True}))
            self.assertEqual(data['features'], ['hide'])  # also with hiding off: this version applies hidden lists
            self.assertEqual(set(data['catalog']), {'exports', 'ok', 'age_s', 'delete_enabled', 'zip_deletes_pending',
                                                    'soft_delete_days'})
            self.assertEqual((data['catalog']['exports'], data['catalog']['ok']), (1, catalog_ok))
            self.assertIs(data['catalog']['delete_enabled'], False)
            self.assertEqual(data['catalog']['zip_deletes_pending'], 0)
            self.assertIsNone(data['catalog']['soft_delete_days'])
            for leak in (self.entry['id'], self.entry['sha256'], self.entry['name'], self.entry['label'], 'Synthetic',
                         self.entry['published_by'], 'publisher', self.hosted.config.cache_dir, 'alice', '@',
                         self.blob.secret, '127.0.0.1', 'HTTP 403', 'storage'):
                self.assertNotIn(leak.encode(), body)

    def test_healthz_needs_no_session_or_host_and_names_nothing(self):
        session = self.hosted.session()
        listing = self.json_of(self.hosted.get('/api/exports', session)[2])  # a loaded catalog
        self.assertEqual(len(listing['catalog']['items']), 1)
        self.assert_healthz_names_nothing(catalog_ok=True)
        self.blob.faults['catalog.json'] = '403'  # and while the catalog cannot be read
        try:
            self.assertFalse(self.json_of(self.hosted.get('/api/exports', session)[2])['catalog']['ok'])
            self.assert_healthz_names_nothing(catalog_ok=False)
        finally:
            self.blob.faults.clear()
        self.assertEqual(self.hosted.request('POST', '/healthz')[0], 405)

    def test_a_get_with_a_body_closes_the_connection(self):
        """A body on a GET is never read as the next request (a shared front-end connection could
        otherwise hand one client's answer to another)."""
        session = self.hosted.session()
        host = '127.0.0.1:%d' % self.hosted.port
        cookie = '%s=%s' % (SESSION_COOKIE, session.cookie)
        for path, cookies in (('/healthz', ''), ('/robots933456.txt', ''), ('/auth/login', ''),
                              ('/static/app.js', ''), ('/', cookie), ('/api/exports', cookie)):
            with self.subTest(path=path):
                self.assertEqual(smuggle(self.hosted.port, host, path, cookies), (1, True))
        # A GET without a body keeps the connection for the next request.
        with socket.create_connection(('127.0.0.1', self.hosted.port), timeout=5) as sock:
            sock.sendall(('GET /healthz HTTP/1.1\r\nHost: %s\r\nContent-Length: 0\r\n\r\n' % host).encode() * 2)
            sock.settimeout(2)
            data = b''
            try:
                while data.count(b'HTTP/1.1 200') < 2:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
        self.assertEqual(data.count(b'HTTP/1.1 200'), 2)

    def test_start_probe(self):
        status, headers, body = self.hosted.request('GET', '/robots933456.txt', host='169.254.0.1:8000')
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/plain'))
        self.assertEqual(body, b'User-agent: *\nDisallow: /\n')

    def test_foreign_host_is_refused_everywhere_else(self):
        for path in ('/', '/auth/login', '/auth/callback', '/auth/logout', '/static/app.js', '/api/health'):
            for host in ('evil.example', 'localhost:%d' % self.hosted.port, '127.0.0.1', ''):
                with self.subTest(path=path, host=host):
                    status, _, body = self.hosted.request('GET', path, host=host)
                    self.assertEqual(status, 403)
                    self.assertEqual(self.json_of(body)['error']['code'], 'forbidden_host')

    def test_security_headers_include_hsts(self):
        session = self.hosted.session()
        for path, session_ in (('/', session), ('/static/app.css', None), ('/api/health', session),
                               ('/api/health', None), ('/auth/logout', None), ('/healthz', None)):
            with self.subTest(path=path):
                _, headers, _ = self.hosted.get(path, session_)
                self.assertEqual(headers['Content-Security-Policy'], CSP)
                self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000; includeSubDomains')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(headers['Referrer-Policy'], 'no-referrer')


class UnauthenticatedTests(HostedTestCase):
    ROUTES = ['/', '/index.html', '/x/abc/gallery', '/api/health', '/api/exports', '/api/exports/open',
              '/api/exports/upload', '/api/catalog/{cid}', '/api/catalog/{cid}/open', '/api/catalog/{cid}/delete',
              '/api/catalog/{cid}/hide', '/api/catalog/{cid}/unhide', '/api/exports/{cid}/hidden',
              '/api/exports/{cid}/overview',
              '/api/exports/{cid}/jobs', '/api/exports/{cid}/jobs/job-grid-a', '/api/exports/{cid}/moodboards',
              '/api/exports/{cid}/moodboards/board-1', '/api/exports/{cid}/profiles', '/api/exports/{cid}/likes',
              '/files/{cid}/0123456789abcdef0123', '/files/{cid}/0123456789abcdef0123?size=thumb', '/missing.txt',
              '/api/nope']

    def assert_nothing_leaks(self, body: bytes):
        for leak in (self.entry['id'], self.entry['name'], 'Synthetic', 'synthetic', 'fox', self.blob.secret,
                     self.hosted.config.cache_dir):
            self.assertNotIn(leak.encode(), body)

    def test_every_route_needs_a_session(self):
        session = self.hosted.session()  # open the export, so that there is something that could leak
        self.hosted.post('/api/catalog/%s/open' % self.entry['id'], session)
        self.wait_ready(session, self.entry['id'])
        bad_cookies = [None, {SESSION_COOKIE: 'garbage'}, {SESSION_COOKIE: session.cookie + 'x'},
                       {'mjv_session': session.cookie}, {'__Secure-mjv_session': session.cookie},
                       '%s=%s; %s=%s' % (SESSION_COOKIE, session.cookie, SESSION_COOKIE, session.cookie)]
        pages = {'/', '/index.html', '/x/abc/gallery'}
        for route in self.ROUTES:
            path = route.format(cid=self.entry['id'])
            for cookies in bad_cookies:
                for method in ('GET', 'POST'):
                    with self.subTest(path=path, method=method, cookies=str(cookies)[:30]):
                        status, headers, body = self.hosted.request(
                            method, path, cookies=cookies, body=b'{}' if method == 'POST' else None,
                            headers={'X-MJV-Token': session.token, 'Origin': self.hosted.origin})
                        duplicate = isinstance(cookies, str)
                        if method == 'GET' and path in pages and not duplicate:
                            self.assertEqual(status, 302)
                            location = headers['Location']
                            self.assertTrue(location.startswith('/auth/login?next='), location)
                            self.assertEqual(urllib.parse.unquote(location.split('=', 1)[1]), path)
                        else:
                            self.assertEqual(status, 401, body)
                            code = self.json_of(body)['error']['code']
                            self.assertEqual(code, 'duplicate_session' if duplicate else 'unauthenticated')
                        self.assertNotIn('Set-Cookie', headers)
                        self.assert_nothing_leaks(body)

    def test_a_browser_without_a_session_signs_in_from_a_page_that_keeps_the_view(self):
        for path in ('/', '/x/%s/moodboards' % self.entry['id'], '/x/abc/gallery?q=red%20fox&kind=grid'):
            with self.subTest(path=path):
                login = '/auth/login?next=' + urllib.parse.quote(path, safe='')
                status, headers, body = self.hosted.request('GET', path, headers={
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith('text/html'))
                self.assertIn(b'<script src="/static/signin.js"></script>', body)
                link = ('href="%s"' % login.replace('&', '&amp;')).encode()
                self.assertIn(link, body)  # for a browser without JavaScript
                self.assertNotIn('Set-Cookie', headers)
                self.assertEqual((headers['Cache-Control'], headers['Content-Security-Policy']), ('no-store', CSP))
                self.assert_nothing_leaks(body.replace(link, b''))  # only the address it was asked for
                # Other clients keep the plain redirect, query included.
                status, headers, _ = self.hosted.request('GET', path)
                self.assertEqual((status, headers['Location']), (302, login))
        status, headers, script = self.hosted.request('GET', '/static/signin.js')
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/javascript'))
        self.assertIn(b'location.hash', script)

    def test_static_assets_need_no_session(self):
        for path in ('/static/app.js', '/static/app.css', '/static/theme.js'):
            status, _, body = self.hosted.request('GET', path)
            self.assertEqual(status, 200)
            self.assert_nothing_leaks(body)
        self.assertEqual(self.hosted.request('GET', '/favicon.ico')[0], 204)
        self.assertEqual(self.hosted.request('GET', '/static/../hosted.py')[0], 404)


class SignInTests(HostedTestCase):
    def login(self, next_path='/'):
        status, headers, _ = self.hosted.request('GET', '/auth/login?next=' + urllib.parse.quote(next_path, safe=''))
        self.assertEqual(status, 302)
        return headers

    def test_login_redirects_to_google_with_a_bound_state(self):
        headers = self.login('/#/x/abc/gallery')
        location = urllib.parse.urlsplit(headers['Location'])
        self.assertEqual('%s://%s%s' % (location.scheme, location.netloc, location.path),
                         self.hosted.env['MJV_GOOGLE_AUTH_URL'])
        params = dict(urllib.parse.parse_qsl(location.query))
        self.assertEqual(params['response_type'], 'code')
        self.assertEqual(params['scope'], 'openid email profile')
        self.assertEqual(params['hd'], DOMAIN)
        self.assertEqual(params['prompt'], 'select_account')
        self.assertEqual(params['redirect_uri'], self.hosted.origin + '/auth/callback')
        self.assertEqual(params['client_id'], self.google.client_id)
        self.assertEqual(params['code_challenge_method'], 'S256')
        self.assertGreaterEqual(len(params['state']), 32)
        self.assertNotIn('client_secret', params)
        cookie = [c for c in headers.get_all('Set-Cookie') if c.startswith(OAUTH_COOKIE + '=')][0]
        for flag in ('Max-Age=600', 'Path=/', 'Secure', 'HttpOnly', 'SameSite=Lax'):
            self.assertIn(flag, cookie)
        self.assertNotIn('Domain', cookie)
        self.assertNotIn(params['state'], cookie)  # the nonce is inside the signed value, not readable as-is
        # The signed state itself expires after 10 minutes, whatever the browser does with Max-Age.
        value = set_cookies(headers)[OAUTH_COOKIE]
        payload = json.loads(base64.urlsafe_b64decode(value.split('.')[0] + '=' * (-len(value.split('.')[0]) % 4)))
        self.assertTrue(590 <= payload['exp'] - time.time() <= 600, payload['exp'] - time.time())
        other = urllib.parse.urlsplit(self.login()['Location'])
        self.assertNotEqual(dict(urllib.parse.parse_qsl(other.query))['state'], params['state'])

    def test_full_sign_in_sets_a_session_and_returns_to_next(self):
        status, headers, _ = self.hosted.sign_in('/#/x/abc/gallery?q=red')
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/#/x/abc/gallery?q=red')
        cookies = {c.split('=', 1)[0]: c for c in headers.get_all('Set-Cookie')}
        session = cookies[SESSION_COOKIE]
        for flag in ('Max-Age=%d' % (30 * 86400), 'Path=/', 'Secure', 'HttpOnly', 'SameSite=Lax'):
            self.assertIn(flag, session)
        self.assertNotIn('Domain', session)
        self.assertIn('Max-Age=0', cookies[OAUTH_COOKIE])
        value = set_cookies(headers)[SESSION_COOKIE]
        self.assertEqual(read_session(self.hosted.config, value, time.time()), 'alice@example.com')
        token_request = self.google.token_requests[-1]
        self.assertTrue(token_request['code_verifier'])
        self.assertEqual(token_request['redirect_uri'], self.hosted.origin + '/auth/callback')

    def test_open_redirects_in_next_end_at_the_start_page(self):
        for bad in ('//evil.example/x', '/\\evil.example', 'https://evil.example/', '/\t/evil.example',
                    'javascript:alert(1)', '/%s' % ('a' * 1100)):
            with self.subTest(next=bad):
                status, headers, _ = self.hosted.sign_in(bad)
                self.assertEqual(status, 302)
                self.assertEqual(headers['Location'], '/')

    def callback(self, query: str, oauth: str = None):
        return self.hosted.request('GET', '/auth/callback?' + query,
                                   cookies={OAUTH_COOKIE: oauth} if oauth is not None else None)

    def google_answer(self, headers) -> dict:
        _, answer, _ = raw_get(headers['Location'])
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(answer['Location']).query))

    def assert_refused(self, response, status, text=None):
        code, headers, body = response
        self.assertEqual(code, status, body)
        self.assertNotIn(SESSION_COOKIE + '=', ' '.join(c for c in headers.get_all('Set-Cookie') or []
                                                          if 'Max-Age=0' not in c))
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.assertIn(b'href="/auth/login"', body)
        if text:
            self.assertIn(text.encode(), body)
        for leak in (self.google.client_secret, SECRET, 'invalid_grant', 'Bad Request', 'Invalid Value'):
            self.assertNotIn(leak.encode(), body)
        cleared = [c for c in headers.get_all('Set-Cookie') or [] if c.startswith(OAUTH_COOKIE + '=;')]
        self.assertTrue(cleared and 'Max-Age=0' in cleared[0], headers.get_all('Set-Cookie'))  # the sign-in cookie goes

    def test_state_must_match_the_cookie(self):
        headers = self.login()
        oauth = set_cookies(headers)[OAUTH_COOKIE]
        answer = self.google_answer(headers)
        good = urllib.parse.urlencode(answer)
        self.assert_refused(self.callback(good), 400, 'Sign-in expired')                      # no cookie
        self.assert_refused(self.callback(urllib.parse.urlencode(dict(answer, state='x' * 43)), oauth), 400)
        self.assert_refused(self.callback(urllib.parse.urlencode({'code': answer['code']}), oauth), 400)
        other = set_cookies(self.login())[OAUTH_COOKIE]                                      # another login's cookie
        self.assert_refused(self.callback(good, other), 400)
        expired = sign(self.hosted.config.auth_secret, 'oauth', {
            'state': answer['state'], 'verifier': 'v', 'next': '/', 'exp': int(time.time()) - 1})
        self.assert_refused(self.callback(good, expired), 400)
        self.assert_refused(self.callback(good, '%s; %s=%s' % (oauth, OAUTH_COOKIE, oauth)), 400)
        self.assert_refused(self.callback(urllib.parse.urlencode(dict(answer, code='4/' + 'c' * 4095)), oauth), 400,
                            'Sign-in failed')                                                   # code over the cap
        self.assert_refused(self.callback(urllib.parse.urlencode({'state': answer['state']}), oauth), 400,
                            'Sign-in failed')                                                   # no code
        self.assertNotIn(answer['code'], [r.get('code') for r in self.google.token_requests])  # never exchanged
        status, headers, _ = self.callback(good, oauth)
        self.assertEqual(status, 302)
        self.assertIn(SESSION_COOKIE, set_cookies(headers))
        self.assert_refused(self.callback(good, oauth), 502)  # a code works once

    def test_cancelled_sign_in(self):
        self.google.approve = False
        headers = self.login()
        answer = self.google_answer(headers)
        self.assertEqual(answer['error'], 'access_denied')
        self.assert_refused(self.callback(urllib.parse.urlencode(answer), set_cookies(headers)[OAUTH_COOKIE]), 401,
                            'Sign-in cancelled')

    def test_every_id_token_rejection(self):
        cases = [({'aud': 'another-client.apps.example.com'}, 'another application'),
                 ({'iss': 'https://issuer.example'}, 'not issued by Google'),
                 ({'email_verified': 'false'}, 'not verified'),
                 ({'hd': 'other.example'}, 'not part of the example.com Workspace'),
                 ({'hd': REMOVE}, 'not part of the example.com Workspace'),
                 ({'email': 'mallory@other.example'}, 'not at example.com'),
                 ({'exp': str(int(time.time()) - 5)}, 'expired')]
        for claims, reason in cases:
            with self.subTest(claims=str(claims)):
                self.google.claims = claims
                self.assert_refused(self.hosted.sign_in(), 403, reason)
        self.google.claims = {}
        self.google.email, self.google.hd = 'mallory@other.example', 'other.example'
        self.assert_refused(self.hosted.sign_in(), 403, 'Access refused')

    def test_unusual_claim_values_are_refused_cleanly(self):
        for claims, reason in [({'exp': '\u00b2'}, 'expired'), ({'exp': '\u00b9\u2077' + '0' * 8}, 'expired'),
                               ({'exp': '1e12'}, 'expired'), ({'email_verified': 1}, 'not verified'),
                               ({'email_verified': 1.0}, 'not verified'), ({'email_verified': 'True'}, 'not verified')]:
            with self.subTest(claims=str(claims)):
                self.google.claims = claims
                self.assert_refused(self.hosted.sign_in(), 403, reason)

    def test_token_endpoint_failures(self):
        for fault in ('http_400', 'http_500', 'no_id_token', 'not_json', 'close', 'redirect'):
            with self.subTest(fault=fault):
                self.google.reset()
                self.google.token_fault = fault
                self.assert_refused(self.hosted.sign_in(), 502, 'Sign-in failed')
        for fault in ('http_400', 'http_500', 'not_json'):
            with self.subTest(tokeninfo=fault):
                self.google.reset()
                self.google.tokeninfo_fault = fault
                self.assert_refused(self.hosted.sign_in(), 502, 'Sign-in failed')
        log = self.hosted.log.getvalue()
        self.assertIn('token endpoint answered HTTP 400 (invalid_grant)', log)
        self.assertIn('token endpoint answered HTTP 302', log)
        self.assertEqual(self.google.stray_requests, [])  # the redirect was not followed
        self.assertNotIn(self.google.client_secret, log)

    def test_logout_clears_the_cookies(self):
        for method in ('GET', 'POST'):
            with self.subTest(method=method):
                session = self.hosted.session()
                status, headers, body = self.hosted.request(method, '/auth/logout', cookies=session.cookies)
                self.assertEqual(status, 200)
                self.assertIn(b'Signed out', body)
                self.assertIn(b'href="/auth/login"', body)
                cleared = {c.split('=', 1)[0]: c for c in headers.get_all('Set-Cookie')}
                for name in (SESSION_COOKIE, OAUTH_COOKIE):
                    self.assertTrue(cleared[name].startswith(name + '=;'))
                    for flag in ('Max-Age=0', 'Path=/', 'Secure', 'HttpOnly'):
                        self.assertIn(flag, cleared[name])


class SessionRouteTests(HostedTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.session = cls.hosted.session()

    def test_index_carries_the_csrf_token_and_mode(self):
        status, headers, body = self.hosted.get('/', self.session)
        self.assertEqual(status, 200)
        self.assertIn(('content="%s"' % self.session.token).encode(), body)
        self.assertIn(b'<meta name="mjv-mode" content="hosted">', body)
        self.assertNotIn(b'{{', body)
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.hosted.get('/x/abc/gallery', self.session)[0], 200)

    def test_health_names_the_user(self):
        data = self.json_of(self.hosted.get('/api/health', self.session)[2])
        self.assertEqual((data['mode'], data['user'], data['max_upload_bytes']), ('hosted', 'alice@example.com', 0))
        self.assertIsNone(data['soft_delete_days'])

    def test_deleting_is_off_without_a_write_url(self):
        health = self.json_of(self.hosted.get('/api/health', self.session)[2])
        self.assertEqual((health['delete_enabled'], health['can_delete_any']), (False, False))
        items = self.json_of(self.hosted.get('/api/exports', self.session)[2])['catalog']['items']
        self.assertEqual([(i['owner'], i['can_delete']) for i in items], [(None, False)])
        before = self.blob.read_json('catalog.json')
        status, _, body = self.hosted.post('/api/catalog/%s/delete' % self.entry['id'], self.session,
                                           body=json.dumps({'confirm': self.entry['id']}).encode())
        self.assertEqual((status, self.json_of(body)['error']['code']), (403, 'delete_disabled'))
        self.assertEqual(self.blob.read_json('catalog.json'), before)
        self.assertEqual([self.blob.count('catalog.json', method='PUT'), self.blob.count(self.entry['blob'], method='DELETE')],
                         [0, 0])

    def test_upload_open_by_path_and_library_are_refused(self):
        for path in ('/api/exports/open', '/api/exports/upload'):
            for method in ('GET', 'POST'):
                with self.subTest(path=path, method=method):
                    if method == 'POST':
                        status, _, body = self.hosted.post(path, self.session, body=json.dumps({'path': '/'}).encode())
                    else:
                        status, _, body = self.hosted.get(path, self.session)
                    self.assertEqual(status, 404)
                    self.assertEqual(self.json_of(body)['error']['code'], 'not_available')
        data = self.json_of(self.hosted.get('/api/exports', self.session)[2])
        self.assertEqual((data['library'], data['failed']), ([], []))
        self.assertNotIn(self.hosted.config.cache_dir, json.dumps(data))

    def test_post_needs_token_and_same_origin(self):
        path = '/api/catalog/%s/open' % self.entry['id']
        other = self.hosted.session(email='bob@example.com')
        for headers, code in [({'X-MJV-Token': ''}, 'forbidden'), ({'X-MJV-Token': 'x' * 43}, 'forbidden'),
                              ({'X-MJV-Token': other.token}, 'forbidden'),
                              ({'Origin': ''}, 'forbidden_origin'), ({'Origin': 'http://evil.example'}, 'forbidden_origin'),
                              ({'Origin': 'http://127.0.0.1:%d' % (self.hosted.port + 1)}, 'forbidden_origin'),
                              ({'Origin': 'https://127.0.0.1:%d' % self.hosted.port}, 'forbidden_origin'),
                              ({'Origin': 'null'}, 'forbidden_origin'), ({'Origin': None}, 'forbidden_origin'),
                              ({'Origin': 'http://evil.example', 'X-Forwarded-Host': 'evil.example',
                                'X-Forwarded-Proto': 'http'}, 'forbidden_origin')]:
            with self.subTest(headers=headers):
                if headers.get('Origin', '') is None:  # no Origin header at all
                    status, _, body = self.hosted.request('POST', path, body=b'{}', cookies=self.session.cookies,
                                                          headers={'X-MJV-Token': self.session.token})
                else:
                    status, _, body = self.hosted.post(path, self.session, headers=headers)
                self.assertEqual(status, 403)
                self.assertEqual(self.json_of(body)['error']['code'], code)
        status, _, body = self.hosted.post(path, self.session)
        self.assertIn(status, (200, 202))

    def test_unknown_catalog_entries_and_exports(self):
        self.assertEqual(self.hosted.get('/api/catalog/0123456789abcdef', self.session)[0], 404)
        self.assertEqual(self.hosted.post('/api/catalog/0123456789abcdef/open', self.session)[0], 404)
        self.assertEqual(self.hosted.get('/api/exports/0123456789abcdef/overview', self.session)[0], 404)
        self.assertEqual(self.hosted.get('/api/exports/../../etc/overview', self.session)[0], 404)
        self.assertEqual(self.hosted.get('/api/catalog/%s/nope' % self.entry['id'], self.session)[0], 404)
        self.assertEqual(self.hosted.request('PUT', '/api/health', cookies=self.session.cookies)[0], 405)


class DeleteTests(unittest.TestCase):
    """Who may delete a published export, and the checks on the request. alice@example.com owns one export,
    the other has no owner recorded, carol@example.com is an administrator and bob@example.com neither."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-delete-')
        cls.blob = FakeBlob(os.path.join(cls._tmp.name, 'blob'))
        cls.google = FakeGoogle()
        cls.hosted = Hosted(cls._tmp.name, cls.blob, cls.google, env={
            'MJV_CATALOG_WRITE_URL': cls.blob.write_url, 'MJV_ADMIN_EMAILS': 'Carol@example.com',
            'MJV_SOFT_DELETE_DAYS': '7'})
        cls.sessions = {name: cls.hosted.session('%s@example.com' % name) for name in ('alice', 'bob', 'carol')}

    @classmethod
    def tearDownClass(cls):
        cls.hosted.stop()
        cls.blob.stop()
        cls.google.stop()
        cls._tmp.cleanup()

    def setUp(self):
        # Only the catalog is read and written here, so the ZIPs need not be real ones.
        self.blob.faults.clear()
        self.blob.write_faults.clear()
        self.mine = self.blob.put_export(b'invented export of alice ' * 4, name='mj_export_alice.zip',
                                         owner='Alice@Example.com')
        self.orphan = self.blob.put_export(b'invented export without owner ' * 4, name='mj_export_orphan.zip')
        self.blob.write_catalog([self.mine, self.orphan])

    def delete(self, who, entry, body=None, headers=None):
        body = json.dumps({'confirm': entry['id']}).encode() if body is None else body
        status, _, answer = self.hosted.post('/api/catalog/%s/delete' % entry['id'], self.sessions[who], body=body,
                                             headers=headers)
        return status, json.loads(answer)

    def listed(self):
        return [e['id'] for e in self.blob.read_json('catalog.json')['exports']]

    def rights(self, who):
        items = json.loads(self.hosted.get('/api/exports', self.sessions[who])[2])['catalog']['items']
        return {i['id']: (i['owner'], i['can_delete']) for i in items}

    def assert_unchanged(self):
        self.assertEqual(self.listed(), [self.mine['id'], self.orphan['id']])
        for entry in (self.mine, self.orphan):
            self.assertIsNotNone(self.blob.file(entry['blob']))

    def test_owners_and_administrators_may_delete(self):
        mine, orphan = self.mine['id'], self.orphan['id']
        writes = self.blob.count('catalog.json', method='PUT')
        self.assertEqual(self.rights('alice'), {mine: ('alice@example.com', True), orphan: (None, False)})
        self.assertEqual(self.rights('bob'), {mine: ('alice@example.com', False), orphan: (None, False)})
        self.assertEqual(self.rights('carol'), {mine: ('alice@example.com', True), orphan: (None, True)})
        for who, admin in (('alice', False), ('bob', False), ('carol', True)):
            with self.subTest(who=who):
                health = json.loads(self.hosted.get('/api/health', self.sessions[who])[2])
                self.assertEqual((health['delete_enabled'], health['can_delete_any']), (True, admin))
        # Everyone else is refused, and nothing changes.
        for who, entry, says in (('bob', self.mine, 'only the owner'), ('bob', self.orphan, 'no owner recorded'),
                                 ('alice', self.orphan, 'no owner recorded')):
            with self.subTest(who=who, entry=entry['name']):
                status, answer = self.delete(who, entry)
                self.assertEqual((status, answer['error']['code']), (403, 'not_owner'))
                self.assertIn(says, answer['error']['message'])
                self.assertIn('could not delete %s for %s@example.com (not_owner): %s' % (
                    entry['id'], who, answer['error']['message']), self.hosted.log.getvalue())
                self.assert_unchanged()
        self.assertEqual(self.blob.count('catalog.json', method='PUT'), writes)
        # The owner, then an administrator.
        self.assertEqual(self.delete('alice', self.mine), (200, {'deleted': mine}))
        self.assertEqual(self.delete('carol', self.orphan), (200, {'deleted': orphan}))
        self.assertEqual(self.listed(), [])
        stones = self.blob.read_json('catalog.json')['deleted']
        self.assertEqual([(s['id'], s['owner'], s['deleted_by']) for s in stones],
                         [(mine, 'alice@example.com', 'alice@example.com'), (orphan, None, 'carol@example.com')])
        for entry in (self.mine, self.orphan):
            self.assertIsNone(self.blob.file(entry['blob']))
        status, answer = self.delete('alice', self.mine)  # already gone
        self.assertEqual((status, answer['error']['code']), (404, 'unknown_catalog_entry'))
        log = self.hosted.log.getvalue()
        self.assertEqual(log.count('deleted %s by alice@example.com' % mine), 1)
        self.assertEqual(log.count('deleted %s by carol@example.com' % orphan), 1)

    def test_an_administrator_may_delete_an_export_someone_owns(self):
        self.assertEqual(self.delete('carol', self.mine), (200, {'deleted': self.mine['id']}))
        self.assertEqual(self.listed(), [self.orphan['id']])

    def test_the_owner_is_checked_in_catalog_json_as_storage_holds_it(self):
        """Never in the copy an instance keeps for MJV_CATALOG_TTL_S: an owner changed in storage counts at once,
        for the owner it no longer names and for the one it names now."""
        instances = []
        for n in range(2):  # both keep the catalog for 10 minutes, read before the owners change
            hosted = Hosted(self._tmp.name, self.blob, self.google, ttl='600', env={
                'MJV_CATALOG_WRITE_URL': self.blob.write_url, 'MJV_CACHE_DIR': os.path.join(self._tmp.name, 'ttl-%d' % n)})
            self.addCleanup(hosted.stop)
            sessions = {name: hosted.session('%s@example.com' % name) for name in ('alice', 'bob')}
            hosted.get('/api/exports', sessions['alice'])
            instances.append((hosted, sessions))
        self.blob.write_catalog([dict(self.mine, owner='bob@example.com'), dict(self.orphan, owner='bob@example.com')])
        stored, writes = self.blob.read_json('catalog.json'), self.blob.count('catalog.json', method='PUT')
        (first, at_first), (second, at_second) = instances
        cached = json.loads(first.get('/api/exports', at_first['bob'])[2])['catalog']['items']
        self.assertEqual([(i['owner'], i['can_delete']) for i in cached], [('alice@example.com', False), (None, False)])

        def delete(hosted, session, entry):
            status, _, answer = hosted.post('/api/catalog/%s/delete' % entry['id'], session,
                                            body=json.dumps({'confirm': entry['id']}).encode())
            return status, json.loads(answer)

        # The owner the instance's copy names, but storage no longer does: refused, and nothing is written.
        status, answer = delete(first, at_first['alice'], self.mine)
        self.assertEqual((status, answer.get('error', {}).get('code')), (403, 'not_owner'))
        self.assertEqual((self.blob.read_json('catalog.json'), self.blob.count('catalog.json', method='PUT')),
                         (stored, writes))
        self.assertIsNotNone(self.blob.file(self.mine['blob']))
        # The owner storage names now, where the instance's copy names nobody, then someone else.
        self.assertEqual(delete(first, at_first['bob'], self.orphan), (200, {'deleted': self.orphan['id']}))
        self.assertEqual(delete(second, at_second['bob'], self.mine), (200, {'deleted': self.mine['id']}))
        self.assertEqual(self.listed(), [])
        self.assertEqual([s['deleted_by'] for s in self.blob.read_json('catalog.json')['deleted']],
                         ['bob@example.com', 'bob@example.com'])

    def test_the_request_must_confirm_the_id(self):
        path = '/api/catalog/%s/delete' % self.mine['id']
        for body, status, code in ((b'', 400, 'bad_request'), (b'not json', 400, 'bad_request'),
                                   (b'[]', 400, 'bad_request'), (b'{}', 400, 'bad_request'),
                                   (b'{"confirm": null}', 400, 'bad_request'),
                                   (json.dumps({'confirm': self.orphan['id']}).encode(), 400, 'bad_request'),
                                   (json.dumps({'confirm': self.mine['id'].upper()}).encode(), 400, 'bad_request'),
                                   (json.dumps({'confirm': [self.mine['id']]}).encode(), 400, 'bad_request'),
                                   (b'{"confirm": "' + b'a' * 5000 + b'"}', 413, 'too_large')):
            with self.subTest(body=body[:24]):
                answered, answer = self.delete('alice', self.mine, body=body)
                self.assertEqual((answered, answer['error']['code']), (status, code))
                self.assert_unchanged()
        self.assertEqual(self.hosted.get(path, self.sessions['alice'])[0], 405)
        body = json.dumps({'confirm': self.mine['id']}).encode()
        for length in ('9' * 5000, '0' * 20 + str(len(body)), '-1', '+%d' % len(body), '1e3', '0x10', '\u0663'):
            with self.subTest(length=length[:12]):  # never a number Python cannot convert: 413, not a traceback
                self.assertEqual(raw_post(self.hosted, path, self.sessions['alice'], length, body), (413, 'too_large'))
        self.assertNotIn('Traceback', self.hosted.log.getvalue())
        for catalog_id in ('0123456789abcdef', 'not-an-id', self.mine['id'] + '-1'):
            with self.subTest(catalog_id=catalog_id):
                status, _, answer = self.hosted.post('/api/catalog/%s/delete' % catalog_id, self.sessions['carol'],
                                                     body=json.dumps({'confirm': catalog_id}).encode())
                self.assertEqual((status, json.loads(answer)['error']['code']), (404, 'unknown_catalog_entry'))
        self.assert_unchanged()

    def test_token_and_origin_are_still_required(self):
        for headers, code in [({'X-MJV-Token': ''}, 'forbidden'),
                              ({'X-MJV-Token': self.sessions['carol'].token}, 'forbidden'),
                              ({'Origin': 'http://evil.example'}, 'forbidden_origin'),
                              ({'Origin': 'null'}, 'forbidden_origin'),
                              ({'Origin': 'http://evil.example', 'X-Forwarded-Host': 'evil.example'}, 'forbidden_origin')]:
            with self.subTest(headers=headers):
                status, answer = self.delete('alice', self.mine, headers=headers)
                self.assertEqual((status, answer['error']['code']), (403, code))
        status, _, body = self.hosted.request('POST', '/api/catalog/%s/delete' % self.mine['id'],
                                              body=json.dumps({'confirm': self.mine['id']}).encode(),
                                              cookies=self.sessions['alice'].cookies,
                                              headers={'X-MJV-Token': self.sessions['alice'].token})  # no Origin
        self.assertEqual((status, json.loads(body)['error']['code']), (403, 'forbidden_origin'))
        self.assert_unchanged()

    def test_healthz_says_deleting_is_on_and_names_nobody(self):
        status, _, body = self.hosted.request('GET', '/healthz', host='internal-probe')
        data = json.loads(body)
        self.assertEqual((status, data['catalog']['delete_enabled'], data['catalog']['soft_delete_days']), (200, True, 7))
        health = json.loads(self.hosted.get('/api/health', self.sessions['bob'])[2])
        self.assertEqual(health['soft_delete_days'], 7)
        for leak in (b'@', b'carol', b'alice', self.blob.write_secret.encode(), self.blob.secret.encode()):
            self.assertNotIn(leak, body)

    def test_secrets_never_reach_an_answer_or_the_log(self):
        responses = [self.hosted.get(path, self.sessions['alice']) for path in ('/api/exports', '/api/health', '/')]
        for fault in ('403', '500'):
            self.blob.write_faults['catalog.json'] = fault
            responses.append(self.hosted.post('/api/catalog/%s/delete' % self.mine['id'], self.sessions['alice'],
                                              body=json.dumps({'confirm': self.mine['id']}).encode()))
        texts = [self.hosted.log.getvalue(), repr(self.hosted.config), repr(self.hosted.app.store)]
        texts += [str(headers) + body.decode('utf-8', 'replace') for _, headers, body in responses]
        for text in texts:
            for secret in (self.blob.write_secret, self.blob.write_query, self.blob.secret, self.blob.query):
                self.assertNotIn(secret, text)
        self.assertEqual([json.loads(body)['error']['code'] for _, _, body in responses[-2:]],
                         ['storage_error', 'delete_unconfirmed'])


def json_keys(value) -> set:
    """Every key of every object inside a JSON value."""
    found, stack = set(), [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            found.update(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


class HideRouteTests(unittest.TestCase):
    """Hiding through the routes, and what reaches whom. alice@example.com owns two published ZIPs that hide
    fixtures.HIDE: hiding_export() and the two-root ZIP; a third ZIP has no owner recorded. carol@example.com is an
    administrator, bob@example.com neither. Every id, name, code and file name of a hidden item carries
    fixtures.MARKER."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-hide-')
        cls.blob = FakeBlob(os.path.join(cls._tmp.name, 'blob'))
        cls.google = FakeGoogle()
        cls.single = cls.blob.put_export(fx.hiding_zip_bytes(), name='mj_export_hiding.zip', owner='alice@example.com')
        cls.multi = cls.blob.put_export(fx.two_root_hiding_zip(), name='mj_export_two.zip', owner='alice@example.com',
                                        label='Two roots')
        cls.orphan = cls.blob.put_export(fx.hiding_zip_bytes(root='mj_export_orphan'), name='mj_export_orphan.zip')
        cls.write_catalog()
        cls.hosted = Hosted(cls._tmp.name, cls.blob, cls.google, env={
            'MJV_CATALOG_WRITE_URL': cls.blob.write_url, 'MJV_ADMIN_EMAILS': 'carol@example.com'})
        cls.sessions = {name: cls.hosted.session('%s@example.com' % name) for name in ('alice', 'bob', 'carol')}
        for entry in (cls.single, cls.multi, cls.orphan):
            cls.hosted.post('/api/catalog/%s/open' % entry['id'], cls.sessions['alice'])
            deadline = time.time() + 30
            while json.loads(cls.hosted.get('/api/catalog/%s' % entry['id'], cls.sessions['alice'])[2])['state'] != 'ready':
                assert time.time() < deadline, 'not ready'
                time.sleep(0.05)

    @classmethod
    def write_catalog(cls):
        record = {'jobs': [{'id': 'job-zq-solo', 'hidden_at': '2026-11-03T10:15:00Z', 'hidden_by': 'alice@example.com'}],
                  'moodboards': [{'id': 'board-zq', 'hidden_at': '2026-11-03T10:20:00Z',
                                  'hidden_by': 'carol@example.com'}]}
        cls.blob.write_catalog([dict(entry, hidden=record) for entry in (cls.single, cls.multi, cls.orphan)])

    @classmethod
    def tearDownClass(cls):
        cls.hosted.stop()
        cls.blob.stop()
        cls.google.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.write_catalog()  # a test that hid or showed something leaves the next one the same lists

    def answer(self, path, who, method='GET', body=None, headers=None):
        if method == 'POST':
            status, sent, raw = self.hosted.post(path, self.sessions[who], body=body or b'{}', headers=headers)
        else:
            status, sent, raw = self.hosted.get(path, self.sessions[who])
        return status, sent, raw

    def change(self, who, entry, kind, item_id, show=False):
        status, _, raw = self.answer('/api/catalog/%s/%s' % (entry['id'], 'unhide' if show else 'hide'), who, 'POST',
                                     json.dumps({'kind': kind, 'id': item_id}).encode())
        return status, json.loads(raw)

    def roots(self, entry):
        """The server's own full index of every root of an entry, which knows every hidden id and file."""
        prep = self.hosted.app.published._prep(entry['id'])
        return [self.hosted.app.registry.get(export_id) for export_id in prep.export_ids]

    def routes(self, entry):
        """Every GET path of 3.9 for an entry: its items, every root's views with every page, filter and search
        word the full index knows of, the detail of every job and moodboard, and every file and thumbnail."""
        roots = self.roots(entry)
        closure = hidden_closure(roots, fx.HIDE['jobs'], fx.HIDE['moodboards'])
        paths = ['/healthz', '/api/health', '/api/exports', '/api/catalog/%s' % entry['id']]
        for full in roots:
            base = '/api/exports/%s' % full.id
            facets = full.overview()['facets']
            prompts = {word for job_id in closure.jobs if job_id in full.jobs for word in full.jobs[job_id].prompt.split()}
            queries = [{'limit': '500'}, {'limit': '500', 'sort': 'oldest'}, {'limit': '2', 'offset': '1'},
                       {'q': fx.MARKER}, {'q': fx.COPY}, {'links': 'explicit', 'moodboard': 'board-zq'}]
            queries += [{'q': word} for word in sorted(prompts)]
            queries += [{'kind': kind['value']} for kind in facets['kinds']]
            queries += [{'version': version['value']} for version in facets['versions']]
            queries += [{'moodboard': board['id']} for board in facets['moodboards']]
            queries += [{'profile': profile['id']} for profile in facets['profiles']]
            queries += [{'profile': 'code:' + code['code']} for job in full.jobs.values() for code in job.codes]
            queries += [{'date_from': facets['date_min'], 'date_to': facets['date_min']},
                        {'date_from': facets['date_min'], 'date_to': facets['date_max']}]
            paths += [base + '/' + route for route in ('overview', 'moodboards', 'profiles', 'likes')]
            paths += [base + '/jobs?' + urllib.parse.urlencode(query) for query in queries]
            paths += [base + '/jobs/' + urllib.parse.quote(job_id, safe='') for job_id in full.jobs]
            paths += [base + '/moodboards/' + urllib.parse.quote(board_id, safe='') for board_id in full.boards]
            for file_id in full.file_ids:
                paths += ['/files/%s/%s' % (full.id, file_id), '/files/%s/%s?size=thumb' % (full.id, file_id)]
        return paths

    def assert_clean(self, path, headers, body):
        """No trace of a hidden item in an answer: headers and text never carry the marker, the copied prompt only
        where its visible upscale is, and nothing that only managers get. (Image bytes are not text: a hidden
        picture's address answers 404, checked by the caller.)"""
        self.assertNotIn(fx.MARKER, str(headers).lower(), path)
        if not headers['Content-Type'].startswith('image/'):
            text = body.lower()
            self.assertNotIn(fx.MARKER.encode(), text, path)
            if fx.COPY.encode() in text:
                self.assertIn(b'job-child-up', text, path)
            self.assertNotIn(fx.COPY.encode(), text.replace(fx.COPY_PROMPT.encode(), b''), path)
            if headers['Content-Type'].startswith('application/json'):
                self.assertFalse({'hidden', 'hide_preview'} & json_keys(json.loads(body)), path)

    def test_healthz_lists_its_features(self):
        status, _, body = self.hosted.request('GET', '/healthz', host='internal-probe')
        data = json.loads(body)
        self.assertEqual((status, data['features'], data['catalog']['delete_enabled']), (200, ['hide'], True))
        for who in ('alice', 'bob'):
            self.assertIs(json.loads(self.answer('/api/health', who)[2])['hide_enabled'], True)

    def test_who_may_hide_and_show_again(self):
        def rights(who):
            items = json.loads(self.answer('/api/exports', who)[2])['catalog']['items']
            return {i['id']: (i['can_hide'], i['can_delete'], 'hidden' in i) for i in items}

        single, multi, orphan = self.single['id'], self.multi['id'], self.orphan['id']
        self.assertEqual(rights('alice'), {single: (True, True, True), multi: (True, True, True),
                                           orphan: (False, False, False)})
        self.assertEqual(rights('bob'), {key: (False, False, False) for key in (single, multi, orphan)})
        self.assertEqual(rights('carol'), {key: (True, True, True) for key in (single, multi, orphan)})
        writes = self.blob.count('catalog.json', method='PUT')
        for who, entry, says in (('bob', self.single, 'only the owner of this export or an administrator can hide or '
                                                      'show its generations and moodboards'),
                                 ('bob', self.orphan, 'this export has no owner recorded, so only an administrator can '
                                                      'hide or show its generations and moodboards'),
                                 ('alice', self.orphan, 'this export has no owner recorded')):
            for show in (False, True):
                with self.subTest(who=who, entry=entry['name'], show=show):
                    status, answer = self.change(who, entry, 'job', 'job-grid-a' if not show else 'job-zq-solo', show)
                    self.assertEqual((status, answer['error']['code']), (403, 'not_owner'))
                    self.assertIn(says, answer['error']['message'])
                    self.assertIn('could not %s in %s for %s@example.com (not_owner): ' % (
                        'show again' if show else 'hide', entry['id'], who), self.hosted.log.getvalue())
        self.assertEqual(self.blob.count('catalog.json', method='PUT'), writes)
        # The owner and an administrator may, both ways.
        for who, entry in (('alice', self.single), ('carol', self.orphan)):
            with self.subTest(who=who):
                status, answer = self.change(who, entry, 'job', 'job-grid-a')
                self.assertEqual((status, answer['changed'], answer['catalog']['can_hide']), (200, True, True))
                # HIDE's 5 generations and 18 pictures, and job-grid-a's 4
                self.assertEqual(answer['catalog']['hidden'], {'jobs': 6, 'moodboards': 1, 'images': 22})
                status, answer = self.change(who, entry, 'job', 'job-grid-a', show=True)
                self.assertEqual((status, answer['changed']), (200, True))
        # Writes off: nobody manages anything, and every hidden list still applies.
        other = Hosted(self._tmp.name, self.blob, self.google, env={
            'MJV_ADMIN_EMAILS': 'carol@example.com', 'MJV_CACHE_DIR': os.path.join(self._tmp.name, 'off')})
        self.addCleanup(other.stop)
        carol = other.session('carol@example.com')
        self.assertIs(json.loads(other.get('/api/health', carol)[2])['hide_enabled'], False)
        status, _, body = other.post('/api/catalog/%s/hide' % self.single['id'], carol,
                                     body=json.dumps({'kind': 'job', 'id': 'job-grid-a'}).encode())
        self.assertEqual((status, json.loads(body)['error']), (403, {'code': 'hide_disabled',
                                                                     'message': 'hiding is not turned on for this viewer'}))
        self.assertEqual(json.loads(other.get('/api/exports/%s/hidden' % self.single['id'], carol)[2])['error']['code'],
                         'hide_disabled')
        items = json.loads(other.get('/api/exports', carol)[2])['catalog']['items']
        self.assertEqual({(i['can_hide'], 'hidden' in i) for i in items}, {(False, False)})
        other.post('/api/catalog/%s/open' % self.single['id'], carol)
        deadline = time.time() + 30
        while json.loads(other.get('/api/catalog/%s' % self.single['id'], carol)[2])['state'] != 'ready':
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        status, _, body = other.get('/api/exports/%s/jobs/job-zq-solo' % self.single['id'], carol)
        self.assertEqual((status, json.loads(body)['error']['code']), (404, 'unknown_job'))

    def test_the_request_is_checked(self):
        path = '/api/catalog/%s/hide' % self.single['id']
        bad = 'body must be a JSON object such as {"kind": "job", "id": "<job id>"}'
        for body in (b'', b'not json', b'[]', b'{}', b'{"id": "job-grid-a"}', b'{"kind": "generation", "id": "job-grid-a"}',
                     b'{"kind": "job"}', b'{"kind": "job", "id": null}', b'{"kind": "job", "id": ""}',
                     b'{"kind": "job", "id": "   "}', json.dumps({'kind': 'job', 'id': 'j' * 201}).encode(),
                     b'{"kind": "job", "id": "job\\u0007"}', b'{"kind": "job", "id": 12345}',
                     b'{"kind": ["job"], "id": "job-grid-a"}'):
            with self.subTest(body=body[:40]):
                for route in ('hide', 'unhide'):
                    status, _, raw = self.hosted.post('/api/catalog/%s/%s' % (self.single['id'], route),
                                                      self.sessions['alice'], body=body)
                    self.assertEqual((status, json.loads(raw)['error']), (400, {'code': 'bad_request', 'message': bad}))
        status, _, raw = self.hosted.post(path, self.sessions['alice'],
                                          body=b'{"kind": "job", "id": "' + b'a' * 5000 + b'"}')
        self.assertEqual((status, json.loads(raw)['error']['code']), (413, 'too_large'))
        for catalog_id in ('0123456789abcdef', 'not-an-id', self.multi['id'] + '-1'):
            with self.subTest(catalog_id=catalog_id):
                status, _, raw = self.hosted.post('/api/catalog/%s/hide' % catalog_id, self.sessions['carol'],
                                                  body=b'{"kind": "job", "id": "job-grid-a"}')
                self.assertEqual((status, json.loads(raw)['error']['code']), (404, 'unknown_catalog_entry'))
        for kind, item_id, code in (('job', 'job-invented', 'unknown_job'), ('moodboard', 'board-invented',
                                                                            'unknown_moodboard'),
                                    ('job', 'board-1', 'unknown_job'), ('moodboard', 'job-grid-a', 'unknown_moodboard')):
            with self.subTest(kind=kind, item=item_id):
                status, answer = self.change('alice', self.single, kind, item_id)
                self.assertEqual((status, answer['error']['code']), (404, code))
                self.assertEqual(answer['error']['message'], 'no %s with this id in the export' % kind)
        self.assertEqual(self.change('alice', self.single, 'job', 'job-invented', show=True)[1]['changed'], False)
        # A job of the second root only is one of the ZIP's: hiding it works from either root's export.
        self.assertEqual(self.change('alice', self.multi, 'job', 'job-r2-plain')[1]['changed'], True)
        for route in ('/api/catalog/%s/hide' % self.single['id'], '/api/catalog/%s/unhide' % self.single['id']):
            self.assertEqual(self.answer(route, 'alice')[0], 405)
        self.assertEqual(self.answer('/api/exports/%s/hidden' % self.single['id'], 'alice', 'POST')[0], 405)
        for headers, code in (({'X-MJV-Token': ''}, 'forbidden'), ({'X-MJV-Token': self.sessions['bob'].token}, 'forbidden'),
                              ({'Origin': 'http://evil.example'}, 'forbidden_origin'),
                              ({'Origin': 'null'}, 'forbidden_origin')):
            with self.subTest(headers=headers):
                status, _, raw = self.answer(path, 'alice', 'POST', b'{"kind": "job", "id": "job-grid-a"}', headers)
                self.assertEqual((status, json.loads(raw)['error']['code']), (403, code))
        self.assertNotIn('Traceback', self.hosted.log.getvalue())

    def test_hidden_ids_and_files_answer_like_unknown_ones_for_everyone_else(self):
        full = self.roots(self.single)[0]
        visible = self.hosted.app.published._variants[self.single['id']].views[full.id]
        pinned = full.files['images/moodboard_board-1_2.png'].file_id
        hidden_file = full.files['images/job-zq-solo_0.png'].file_id
        base = '/api/exports/%s' % self.single['id']
        pairs = [(base + '/jobs/job-zq-solo', base + '/jobs/job-invented'),
                 (base + '/jobs/job-zq-gone', base + '/jobs/job-invented'),
                 (base + '/moodboards/board-zq', base + '/moodboards/board-invented')]
        for file_id in (hidden_file, pinned):
            for query in ('', '?size=thumb'):
                pairs.append(('/files/%s/%s%s' % (self.single['id'], file_id, query),
                              '/files/%s/%s%s' % (self.single['id'], '0' * 20, query)))
        for hidden, invented in pairs:
            with self.subTest(path=hidden):
                status, headers, body = self.answer(hidden, 'bob')
                other_status, other_headers, other_body = self.answer(invented, 'bob')
                self.assertEqual((status, headers['Content-Type'], headers['Cache-Control'], body),
                                 (other_status, other_headers['Content-Type'], other_headers['Cache-Control'], other_body))
                self.assertEqual(status, 404)
        self.assertIn('images/moodboard_board-1_2.png', visible.hidden.files)
        # Managers get them.
        for path in (base + '/jobs/job-zq-solo', base + '/moodboards/board-zq', pairs[3][0], pairs[4][0]):
            self.assertEqual(self.answer(path, 'alice')[0], 200, path)

    def test_nothing_hidden_reaches_anyone_else_by_any_route(self):
        unknown_file = self.answer('/files/%s/%s' % (self.single['id'], '0' * 20), 'bob')[2]
        for entry in (self.single, self.multi):
            roots = self.roots(entry)
            hidden_files = {(index.id, index.files[rel].file_id) for index in roots
                            for rel in self.hosted.app.published._variants[entry['id']].views[index.id].hidden.files}
            self.assertTrue(hidden_files)
            requests = 0
            for path in self.routes(entry):
                status, headers, body = self.answer(path, 'bob')
                requests += 1
                with self.subTest(path=path):
                    self.assertIn(status, (200, 404), body[:200])
                    self.assert_clean(path, headers, body)
                    if path.startswith('/files/'):
                        export_id, file_id = path.split('?')[0].split('/')[2:4]
                        hidden = (export_id, file_id) in hidden_files
                        self.assertEqual(status, 404 if hidden else 200)
                        if hidden:  # the very bytes of an id that never existed
                            self.assertEqual(body, unknown_file)
                    if '/jobs/' in path or '/moodboards/' in path:
                        item_id = urllib.parse.unquote(path.rsplit('/', 1)[1])
                        self.assertEqual(status, 404 if fx.MARKER in item_id else 200, path)
            self.assertGreater(requests, 150)
            # POST routes a visitor may send.
            for route, body in (('open', b'{}'), ('hide', b'{"kind": "job", "id": "job-zq-solo"}'),
                                ('unhide', b'{"kind": "moodboard", "id": "board-zq"}'),
                                ('delete', json.dumps({'confirm': entry['id']}).encode())):
                status, headers, answer = self.answer('/api/catalog/%s/%s' % (entry['id'], route), 'bob', 'POST', body)
                self.assertEqual(status, 200 if route == 'open' else 403)
                self.assert_clean(route, headers, answer)
            for index in roots:
                status, headers, answer = self.answer('/api/exports/%s/hidden' % index.id, 'bob')
                self.assertEqual((status, json.loads(answer)['error']['code']), (403, 'not_owner'))
                self.assert_clean('hidden', headers, answer)
        # The lists a manager browses are the very same answers.
        for entry in (self.single, self.multi):
            for index in self.roots(entry):
                base = '/api/exports/%s' % index.id
                for path in (base + '/overview', base + '/jobs?limit=500', base + '/moodboards', base + '/profiles',
                             base + '/likes', base + '/jobs?q=' + fx.COPY):
                    self.assertEqual(self.answer(path, 'alice')[2], self.answer(path, 'bob')[2], path)

    def test_managers_see_hidden_items_only_where_asked(self):
        base = '/api/exports/%s' % self.single['id']
        listing = json.loads(self.answer(base + '/hidden', 'alice')[2])
        self.assertEqual(listing['counts'], None)  # written by hand without counts: not counted yet
        self.assertEqual([(row['id'], row['hidden_by'], row['generations']) for row in listing['moodboards']],
                         [('board-zq', 'carol@example.com', 4)])
        self.assertEqual([(row['id'], row['hidden_at'], row['export_id']) for row in listing['jobs']],
                         [('job-zq-solo', '2026-11-03T10:15:00Z', self.single['id'])])
        self.assertEqual(listing['jobs'][0]['job']['job_id'], 'job-zq-solo')
        detail = json.loads(self.answer(base + '/jobs/job-zq-exp', 'alice')[2])
        self.assertEqual(detail['hidden'], {'on_its_own': None, 'with_moodboards': [
            {'id': 'board-zq', 'name': 'Zqboard', 'export_id': self.single['id'], 'hidden_at': '2026-11-03T10:20:00Z',
             'hidden_by': 'carol@example.com'}]})
        self.assertNotIn('hide_preview', detail)
        board = json.loads(self.answer(base + '/moodboards/board-zq', 'alice')[2])
        self.assertEqual(board['hidden'], {'hidden_at': '2026-11-03T10:20:00Z', 'hidden_by': 'carol@example.com',
                                           'generations': 4, 'stay_hidden': 0})
        self.assertEqual(len(board['generations']), 4)
        visible = json.loads(self.answer(base + '/jobs/job-grid-a', 'alice')[2])
        self.assertEqual((visible['hide_preview'], 'hidden' in visible), ({'made_from_it': 2}, False))
        visible = json.loads(self.answer(base + '/moodboards/board-1', 'alice')[2])
        self.assertEqual(visible['hide_preview'], {'generations': 1, 'made_from_them': 2})
        self.assertEqual(json.loads(self.answer(base + '/jobs/job-child-up', 'alice')[2])['job']['parent_status'],
                         'unavailable')  # managers browse like everyone (R4)
        # Their pictures, for managers only.
        file_url = listing['jobs'][0]['job']['images'][0]['url']
        self.assertEqual([self.answer(file_url, who)[0] for who in ('alice', 'carol', 'bob')], [200, 200, 404])
        # The Hidden listing covers the whole ZIP whatever root is asked, and only roots that exist.
        multi = json.loads(self.answer('/api/exports/%s-2/hidden' % self.multi['id'], 'alice')[2])
        self.assertEqual(multi, json.loads(self.answer('/api/exports/%s-1/hidden' % self.multi['id'], 'alice')[2]))
        self.assertEqual(multi['moodboards'][0]['generations'], 5)
        for path in ('/api/exports/%s-3/hidden' % self.multi['id'], '/api/exports/%s/hidden' % self.multi['id'],
                     '/api/exports/0123456789abcdef/hidden'):
            self.assertEqual(json.loads(self.answer(path, 'alice')[2])['error']['code'], 'unknown_export', path)
        # An administrator manages the export without an owner.
        self.assertEqual(json.loads(self.answer('/api/exports/%s/hidden' % self.orphan['id'], 'carol')[2])['counts'],
                         None)
        # Hiding counts: the item and its listing say what is hidden once the app wrote the list.
        status, answer = self.change('alice', self.single, 'job', 'job-zq-exp')  # on its own too: no rebuild
        self.assertEqual((status, answer['catalog']['hidden']), (200, {'jobs': 5, 'moodboards': 1, 'images': 18}))
        self.assertEqual(json.loads(self.answer(base + '/hidden', 'alice')[2])['counts'],
                         {'jobs': 5, 'moodboards': 1, 'images': 18})


class CacheCheckTests(unittest.TestCase):
    def test_an_unwritable_cache_shows_in_healthz(self):
        if os.geteuid() == 0:
            self.skipTest('root can write anywhere')
        with tempfile.TemporaryDirectory(prefix='mjv-rocache-') as base:
            blob, google = FakeBlob(os.path.join(base, 'blob')), FakeGoogle()
            cache = os.path.join(base, 'cache')
            os.makedirs(cache)
            os.chmod(cache, 0o555)
            hosted = Hosted(base, blob, google)
            try:
                self.assertFalse(hosted.app.check_cache())
                health = json.loads(hosted.get('/healthz')[2])
                self.assertEqual((health['ok'], health['cache']), (True, {'ok': False}))
                self.assertIn('is not writable', hosted.log.getvalue())
                os.chmod(cache, 0o755)
                self.assertTrue(hosted.app.check_cache())
                self.assertEqual(json.loads(hosted.get('/healthz')[2])['cache'], {'ok': True})
            finally:
                os.chmod(cache, 0o755)
                hosted.stop()
                blob.stop()
                google.stop()


class RunTests(unittest.TestCase):
    """python3 -m mjviewer --hosted as its own process, the way a container runs it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mjv-run-')
        self.base = self._tmp.name
        self.blob = FakeBlob(os.path.join(self.base, 'blob'))
        self.google = FakeGoogle()

    def tearDown(self):
        self.blob.stop()
        self.google.stop()
        self._tmp.cleanup()

    def test_start_probe_health_and_a_clean_stop_during_a_download(self):
        entry = self.blob.put_export(fx.export_zip_bytes(fx.gallery_export(60)), name='slow.zip')
        self.blob.write_catalog([entry])
        self.blob.faults[entry['blob']] = 'slow'
        self.blob.chunk, self.blob.slow_delay = 1024, 0.05
        port = free_port()
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.base, 'PORT': str(port), 'PYTHONDONTWRITEBYTECODE': '1'}
        env.update(hosted_env(port, self.base, self.blob, self.google))
        zips = os.path.join(self.base, 'cache', 'zips')
        os.makedirs(zips)
        for name in ('.dl-%s-dead-1.part' % ('0' * 64), '.dl-%s.lock' % ('0' * 64)):  # left by a killed process
            with open(os.path.join(zips, name), 'wb') as handle:
                handle.write(b'x')
            os.utime(os.path.join(zips, name), (time.time() - 3600,) * 2)
        with open(os.path.join(self.base, 'stderr.log'), 'w') as err:
            proc = subprocess.Popen([sys.executable, '-m', 'mjviewer', '--hosted'], cwd=VIEWER_DIR, env=env,
                                    stdout=subprocess.PIPE, stderr=err, text=True)
            try:
                self.assertIn('(hosted) on port %d' % port, proc.stdout.readline())
                client = Client(port, 'http://127.0.0.1:%d' % port, self.google)
                # The catalog is read at start, before anyone asks for it.
                deadline = time.time() + 15
                health = {}
                while time.time() < deadline and not health.get('catalog', {}).get('ok'):
                    health = json.loads(client.get('/healthz')[2])
                    time.sleep(0.05)
                self.assertEqual((health['catalog'], health['cache']), ({'exports': 1, 'ok': True,
                                                                        'age_s': health['catalog']['age_s'],
                                                                        'delete_enabled': False,
                                                                        'zip_deletes_pending': 0,
                                                                        'soft_delete_days': None},
                                                                       {'ok': True}))
                self.assertEqual(self.blob.count('catalog.json'), 1)
                self.assertEqual(listdir(zips), [])  # abandoned downloads removed at start
                if sys.platform.startswith('linux'):  # bound to every address, not only 127.0.0.1
                    connection = http.client.HTTPConnection('127.0.0.2', port, timeout=10)
                    connection.request('GET', '/robots933456.txt')
                    self.assertEqual(connection.getresponse().status, 200)
                    connection.close()
                session = client.session()
                self.assertEqual(client.post('/api/catalog/%s/open' % entry['id'], session)[0], 202)
                while time.time() < deadline + 15 and not any(n.endswith('.part') for n in listdir(zips)):
                    time.sleep(0.02)
                self.assertTrue(any(n.endswith('.lock') for n in listdir(zips)), listdir(zips))
                proc.send_signal(signal.SIGTERM)  # what the platform sends on every deploy, restart or scale
                self.assertEqual(proc.wait(timeout=20), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
        self.assertEqual([n for n in listdir(zips) if n.startswith('.dl-')], [])  # no orphaned download files
        with open(os.path.join(self.base, 'stderr.log')) as handle:
            log = handle.read()
        self.assertIn('the server is stopping', log)
        for secret in (SECRET, self.google.client_secret, self.blob.secret):
            self.assertNotIn(secret, log)


def listdir(path: str) -> list:
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


if __name__ == '__main__':
    unittest.main()
