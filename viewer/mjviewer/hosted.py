"""Hosted mode: Google sign-in for one Workspace domain, exports from a published catalog.

python3 -m mjviewer --hosted takes its whole configuration from the environment
(README.md, "Hosted mode"; API.md, "Hosted mode"). Compared with local mode:

* Every page, /api/* and /files/* request needs a session: an HMAC-signed
  __Host-mjv_session cookie, set after a Google sign-in whose id token names
  the configured Workspace domain both as its hd claim and as its email domain.
  A browser that opens a page without one gets a small page whose script starts
  the sign-in with the whole address, #view included, so it comes back there.
* POST requests also need X-MJV-Token (an HMAC of the session, written into
  index.html) and an Origin header equal to MJV_PUBLIC_URL's origin.
* The Host header must name MJV_PUBLIC_URL's host, except on /healthz and the
  platform's start probe, which carry no data.
* Exports come only from the published catalog (catalog.py). There is no
  upload, no opening by path and no library, and no file path is shown.
* Everyone signed in sees every published export. With MJV_CATALOG_WRITE_URL
  set, an export's owner (the email recorded when it was published) or an
  administrator (MJV_ADMIN_EMAILS) can delete it: it leaves catalog.json and
  its ZIP is deleted from storage. Without it nothing is ever written there.

The OAuth client secret, the signing secret and the storage query strings are
held as Secret values: they never appear in a log line, a page or a response.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

from . import API_VERSION, __version__
from .catalog import (LOOPBACK, USER_AGENT, BlobStore, Catalog, CatalogError, CatalogUnavailable, Published,
                      ZipCache, describe, email_address, opener_for)
from .server import (DEFAULT_STATIC, PIL_IMAGE, Handler, ViewerApp, ViewerServer, default_cache_dir, dump_json)
from .sources import Limits

GOOGLE_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_TOKENINFO_URL = 'https://oauth2.googleapis.com/tokeninfo'
GOOGLE_ISSUERS = ('accounts.google.com', 'https://accounts.google.com')
SESSION_COOKIE = '__Host-mjv_session'
OAUTH_COOKIE = '__Host-mjv_oauth'
OAUTH_TTL_S = 600
HSTS = 'max-age=31536000; includeSubDomains'
PROBES = ('/healthz', '/robots933456.txt')
RESOLVE_WAIT_S = 20.0  # how long a request waits for an export already in the shared cache to open
MAINTENANCE_S = 60.0  # how often the catalog is re-read and unpublished exports are cleaned up
MAX_GOOGLE_BYTES = 256 * 1024
MAX_POST_BODY = 4096
DELETE_STATUS = {'delete_disabled': 403, 'not_owner': 403, 'unknown_catalog_entry': 404, 'catalog_busy': 409,
                 'storage_error': 502, 'delete_unconfirmed': 502}
_HOST_RE = re.compile(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?')
_DOMAIN_RE = re.compile(r'(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]')
_SIGNED_RE = re.compile(r'[A-Za-z0-9_-]{2,4000}\.[A-Za-z0-9_-]{43}')
_NEXT_RE = re.compile(r'/(?![/\\])[\x21-\x7e]{0,1023}')
_TOKEN_RE = re.compile(r'[A-Za-z0-9._-]{1,8192}')
_ERROR_CODE_RE = re.compile(r'[a-z_]{1,40}')
_COMMIT_RE = re.compile(r'[0-9A-Za-z._-]{1,64}')
_CLAIM_INT_RE = re.compile(r'[0-9]{1,12}')  # ASCII digits only: str.isdigit() also accepts superscripts


# ------------------------------------------------------------------ config

class Secret:
    """A configuration value that never shows itself in repr(), str() or a log line."""

    __slots__ = ('_value',)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return '<secret>'

    __str__ = __repr__


class ConfigError(Exception):
    def __init__(self, problems: List[str]):
        super().__init__('; '.join(problems))
        self.problems = problems


@dataclass(frozen=True)
class HostedConfig:
    public_url: str          # the origin users open, normalized: scheme://host[:port]
    hosts: frozenset         # Host header values that name it
    auth_secret: Secret
    allowed_domain: str
    client_id: str
    client_secret: Secret
    catalog_url: Secret
    cache_dir: str
    cache_warning: str = ''
    port: int = 8000
    cache_max_bytes: int = 50 * 1024 ** 3
    session_days: float = 30.0
    catalog_ttl_s: float = 60.0
    commit: str = 'unknown'
    google_auth_url: str = GOOGLE_AUTH_URL
    google_token_url: str = GOOGLE_TOKEN_URL
    google_tokeninfo_url: str = GOOGLE_TOKENINFO_URL
    catalog_write_url: Optional[Secret] = None  # None: deleting is turned off
    admin_emails: frozenset = frozenset()

    @property
    def redirect_uri(self) -> str:
        return self.public_url + '/auth/callback'


def _public_origin(value: str) -> Tuple[str, frozenset]:
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ('https', 'http'):
        raise ValueError('must start with https://')
    if parts.username is not None or parts.password is not None:
        raise ValueError('must not contain a user name or password')
    if parts.query or parts.fragment or parts.path not in ('', '/'):
        raise ValueError('must be an origin only (no path, query or fragment)')
    host = (parts.hostname or '').lower()
    try:
        port = parts.port
    except ValueError:
        raise ValueError('has an invalid port') from None
    if not _HOST_RE.fullmatch(host):
        raise ValueError('has an invalid host name')
    if parts.scheme == 'http' and host not in LOOPBACK:
        raise ValueError('must use https (http only for localhost or 127.0.0.1, in tests)')
    default = 443 if parts.scheme == 'https' else 80
    netloc = host if port in (None, default) else '%s:%d' % (host, port)
    return '%s://%s' % (parts.scheme, netloc), frozenset({netloc, '%s:%d' % (host, port or default)})


def _catalog_problems(value: str) -> List[str]:
    """Problems with MJV_CATALOG_URL. Never quotes the value: its query string is a credential."""
    if any(ord(ch) <= 32 or ord(ch) >= 127 for ch in value):
        return ['MJV_CATALOG_URL contains spaces, control or non-ASCII characters']
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or '').lower()
    problems = []
    if not (parts.scheme == 'https' or (parts.scheme == 'http' and host in LOOPBACK)):
        problems.append('MJV_CATALOG_URL must use https (http only for localhost or 127.0.0.1, in tests)')
    if not host:
        problems.append('MJV_CATALOG_URL has no host')
    if parts.username is not None or parts.password is not None or parts.fragment:
        problems.append('MJV_CATALOG_URL must not contain a user name, password or fragment')
    if not parts.query:
        problems.append('MJV_CATALOG_URL needs the read-only query string of the container '
                        '(for example a SAS with sp=rl)')
    for permissions in urllib.parse.parse_qs(parts.query).get('sp', []):
        if set(permissions) - set('rl'):
            problems.append('MJV_CATALOG_URL must be read-only: its sp parameter allows more than r and l')
    return problems


def _container(url: str) -> tuple:
    """(scheme, host, port, path) of a container URL, to tell whether two URLs name the same container."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or {'https': 443, 'http': 80}.get(parts.scheme)
    except ValueError:
        port = 'invalid'
    return parts.scheme, (parts.hostname or '').lower(), port, parts.path.rstrip('/')


