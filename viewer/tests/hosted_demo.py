"""Try the hosted mode on this computer, against the fake Google and the fake storage of the tests.

    cd viewer
    python3 -m tests.hosted_demo                       # two invented exports
    python3 -m tests.hosted_demo export.zip [...]      # publish ZIPs into a temporary fake container
    python3 -m tests.hosted_demo --store DIR           # serve a folder holding catalog.json and exports/<sha256>.zip

It prints the address to open. Signing in goes through tests/fake_google.py, which
approves the --email account at once (--chooser shows an account page first, like
Google's). Everything listens on 127.0.0.1 only, and nothing is sent anywhere else.
Ctrl+C stops it and deletes its temporary folder; a --store folder is only read.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import signal
import sys
import tempfile

from mjviewer.hosted import HostedApp, load_config
from mjviewer.server import make_server
from tests import fixtures as fx
from tests.fake_blob import FakeBlob
from tests.fake_google import FakeGoogle


def publish(blob: FakeBlob, path: str) -> dict:
    """Copy one ZIP into the fake container (streamed, not read into memory) and return its catalog entry."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    sha = digest.hexdigest()
    target = os.path.join(blob.root, 'exports', sha + '.zip')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(path, target)
    return {'id': sha[:16], 'blob': 'exports/%s.zip' % sha, 'name': os.path.basename(path), 'sha256': sha,
            'size': os.path.getsize(target), 'published_by': 'demo'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python3 -m tests.hosted_demo', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('zips', nargs='*', metavar='ZIP', help='export ZIPs to publish into a temporary fake container')
    parser.add_argument('--store', metavar='DIR', help='a folder with catalog.json and exports/ (read only)')
    parser.add_argument('--port', type=int, default=8780, help='port on 127.0.0.1 (default 8780)')
    parser.add_argument('--email', default='alice@example.com', help='the account the fake Google signs in')
    parser.add_argument('--chooser', action='store_true', help="show an account page first, as Google's does")
    args = parser.parse_args(argv)
    if args.store and args.zips:
        parser.error('give ZIPs or --store, not both')
    domain = args.email.rpartition('@')[2]
    with tempfile.TemporaryDirectory(prefix='mjv-demo-') as work:
        blob = FakeBlob(os.path.abspath(args.store) if args.store else os.path.join(work, 'blob'))
        google = FakeGoogle(email=args.email, hd=domain)
        google.chooser = args.chooser
        if not args.store:
            if args.zips:
                entries = [publish(blob, path) for path in args.zips]
            else:
                entries = [blob.put_export(fx.export_zip_bytes(fx.standard_export()), name='mj_export_demo_one.zip',
                                           label='Invented export'),
                           blob.put_export(fx.export_zip_bytes(fx.gallery_export(150)), name='mj_export_demo_two.zip')]
            blob.write_catalog(entries)
        env = {'MJV_PUBLIC_URL': 'http://127.0.0.1:%d' % args.port, 'MJV_AUTH_SECRET': secrets.token_urlsafe(32),
               'MJV_ALLOWED_DOMAIN': domain, 'MJV_CATALOG_URL': blob.url, 'MJV_CACHE_DIR': os.path.join(work, 'cache'),
               'MJV_CATALOG_TTL_S': '5'}
        env.update(google.env())
        app = HostedApp(load_config(env), log=sys.stderr)
        try:
            server = make_server(app, '127.0.0.1', args.port)
        except OSError as exc:
            print('Cannot listen on port %d: %s' % (args.port, exc.strerror or exc), file=sys.stderr)
            return 2
        app.start()
        print('Hosted mode (fake Google, fake storage) at http://127.0.0.1:%d/ signing in as %s  (Ctrl+C to stop)'
              % (args.port, args.email), flush=True)

        def stop(signum, frame):
            raise KeyboardInterrupt  # SIGTERM too: the temporary folder is deleted either way

        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('Stopping.', flush=True)
        finally:
            server.server_close()
            app.close()
            blob.stop()
            google.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
