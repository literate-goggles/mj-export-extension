"""Command line entry point: python3 -m mjviewer [PATH ...] [options].

Also: python3 -m mjviewer --hosted (configuration from the environment; see hosted.py)
and python3 -m mjviewer --check PATH [--json] (validate one export without serving it).
"""
from __future__ import annotations

import argparse
import signal
import sys
import webbrowser
from typing import List, Optional

from . import __version__, check
from .server import ViewerApp, default_cache_dir, make_server
from .sources import SourceError, display_name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python3 -m mjviewer',
        description='Local, read-only viewer for MJ Export folders and ZIP files. '
                    'Exports are never modified or extracted.')
    parser.add_argument('paths', nargs='*', metavar='PATH',
                        help='export ZIP or folder to open at start (several allowed)')
    parser.add_argument('--library', action='append', default=[], metavar='DIR',
                        help='folder whose *.zip files and export folders are offered for opening (repeatable)')
    parser.add_argument('--host', default='127.0.0.1', help='address to bind (default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8765, help='port to bind (default 8765, 0 = any free port)')
    parser.add_argument('--cache-dir', default=None, metavar='DIR',
                        help='thumbnail and upload cache (default %s)' % default_cache_dir())
    parser.add_argument('--no-thumbnails', action='store_true', help='always serve original image bytes')
    parser.add_argument('--allow-host', action='append', default=[], metavar='NAME',
                        help='extra Host header name to accept besides localhost (repeatable)')
    parser.add_argument('--max-upload-mb', type=int, default=4096, metavar='N',
                        help='largest ZIP accepted by the upload endpoint (default 4096)')
    parser.add_argument('--open', action='store_true', help='open the viewer in the default browser')
    parser.add_argument('--hosted', action='store_true',
                        help='hosted mode: Google sign-in and a published catalog, configured from the environment '
                             '(see the README)')
    parser.add_argument('--check', metavar='PATH', default=None,
                        help='validate one export ZIP or folder and exit (0 = no errors, 1 = errors, 2 = usage)')
    parser.add_argument('--json', action='store_true', help='with --check: print the report as JSON')
    parser.add_argument('--version', action='version', version='mjviewer %s' % __version__)
    return parser


HOSTED_DEFAULTS = {'host': '127.0.0.1', 'port': 8765, 'cache_dir': None, 'max_upload_mb': 4096}


def hosted_usage_problem(args) -> Optional[str]:
    """Hosted mode takes its settings from the environment and opens nothing by path."""
    given = [name for name in ('paths', 'library', 'allow_host', 'open') if getattr(args, name)]
    given += [name for name, default in HOSTED_DEFAULTS.items() if getattr(args, name) != default]
    if given:
        return '--hosted takes its settings from the environment and cannot be combined with %s' % ', '.join(
            'PATH' if name == 'paths' else '--' + name.replace('_', '-') for name in given)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check is not None or args.json:
        problem = check.usage_problem(args)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        return check.run(args.check, as_json=args.json)
    if args.hosted:
        problem = hosted_usage_problem(args)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        from . import hosted
        return hosted.run(no_thumbnails=args.no_thumbnails)
    if args.max_upload_mb < 1:
        print('--max-upload-mb must be at least 1', file=sys.stderr)
        return 2
    app = ViewerApp(cache_dir=args.cache_dir, thumbnails=not args.no_thumbnails,
                    allow_hosts=args.allow_host, max_upload_bytes=args.max_upload_mb * 1024 * 1024,
                    library_dirs=args.library, log=sys.stderr)
    for path in args.paths:
        problems = []
        try:
            indexes = app.registry.open(path, failures=problems)
        except SourceError as exc:
            print('Could not open %s: %s' % (display_name(path), exc.message), file=sys.stderr)
            app.registry.note_failed([{'path': path, 'code': exc.code, 'message': exc.message}])
            continue
        for problem in problems:
            print('Could not open %s in %s: %s' % (display_name(problem['root'] or '(top)'), display_name(path),
                                                   problem['message']), file=sys.stderr)
        app.registry.note_failed(problems)
        for index in indexes:
            s = index.summary
            c = s['counts']
            print('Opened %s: %d jobs, %d moodboards, %d profiles, %d/%d images present; %d warning(s), %d error(s)'
                  % (display_name(s['label'], 80), c['jobs'], c['moodboards'], c['profiles'],
                     c['images_present'], c['images_expected'], s['warnings'], s['errors']), flush=True)
    try:
        server = make_server(app, args.host, args.port)
    except OSError as exc:
        print('Cannot listen on %s:%d: %s' % (args.host, args.port, exc.strerror or exc), file=sys.stderr)
        app.close()
        return 2
    host = args.host
    shown = 'localhost' if host in ('127.0.0.1', '0.0.0.0', '::', '') else ('[%s]' % host if ':' in host else host)
    url = 'http://%s:%d/' % (shown, server.port)
    print('MJ Export Viewer %s at %s  (Ctrl+C to stop)' % (__version__, url))
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print('Note: bound to %s. Requests must still use localhost or a name given with --allow-host.' % host)
    print('Cache: %s' % app.cache_dir, flush=True)
    if args.open:
        webbrowser.open(url)

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, stop)
    except (ValueError, OSError):  # not the main thread
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping.', flush=True)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