def _write_url_problems(value: str, catalog: str) -> List[str]:
    """Problems with MJV_CATALOG_WRITE_URL. Never quotes the value: its query string is a credential."""
    if any(ord(ch) <= 32 or ord(ch) >= 127 for ch in value):
        return ['MJV_CATALOG_WRITE_URL contains spaces, control or non-ASCII characters']
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or '').lower()
    problems = []
    if not (parts.scheme == 'https' or (parts.scheme == 'http' and host in LOOPBACK)):
        problems.append('MJV_CATALOG_WRITE_URL must use https (http only for localhost or 127.0.0.1, in tests)')
    if parts.username is not None or parts.password is not None or parts.fragment:
        problems.append('MJV_CATALOG_WRITE_URL must not contain a user name, password or fragment')
    if catalog and _container(value) != _container(catalog):
        problems.append('MJV_CATALOG_WRITE_URL must name the same container as MJV_CATALOG_URL '
                        '(the same scheme, host and path)')
    permissions = urllib.parse.parse_qs(parts.query).get('sp', [])
    if len(permissions) != 1 or not set('wd') <= set(permissions[0]) or set(permissions[0]) - set('rwdl'):
        problems.append('MJV_CATALOG_WRITE_URL needs its own query string for the container (for example a SAS) '
                        'whose sp parameter allows write and delete (w and d) and nothing but r, w, d and l')
    return problems


def _admin_emails(value: str, domain: str, problems: List[str]) -> frozenset:
    """The addresses in MJV_ADMIN_EMAILS (separated by commas and/or spaces), in lower case. A bad one is
    reported by its position only."""
    found = set()
    for n, item in enumerate((part for part in re.split(r'[\s,]+', value) if part), 1):
        address = email_address(item)
        if address is None or (domain and address.rpartition('@')[2] != domain):
            problems.append('MJV_ADMIN_EMAILS: entry %d is not an email address at %s'
                            % (n, domain or 'MJV_ALLOWED_DOMAIN'))
        else:
            found.add(address)
    return frozenset(found)


def _loopback_url(value: str) -> bool:
    parts = urllib.parse.urlsplit(value)
    return parts.scheme in ('http', 'https') and (parts.hostname or '').lower() in LOOPBACK and not parts.fragment


def writable(folder: str) -> bool:
    """True when folder exists (or can be made) and a file can be created and deleted in it."""
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, '.write-test-%s' % secrets.token_hex(4))
        with open(probe, 'wb'):
            pass
        os.unlink(probe)
        return True
    except OSError:
        return False


def hosted_cache_dir(base: str = '/home') -> Tuple[str, str]:
    """(cache dir, warning). <base>/mjv-cache when it can be written (an existing writable
    mjv-cache folder, or a writable base to create it in: a shared, persistent mount on the
    platform), else ~/.cache/mj-export-viewer. The warning names the fallback for the log."""
    target = os.path.join(base, 'mjv-cache')
    if (os.path.isdir(target) or (os.path.isdir(base) and os.access(base, os.W_OK))) and writable(target):
        return target, ''
    fallback = default_cache_dir()
    return fallback, '%s is not writable; caching in %s, which other instances do not share' % (base, fallback)


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float,
            problems: List[str], integer: bool = False) -> float:
    raw = (env.get(name) or '').strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:  # NaN fails the range test too
        problems.append('%s must be a %s from %g to %g' % (name, 'whole number' if integer else 'number', low, high))
        return default
    return value


