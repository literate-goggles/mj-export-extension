"""A local stand-in for Google's OAuth endpoints, for hosted-mode tests.

FakeGoogle serves, on http://127.0.0.1:<port>:

* /o/oauth2/v2/auth approves at once and redirects to redirect_uri with a
  one-time code and the state (or with error=access_denied when approve is off).
  With chooser on it answers with an account page instead, whose link the user
  follows, as on Google's own prompt=select_account page: that is a new
  navigation, so the address's #fragment is not carried over the way a plain
  redirect carries it;
* /token exchanges a code for an opaque id token, checking the client id and
  secret, the redirect_uri and the PKCE verifier;
* /tokeninfo returns the claims of an id token, the way Google's endpoint
  does (string values for email_verified, iat and exp).

The signed-in account (email, hd), any claim (claims, with REMOVE to drop one)
and failures of both token endpoints (token_fault, tokeninfo_fault) can be set
per test, so every rejection path can be exercised. All values are invented.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

REMOVE = object()
TOKEN_FAULTS = ('http_400', 'http_500', 'no_id_token', 'not_json', 'close', 'redirect')
TOKENINFO_FAULTS = ('http_400', 'http_500', 'not_json')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'fake-google'
    sys_version = ''

    def log_message(self, format, *args):  # noqa: A002 - quiet
        pass

    @property
    def google(self) -> 'FakeGoogle':
        return self.server.google

    def _send(self, status: int, body: bytes = b'', content_type: str = 'application/json',
              headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict) -> None:
        self._send(status, json.dumps(value).encode())

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition('?')
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        if path == '/o/oauth2/v2/auth':
            return self._authorize(params)
        if path == '/tokeninfo':
            return self._tokeninfo(params)
        self.google.stray_requests.append(path)
        return self._json(404, {'error': 'not_found'})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode(), keep_blank_values=True).items()}
        if self.path.partition('?')[0] == '/token':
            return self._token(form)
        self.google.stray_requests.append(self.path.partition('?')[0])
        return self._json(404, {'error': 'not_found'})

    def _authorize(self, params: dict) -> None:
        google = self.google
        google.authorize_requests.append(dict(params))
        redirect = params.get('redirect_uri') or ''
        if params.get('client_id') != google.client_id or params.get('response_type') != 'code' or not redirect:
            return self._json(400, {'error': 'invalid_request'})
        if not google.approve:
            answer = {'error': 'access_denied', 'state': params.get('state', '')}
        else:
            code = '4/test-' + secrets.token_urlsafe(24)
            google.codes[code] = {'redirect_uri': redirect, 'challenge': params.get('code_challenge'),
                                  'method': params.get('code_challenge_method')}
            answer = {'code': code, 'state': params.get('state', ''), 'scope': params.get('scope', '')}
        location = redirect + '?' + urllib.parse.urlencode(answer)
        if google.chooser:
            page = ('<!doctype html><title>Choose an account</title><p>Choose an account</p>'
                    '<p><a id="account" href="%s">%s</a></p>' % (html.escape(location), html.escape(google.email)))
            return self._send(200, page.encode(), 'text/html; charset=utf-8')
        self._send(302, b'', 'text/plain', {'Location': location})

    def _token(self, form: dict) -> None:
        google = self.google
        google.token_requests.append({k: v for k, v in form.items() if k != 'client_secret'})
        fault = google.token_fault
        if fault == 'close':
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return None
        if fault in ('http_400', 'http_500'):
            return self._json(int(fault[5:]), {'error': 'invalid_grant', 'error_description': 'Bad Request'})
        if fault == 'not_json':
            return self._send(200, b'<html>not json</html>', 'text/html')
        if fault == 'redirect':
            return self._send(302, b'', 'text/plain', {'Location': google.origin + '/token-elsewhere'})
        grant = google.codes.pop(form.get('code', ''), None)
        if (form.get('grant_type') != 'authorization_code' or form.get('client_id') != google.client_id
                or form.get('client_secret') != google.client_secret or grant is None
                or form.get('redirect_uri') != grant['redirect_uri']):
            return self._json(400, {'error': 'invalid_grant', 'error_description': 'Bad Request'})
        if grant['challenge'] is not None:
            verifier = form.get('code_verifier') or ''
            if grant['method'] != 'S256' or _b64(hashlib.sha256(verifier.encode()).digest()) != grant['challenge']:
                return self._json(400, {'error': 'invalid_grant', 'error_description': 'Invalid code verifier.'})
        if fault == 'no_id_token':
            return self._json(200, {'access_token': 'ya29.test', 'expires_in': 3599, 'token_type': 'Bearer'})
        token = 'test.' + secrets.token_urlsafe(32)
        google.tokens[token] = time.time()
        return self._json(200, {'access_token': 'ya29.test', 'expires_in': 3599, 'id_token': token,
                                'scope': 'openid email profile', 'token_type': 'Bearer'})

    def _tokeninfo(self, params: dict) -> None:
        google = self.google
        fault = google.tokeninfo_fault
        if fault in ('http_400', 'http_500'):
            return self._json(int(fault[5:]), {'error': 'invalid_token', 'error_description': 'Invalid Value'})
        if fault == 'not_json':
            return self._send(200, b'not json', 'text/plain')
        issued = google.tokens.get(params.get('id_token', ''))
        if issued is None:
            return self._json(400, {'error': 'invalid_token', 'error_description': 'Invalid Value'})
        claims = {'iss': 'https://accounts.google.com', 'azp': google.client_id, 'aud': google.client_id,
                  'sub': '100000000000000000001', 'hd': google.hd, 'email': google.email, 'email_verified': 'true',
                  'at_hash': 'test', 'name': 'Test User', 'iat': str(int(issued)), 'exp': str(int(issued) + 3600),
                  'alg': 'RS256', 'kid': 'test', 'typ': 'JWT'}
        for key, value in google.claims.items():
            if value is REMOVE:
                claims.pop(key, None)
            else:
                claims[key] = value
        return self._json(200, claims)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # injected faults close connections on purpose


class FakeGoogle:
    def __init__(self, email: str = 'alice@example.com', hd: Optional[str] = 'example.com',
                 client_id: str = 'test-client.apps.example.com', client_secret: str = 'TESTONLY-client-secret-0123',
                 port: int = 0):
        self.email = email
        self.hd = hd
        self.client_id = client_id
        self.client_secret = client_secret
        self.approve = True
        self.chooser = False
        self.claims: Dict[str, object] = {}
        self.token_fault: Optional[str] = None
        self.tokeninfo_fault: Optional[str] = None
        self.authorize_requests: List[dict] = []
        self.token_requests: List[dict] = []
        self.stray_requests: List[str] = []  # paths nobody should ask for (where a redirect would lead)
        self.codes: Dict[str, dict] = {}
        self.tokens: Dict[str, float] = {}
        self.server = _Server(('127.0.0.1', port), _Handler)
        self.server.google = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return 'http://127.0.0.1:%d' % self.server.server_address[1]

    def env(self) -> Dict[str, str]:
        """The client settings and test endpoint overrides for the hosted viewer."""
        return {'GOOGLE_CLIENT_ID': self.client_id, 'GOOGLE_CLIENT_SECRET': self.client_secret,
                'MJV_GOOGLE_AUTH_URL': self.origin + '/o/oauth2/v2/auth',
                'MJV_GOOGLE_TOKEN_URL': self.origin + '/token',
                'MJV_GOOGLE_TOKENINFO_URL': self.origin + '/tokeninfo'}

    def reset(self) -> None:
        self.approve = True
        self.chooser = False
        self.claims = {}
        self.token_fault = self.tokeninfo_fault = None

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