def load_config(env: Mapping[str, str], cache_base: str = '/home') -> HostedConfig:
    """Read and check the hosted settings; ConfigError lists every problem without any secret."""
    problems: List[str] = []

    def required(name: str) -> str:
        value = (env.get(name) or '').strip()
        if not value:
            problems.append('%s is not set' % name)
        return value

    public, hosts = '', frozenset()
    raw_public = required('MJV_PUBLIC_URL')
    if raw_public:
        try:
            public, hosts = _public_origin(raw_public)
        except ValueError as exc:
            problems.append('MJV_PUBLIC_URL %s' % exc)
    secret = required('MJV_AUTH_SECRET')
    if secret and (len(secret) < 32 or len(set(secret)) < 10):
        problems.append('MJV_AUTH_SECRET is too weak: use at least 32 random characters')
    domain = required('MJV_ALLOWED_DOMAIN').lower()
    domain_ok = bool(_DOMAIN_RE.fullmatch(domain))
    if domain and not domain_ok:
        problems.append('MJV_ALLOWED_DOMAIN must be a domain name such as example.com')
    client_id = required('GOOGLE_CLIENT_ID')
    if client_id and not _TOKEN_RE.fullmatch(client_id):
        problems.append('GOOGLE_CLIENT_ID contains characters a client id does not have')
    client_secret = required('GOOGLE_CLIENT_SECRET')
    if client_secret and any(ord(ch) <= 32 or ord(ch) >= 127 for ch in client_secret):
        problems.append('GOOGLE_CLIENT_SECRET contains spaces, control or non-ASCII characters')
    catalog = required('MJV_CATALOG_URL')
    if catalog:
        problems.extend(_catalog_problems(catalog))
    write = (env.get('MJV_CATALOG_WRITE_URL') or '').strip()
    if write:
        problems.extend(_write_url_problems(write, catalog))
    admins = _admin_emails(env.get('MJV_ADMIN_EMAILS') or '', domain if domain_ok else '', problems)
    port = int(_number(env, 'PORT', 8000, 1, 65535, problems, integer=True))
    max_gb = _number(env, 'MJV_CACHE_MAX_GB', 50, 0.001, 1_000_000, problems)
    days = _number(env, 'MJV_SESSION_DAYS', 30, 0.01, 366, problems)
    ttl = _number(env, 'MJV_CATALOG_TTL_S', 60, 0, 86400, problems)
    commit = (env.get('MJV_COMMIT') or '').strip()
    commit = commit if _COMMIT_RE.fullmatch(commit) else 'unknown'
    google = {}
    for name, key, default in (('MJV_GOOGLE_AUTH_URL', 'google_auth_url', GOOGLE_AUTH_URL),
                               ('MJV_GOOGLE_TOKEN_URL', 'google_token_url', GOOGLE_TOKEN_URL),
                               ('MJV_GOOGLE_TOKENINFO_URL', 'google_tokeninfo_url', GOOGLE_TOKENINFO_URL)):
        value = (env.get(name) or '').strip()
        if value and not _loopback_url(value):
            problems.append('%s is for tests only and must point at localhost or 127.0.0.1' % name)
        google[key] = value or default
    if problems:
        raise ConfigError(problems)
    cache_dir, warning = (os.path.abspath(os.path.expanduser(env['MJV_CACHE_DIR'].strip())), '') \
        if (env.get('MJV_CACHE_DIR') or '').strip() else hosted_cache_dir(cache_base)
    return HostedConfig(public_url=public, hosts=hosts, auth_secret=Secret(secret), allowed_domain=domain,
                        client_id=client_id, client_secret=Secret(client_secret), catalog_url=Secret(catalog),
                        cache_dir=cache_dir, cache_warning=warning, port=port,
                        cache_max_bytes=int(max_gb * 1024 ** 3), session_days=days, catalog_ttl_s=ttl,
                        commit=commit, catalog_write_url=Secret(write) if write else None, admin_emails=admins,
                        **google)


# ----------------------------------------------------------------- signing

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _mac(secret: Secret, purpose: str, text: str) -> str:
    message = ('mjv/%s\0%s' % (purpose, text)).encode('utf-8')
    return _b64e(hmac.new(secret.reveal().encode('utf-8'), message, hashlib.sha256).digest())


def sign(secret: Secret, purpose: str, payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8'))
    return '%s.%s' % (body, _mac(secret, purpose, body))


def unsign(secret: Secret, purpose: str, token, now: float) -> Optional[dict]:
    """The payload of a token signed for this purpose and not expired, else None."""
    if not isinstance(token, str) or not _SIGNED_RE.fullmatch(token):
        return None
    body, _, mac = token.rpartition('.')
    if not hmac.compare_digest(mac.encode('ascii'), _mac(secret, purpose, body).encode('ascii')):
        return None
    try:
        payload = json.loads(_b64d(body).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = payload.get('exp') if isinstance(payload, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= now:
        return None
    return payload


def _email_ok(email, domain: str) -> bool:
    if not isinstance(email, str) or len(email) > 254 or email.count('@') != 1:
        return False
    local, _, host = email.lower().partition('@')
    return bool(local) and host == domain and not any(ord(ch) <= 32 or ord(ch) == 127 for ch in email)


def issue_session(config: HostedConfig, email: str, now: float) -> str:
    return sign(config.auth_secret, 'session',
                {'email': email, 'iat': int(now), 'exp': int(now + config.session_days * 86400)})


def read_session(config: HostedConfig, value, now: float) -> Optional[str]:
    """The signed-in email, or None for a missing, forged, expired or foreign-domain session."""
    data = unsign(config.auth_secret, 'session', value, now)
    if data is None or not _email_ok(data.get('email'), config.allowed_domain):
        return None
    iat = data.get('iat')
    if not isinstance(iat, int) or isinstance(iat, bool) or iat > now + 300 \
            or now - iat > config.session_days * 86400:
        return None
    return data['email'].lower()


def csrf_token(config: HostedConfig, session_value: str) -> str:
    return _mac(config.auth_secret, 'csrf', session_value)


def cookie_values(headers, name: str) -> List[str]:
    """Every value of the named cookie across all Cookie headers (more than one is suspicious)."""
    found = []
    for header in headers.get_all('Cookie') or []:
        for part in header.split(';'):
            key, sep, value = part.strip().partition('=')
            if sep and key.strip() == name:
                found.append(value.strip())
    return found


def set_cookie(name: str, value: str, max_age: int) -> str:
    # __Host- cookies must be Secure with Path=/ and no Domain, also when they are cleared.
    return '%s=%s; Max-Age=%d; Path=/; Secure; HttpOnly; SameSite=Lax' % (name, value, max_age)


def safe_next(value) -> str:
    """A same-origin path to return to after sign-in; anything else becomes '/'."""
    if isinstance(value, str) and _NEXT_RE.fullmatch(value) and '\\' not in value:
        return value
    return '/'


# ------------------------------------------------------------------ Google

class AuthError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail  # for the log only; never shown to the browser


class GoogleAuth:
    """Authorization-code sign-in with Google, checked server-side (tokeninfo, hd and email domain)."""

    def __init__(self, config: HostedConfig, timeout: float = 15.0):
        self.config = config
        self.timeout = timeout

    def authorize_url(self, state: str, challenge: str) -> str:
        cfg = self.config
        return cfg.google_auth_url + '?' + urllib.parse.urlencode({
            'client_id': cfg.client_id, 'redirect_uri': cfg.redirect_uri, 'response_type': 'code',
            'scope': 'openid email profile', 'hd': cfg.allowed_domain, 'prompt': 'select_account',
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})

    def _call(self, url: str, form: Optional[dict] = None) -> Tuple[int, Optional[dict]]:
        headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode('ascii')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with opener_for(url).open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read(MAX_GOOGLE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_GOOGLE_BYTES + 1)
            except Exception:
                raw = b''
            exc.close()
        except Exception as exc:
            raise AuthError('unreachable', 'Google unreachable (%s)' % describe(exc)) from None
        try:
            parsed = json.loads(raw.decode('utf-8')) if 0 < len(raw) <= MAX_GOOGLE_BYTES else None
        except (UnicodeDecodeError, ValueError):
            parsed = None
        return status, parsed if isinstance(parsed, dict) else None

    def exchange(self, code: str, verifier: str) -> str:
        """The id token for an authorization code."""
        cfg = self.config
        status, data = self._call(cfg.google_token_url, {
            'code': code, 'client_id': cfg.client_id, 'client_secret': cfg.client_secret.reveal(),
            'redirect_uri': cfg.redirect_uri, 'grant_type': 'authorization_code', 'code_verifier': verifier})
        if status != 200:
            error = data.get('error') if data else None
            raise AuthError('token_exchange', 'token endpoint answered HTTP %d%s' % (
                status, ' (%s)' % error if isinstance(error, str) and _ERROR_CODE_RE.fullmatch(error) else ''))
        token = data.get('id_token') if data else None
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise AuthError('token_exchange', 'token endpoint sent no usable id token')
        return token

    def claims(self, id_token: str) -> dict:
        """The id token's claims as validated by Google's tokeninfo endpoint."""
        status, data = self._call(self.config.google_tokeninfo_url + '?' + urllib.parse.urlencode({'id_token': id_token}))
        if status != 200 or data is None:
            raise AuthError('tokeninfo', 'tokeninfo answered HTTP %d' % status)
        return data


def _int_claim(value) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _CLAIM_INT_RE.fullmatch(value):
        return int(value)
    return None


def check_claims(claims: dict, config: HostedConfig, now: float) -> Tuple[Optional[str], str]:
    """(email, '') when the token allows this account, else (None, reason)."""
    domain = config.allowed_domain
    if claims.get('aud') != config.client_id:
        return None, 'the sign-in was issued for another application'
    if claims.get('iss') not in GOOGLE_ISSUERS:
        return None, 'the sign-in was not issued by Google'
    verified = claims.get('email_verified')
    if not (verified is True or verified == 'true'):  # never 1 or 1.0, which compare equal to True
        return None, 'the account email address is not verified'
    hd = claims.get('hd')
    if not isinstance(hd, str) or hd.lower() != domain:
        return None, 'the account is not part of the %s Workspace' % domain
    if not _email_ok(claims.get('email'), domain):
        return None, 'the account email address is not at %s' % domain
    exp = _int_claim(claims.get('exp'))
    if exp is None or exp <= now:
        return None, 'the sign-in has expired'
    return claims['email'].lower(), ''


# ------------------------------------------------------------------- pages

_PAGE = '''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{title} · MJ Export Viewer</title>
<link rel="stylesheet" href="/static/app.css">
<script src="/static/theme.js"></script>{head}
</head>
<body class="auth-page">
<main class="auth" id="main">
  <div class="auth__card">
    <p class="auth__brand">
      <svg class="brand__mark" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" focusable="false">
        <rect class="brand__tile" x="2" y="2" width="9" height="9" rx="2"></rect>
        <rect class="brand__tile brand__tile--soft" x="13" y="2" width="9" height="9" rx="2"></rect>
        <rect class="brand__tile brand__tile--soft" x="2" y="13" width="9" height="9" rx="2"></rect>
        <rect class="brand__tile" x="13" y="13" width="9" height="9" rx="2"></rect>
      </svg>
      <span>MJ Export Viewer</span>
    </p>
    <h1 class="auth__title">{title}</h1>
    <p class="auth__text">{message}</p>
    <p class="auth__actions"><a class="btn btn--primary" href="{href}">{link}</a></p>
  </div>
</main>
</body>
</html>
'''


def render_page(title: str, message: str, link: str = 'Sign in again', href: str = '/auth/login',
                head: str = '') -> bytes:
    """head: trusted markup for <head> (a script of our own), never user input."""
    return _PAGE.format(title=html.escape(title), message=html.escape(message), link=html.escape(link),
                        href=html.escape(href), head=head).encode('utf-8')


def _params(query: str) -> dict:
    try:
        parsed = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=32)
    except ValueError:
        return {}
    return {key: values[0] for key, values in parsed.items()}


def _is_page(path: str) -> bool:
    if path in ('/', '/index.html'):
        return True
    return not path.startswith(('/api/', '/files/', '/static/', '/auth/')) and '.' not in path.rsplit('/', 1)[-1]


# ----------------------------------------------------------------- handler

class HostedHandler(Handler):
    """Sign-in, sessions and the catalog in front of the local handler's export and file routes."""

    user: Optional[str] = None
    session_value: Optional[str] = None

    @property
    def config(self) -> HostedConfig:
        return self.server.app.config

    def end_headers(self):
        self.send_header('Strict-Transport-Security', HSTS)
        super().end_headers()

    def _send(self, status: int, body: bytes, content_type: str, headers=()) -> None:
        self._send_bytes(status, body, content_type, 'no-store', list(headers))

    def _redirect(self, location: str, cookies=()) -> None:
        self._send(302, b'', 'text/plain; charset=utf-8',
                   [('Location', location)] + [('Set-Cookie', c) for c in cookies])

    def _page(self, status: int, title: str, message: str, link: str = 'Sign in again',
              href: str = '/auth/login', cookies=(), head: str = '') -> None:
        self._send(status, render_page(title, message, link, href, head), 'text/html; charset=utf-8',
                   [('Set-Cookie', c) for c in cookies])

    # ------------------------------------------------------------ dispatch

    def _dispatch(self, method: str) -> None:
        self._sent_headers = False
        self.user = self.session_value = None
        if method != 'GET' or self._has_body():
            # An unread request body (a GET may declare one too) must never be parsed as the next request.
            self.close_connection = True
        try:
            target = self._target()
            if target is None:
                return self._error(400, 'bad_request', 'invalid request target', close=True)
            path, query = target
            if path in PROBES:  # health and the platform's start probe: no session, no Host check, no data
                return self._probe(path) if method == 'GET' else self._method_not_allowed()
            if not self._host_allowed():
                return self._error(403, 'forbidden_host', 'requests must use the address in MJV_PUBLIC_URL', close=True)
            if path == '/auth/login':
                return self._login(query) if method == 'GET' else self._method_not_allowed()
            if path == '/auth/callback':
                return self._callback(query) if method == 'GET' else self._method_not_allowed()
            if path == '/auth/logout':
                return self._logout()
            if path.startswith('/static/'):
                return self._static(path[len('/static/'):]) if method == 'GET' else self._method_not_allowed()
            if path == '/favicon.ico':
                return self._send(204, b'', 'text/plain')
            values = cookie_values(self.headers, SESSION_COOKIE)
            if len(values) > 1:
                return self._error(401, 'duplicate_session', 'more than one session cookie: sign out, then sign in again',
                                   close=method != 'GET')
            email = read_session(self.config, values[0], time.time()) if values else None
            if email is None:
                if method == 'GET' and _is_page(path):
                    return self._sign_in_first(path + ('?' + query if query else ''))
                return self._error(401, 'unauthenticated', 'sign in to use this viewer', close=method != 'GET')
            self.user, self.session_value = email, values[0]
            if method != 'GET':
                if not self._token_ok():
                    return self._error(403, 'forbidden', 'missing or invalid X-MJV-Token header', close=True)
                if not self._origin_ok():
                    return self._error(403, 'forbidden_origin', 'cross-origin request refused', close=True)
            if path in ('/', '/index.html'):
                return self._index() if method == 'GET' else self._method_not_allowed()
            if path.startswith('/api/'):
                return self._api(method, path, query)
            if path.startswith('/files/'):
                return self._files(path, query) if method == 'GET' else self._method_not_allowed()
            if method == 'GET' and '.' not in path.rsplit('/', 1)[-1]:
                return self._index()  # client-side routes
            return self._error(404, 'not_found', 'not found')
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except CatalogUnavailable as exc:  # the list and every export route, while catalog.json cannot be read
            if self._sent_headers:
                self.close_connection = True
            else:
                self._error(503, exc.code, exc.message)
        except Exception:
            # Scrubbed, in case an unexpected error ever carries the storage query string.
            self.app.log(self.app.store.scrub(traceback.format_exc(limit=5)).rstrip())
            if self._sent_headers:
                self.close_connection = True
            else:
                self._error(500, 'internal', 'internal error; see the server log', close=True)

    # ------------------------------------------------------------- security

    def _host_allowed(self) -> bool:
        return (self.headers.get('Host') or '').strip().lower() in self.config.hosts

    def _token_ok(self) -> bool:
        given = (self.headers.get('X-MJV-Token') or '').strip().encode('utf-8', 'replace')
        return hmac.compare_digest(given, csrf_token(self.config, self.session_value).encode('ascii'))

    def _origin_ok(self) -> bool:
        # Compared with the configured origin, never with Host or X-Forwarded-* headers.
        return (self.headers.get('Origin') or '').strip().lower() == self.config.public_url

    # ---------------------------------------------------------------- probes

    def _probe(self, path: str) -> None:
        if path == '/healthz':
            catalog = dict(self.app.catalog.health(), delete_enabled=self.app.delete_enabled,
                           zip_deletes_pending=self.app.published.zip_deletes_pending())
            return self._send(200, dump_json({'ok': True, 'mode': 'hosted', 'commit': self.config.commit,
                                              'catalog': catalog, 'cache': {'ok': self.app.cache_ok}}),
                              'application/json; charset=utf-8')
        return self._send(200, b'User-agent: *\nDisallow: /\n', 'text/plain; charset=utf-8')

    # ------------------------------------------------------------------ auth

    def _sign_in_first(self, target: str) -> None:
        """A page without a session. A browser gets a page whose script sends it to /auth/login with the
        whole address, #view included (the server never sees it, so a redirect from here would drop it);
        other clients get the plain redirect."""
        login = '/auth/login?next=' + urllib.parse.quote(target, safe='')
        if 'text/html' not in (self.headers.get('Accept') or ''):
            return self._redirect(login)
        return self._page(200, 'Signing in', 'Taking you to the sign-in page…', link='Sign in', href=login,
                          head='\n<script src="/static/signin.js"></script>')

    def _login(self, query: str) -> None:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)  # PKCE: the code is useless without it
        challenge = _b64e(hashlib.sha256(verifier.encode('ascii')).digest())
        cookie = sign(self.config.auth_secret, 'oauth', {
            'state': state, 'verifier': verifier, 'next': safe_next(_params(query).get('next')),
            'exp': int(time.time()) + OAUTH_TTL_S})
        return self._redirect(self.app.google.authorize_url(state, challenge),
                              [set_cookie(OAUTH_COOKIE, cookie, OAUTH_TTL_S)])

    def _callback(self, query: str) -> None:
        params = _params(query)
        clear = set_cookie(OAUTH_COOKIE, '', 0)
        if 'error' in params:
            self.app.log('auth: Google ended the sign-in without a code')
            return self._page(401, 'Sign-in cancelled', 'Google did not complete the sign-in. You can try again.',
                              cookies=[clear])
        now = time.time()
        values = cookie_values(self.headers, OAUTH_COOKIE)
        data = unsign(self.config.auth_secret, 'oauth', values[0], now) if len(values) == 1 else None
        state = params.get('state') or ''
        expected = data.get('state') if data else None
        if not isinstance(expected, str) or not hmac.compare_digest(state.encode('utf-8', 'replace'),
                                                                    expected.encode('utf-8', 'replace')):
            self.app.log('auth: sign-in state missing, expired or not matching')
            return self._page(400, 'Sign-in expired', 'This sign-in was not started in this browser, '
                              'or it took longer than 10 minutes. Start it again.', cookies=[clear])
        code = params.get('code') or ''
        if not code or len(code) > 4096:
            return self._page(400, 'Sign-in failed', 'Google sent no sign-in code. Start the sign-in again.',
                              cookies=[clear])
        try:
            claims = self.app.google.claims(self.app.google.exchange(code, str(data.get('verifier') or '')))
        except AuthError as exc:
            self.app.log('auth: %s' % exc.detail)
            return self._page(502, 'Sign-in failed', 'Google could not confirm the sign-in. Try again in a moment.',
                              cookies=[clear])
        try:
            email, reason = check_claims(claims, self.config, now)
        except Exception as exc:  # a claim value nothing anticipated: refuse, like any other bad answer
            self.app.log('auth: unusable tokeninfo answer (%s)' % type(exc).__name__)
            return self._page(502, 'Sign-in failed', 'Google could not confirm the sign-in. Try again in a moment.',
                              cookies=[clear])
        if email is None:
            self.app.log('auth: account refused (%s)' % reason)
            return self._page(403, 'Access refused', 'This viewer is for %s accounts only: %s. '
                              'Sign in with another account.' % (self.config.allowed_domain, reason),
                              link='Use another account', cookies=[clear])
        self.app.log('auth: signed in %s' % email)
        session = issue_session(self.config, email, now)
        return self._redirect(safe_next(data.get('next')),
                              [set_cookie(SESSION_COOKIE, session, int(self.config.session_days * 86400)), clear])

    def _logout(self) -> None:
        return self._page(200, 'Signed out', 'You are signed out of MJ Export Viewer in this browser.', link='Sign in',
                          cookies=[set_cookie(SESSION_COOKIE, '', 0), set_cookie(OAUTH_COOKIE, '', 0)])

    # ----------------------------------------------------------------- pages

    def _index(self) -> None:
        try:
            with open(os.path.join(self.app.static_dir, 'index.html'), 'r', encoding='utf-8') as handle:
                page = handle.read()
        except OSError:
            return self._error(503, 'ui_missing', 'static/index.html is missing')
        body = page.replace('{{MJV_TOKEN}}', csrf_token(self.config, self.session_value)) \
            .replace('{{MJV_MODE}}', 'hosted').encode('utf-8')
        return self._send(200, body, 'text/html; charset=utf-8')

    # ------------------------------------------------------------------- api

    def _api(self, method: str, path: str, query: str) -> None:
        parts = [urllib.parse.unquote(p, errors='replace') for p in path.split('/')[2:]]
        if parts and parts[-1] == '' and len(parts) > 1:
            parts = parts[:-1]
        if parts == ['health']:
            if method != 'GET':
                return self._method_not_allowed()
            return self._json(200, {'ok': True, 'api': API_VERSION, 'version': __version__,
                                    'thumbnails': self.app.thumbs.available, 'max_upload_bytes': 0,
                                    'mode': 'hosted', 'user': self.user, 'delete_enabled': self.app.delete_enabled,
                                    'can_delete_any': self.app.delete_enabled and self._admin()})
        if parts == ['exports']:
            if method != 'GET':
                return self._method_not_allowed()
            items, status = self.app.published.listing()
            catalog = {key: status[key] for key in ('ok', 'updated_at', 'age_s', 'error', 'skipped')}
            catalog['items'] = [self._for_user(item) for item in items]
            return self._json(200, {'exports': [s for item in items for s in item['exports']],
                                    'library': [], 'failed': [], 'catalog': catalog})
        if parts in (['exports', 'open'], ['exports', 'upload']):
            return self._error(404, 'not_available', 'not available in hosted mode: exports are published to the catalog')
        if parts and parts[0] == 'catalog':
            return self._catalog(method, parts)
        return super()._api(method, path, query)

    def _catalog(self, method: str, parts: List[str]) -> None:
        if len(parts) == 2:
            if method != 'GET':
                return self._method_not_allowed()
            item = self.app.published.item(parts[1])
        elif len(parts) == 3 and parts[2] == 'open':
            if method != 'POST':
                return self._method_not_allowed()
            options = self._body_object('{"retry": true}')
            if options is None:
                return None
            item = self.app.published.open(parts[1], retry=options.get('retry') is True)
        elif len(parts) == 3 and parts[2] == 'delete':
            return self._delete(parts[1]) if method == 'POST' else self._method_not_allowed()
        else:
            return self._error(404, 'not_found', 'unknown API route')
        if item is None:
            return self._error(404, 'unknown_catalog_entry', 'no published export with this id')
        return self._json(202 if method == 'POST' and item['state'] not in ('ready', 'failed') else 200,
                          self._for_user(item))

    def _delete(self, catalog_id: str) -> None:
        """POST /api/catalog/<id>/delete with {"confirm": "<id>"}: for the export's owner or an administrator."""
        if not self.app.delete_enabled:
            return self._error(403, 'delete_disabled', 'deleting exports is not turned on for this viewer')
        body = self._body_object('{"confirm": "<id>"}', required=True)
        if body is None:
            return None
        if body.get('confirm') != catalog_id:
            return self._error(400, 'bad_request', 'confirm the deletion with the id of the export: {"confirm": "<id>"}')
        try:
            self.app.published.delete(catalog_id, self.user, admin=self._admin())
        except CatalogError as exc:
            code = exc.code if exc.code in DELETE_STATUS else 'storage_error'
            return self._error(DELETE_STATUS[code], code, self.app.store.scrub(exc.message))
        return self._json(200, {'deleted': catalog_id})

    def _admin(self) -> bool:
        return self.user in self.config.admin_emails

    def _for_user(self, item: dict) -> dict:
        """A catalog item, plus whether the signed-in user may delete it."""
        mine = item.get('owner') is not None and item.get('owner') == self.user
        return dict(item, can_delete=self.app.delete_enabled and (mine or self._admin()))

    def _body_object(self, example: str, required: bool = False) -> Optional[dict]:
        """The small JSON object a POST sends ({} without a body, unless one is required); None once an
        error has been answered."""
        length = (self.headers.get('Content-Length') or '').strip()
        if not length or length == '0':
            return self._error(400, 'bad_request', 'body must be a JSON object such as %s' % example) \
                if required else {}
        if not length.isascii() or not length.isdigit() or int(length) > MAX_POST_BODY:
            return self._error(413, 'too_large', 'request body is too large', close=True)
        try:
            value = json.loads(self.rfile.read(int(length)).decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            value = None
        if not isinstance(value, dict):
            return self._error(400, 'bad_request', 'body must be a JSON object such as %s' % example)
        return value

    def _export_or_404(self, export_id: str):
        index, item = self.app.published.resolve(export_id, RESOLVE_WAIT_S)
        if index is None:
            if item is None:
                self._error(404, 'unknown_export', 'no published export with this id')
            else:
                self._json(409, {'error': {'code': 'export_not_ready',
                                           'message': 'this export is not open yet; open it from the start page'},
                                 'catalog': self._for_user(item)})
        return index


# --------------------------------------------------------------------- app

class HostedApp(ViewerApp):
    handler_class = HostedHandler

    def __init__(self, config: HostedConfig, static_dir: str = DEFAULT_STATIC, thumbnails: bool = True,
                 limits: Limits = Limits(), pil=PIL_IMAGE, log=None):
        super().__init__(static_dir=static_dir, cache_dir=config.cache_dir, thumbnails=thumbnails,
                         max_upload_bytes=0, limits=limits, pil=pil, log=log)
        self.config = config
        self.google = GoogleAuth(config)
        self.store = BlobStore(config.catalog_url.reveal(), write_url=config.catalog_write_url.reveal()
                               if config.catalog_write_url else None)
        self.catalog = Catalog(self.store, config.catalog_ttl_s, log=self.log)
        self.cache = ZipCache(self.cache_dir, config.cache_max_bytes, log=self.log)
        self.published = Published(self.catalog, self.cache, self.registry, self.thumbs.dir, log=self.log)
        self.cache_ok = True
        self._stopping = threading.Event()
        self._maintainer: Optional[threading.Thread] = None

    @property
    def delete_enabled(self) -> bool:
        """MJV_CATALOG_WRITE_URL is set: owners and administrators can delete exports."""
        return self.store.writable

    def check_cache(self) -> bool:
        """Whether downloads and thumbnails can be written (/healthz reports it)."""
        ok = writable(self.cache.dir) and writable(self.thumbs.dir)
        if not ok and self.cache_ok:
            self.log('cache: %s is not writable: published exports cannot be opened' % self.cache_dir)
        self.cache_ok = ok
        return ok

    def start(self) -> None:
        """Maintenance in the background: a first pass at once (it reads the catalog and removes downloads
        abandoned by a stopped process), then every MAINTENANCE_S, also while nobody uses the viewer."""
        self._maintainer = threading.Thread(target=self._maintain_loop, name='mjv-maintenance', daemon=True)
        self._maintainer.start()

    def maintain(self) -> None:
        """One maintenance pass: re-read the catalog when stale, check the cache, tidy it."""
        self.check_cache()
        self.published.maintain()

    def _maintain_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.maintain()
            except Exception:  # never let maintenance die; scrubbed like any traceback here
                self.log(self.store.scrub(traceback.format_exc(limit=5)).rstrip())
            self._stopping.wait(MAINTENANCE_S)

    def close(self) -> None:
        self._stopping.set()
        self.published.close()  # waits briefly, so downloads remove their temporary files and locks
        super().close()


class HostedServer(ViewerServer):
    request_queue_size = 128  # a page of thumbnails from several viewers arrives at once


def run(no_thumbnails: bool = False, env: Optional[Mapping[str, str]] = None, out=None, err=None) -> int:
    """python3 -m mjviewer --hosted: refuse to start (exit 2) unless the configuration is complete."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        config = load_config(os.environ if env is None else env)
    except ConfigError as exc:
        print('Hosted mode cannot start:', file=err)
        for problem in exc.problems:
            print('  - %s' % problem, file=err)
        print('See "Hosted mode" in the viewer README for every setting.', file=err, flush=True)
        return 2
    if config.cache_warning:
        print('Warning: %s' % config.cache_warning, file=err, flush=True)
    app = HostedApp(config, thumbnails=not no_thumbnails, log=err)
    try:
        server = HostedServer(('0.0.0.0', config.port), app, HostedHandler)
    except OSError as exc:
        print('Cannot listen on port %d: %s' % (config.port, exc.strerror or exc), file=err)
        app.close()
        return 2
    app.start()
    print('MJ Export Viewer %s (hosted) on port %d for %s; cache %s' % (
        __version__, server.port, config.public_url, app.cache_dir), file=out, flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)
    except (ValueError, OSError):  # not the main thread
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping.', file=out, flush=True)
    finally:
        server.server_close()
        app.close()
    return 0
