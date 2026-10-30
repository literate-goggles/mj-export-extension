"""python3 -m mjviewer --check PATH [--json]: validate one export ZIP or folder without serving it.

Exit status: 0 when every export root opened without errors, 1 when there are
errors (or nothing could be opened), 2 for a usage problem such as a missing
path. Nothing is written anywhere; the export is read exactly as the viewer
reads it, and every image file is also read to its end once, so that a ZIP
member whose CRC fails is reported as unreadable.
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, Optional

from .model import ExportIndex
from .sources import Limits, SourceError, display_name, open_source

COUNT_KEYS = ('jobs', 'moodboards', 'profiles', 'likes', 'images_expected', 'images_present', 'images_missing')


def _check_text(check: dict) -> str:
    detail = check.get('detail') or ''
    return '%s: %s' % (check['title'], detail) if detail else check['title']


def _root_report(index: ExportIndex) -> dict:
    summary = index.summary
    observed = index.overview()['counts']['observed']
    return {
        'root': index.root,
        'status': summary['status'],
        'extension_version': summary['extension_version'],
        'version_support': summary['version_support'],
        'export_time': summary['export_time'],
        'counts': {key: observed[key] for key in COUNT_KEYS},
        'errors': [_check_text(c) for c in index.checks if c['level'] == 'error'],
        'warnings': [_check_text(c) for c in index.checks if c['level'] == 'warning'],
    }


def check_path(path: str, limits: Limits = Limits()) -> dict:
    """The --json report for one ZIP or folder. Raises FileNotFoundError when nothing is there."""
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        raise FileNotFoundError(path)
    report = {'ok': False, 'path_name': os.path.basename(os.path.normpath(expanded)), 'exports': [], 'error': None}
    try:
        source = open_source(expanded, limits)
    except SourceError as exc:
        report['error'] = {'code': exc.code, 'message': exc.message}
        return report
    try:
        roots = source.roots()
        if not roots:
            report['error'] = {'code': 'no_export_root', 'message': 'no export_meta.json or jobs.jsonl found'}
            return report
        for root in roots:
            info = {'type': source.kind, 'name': source.name, 'path': None}
            try:
                index = ExportIndex(source.view(root), 'check', info, limits)
                index.verify_files()
            except Exception as exc:  # report it like the server would, then check the other roots
                report['exports'].append({
                    'root': root, 'status': None, 'extension_version': None, 'version_support': None,
                    'export_time': None, 'counts': {key: 0 for key in COUNT_KEYS},
                    'errors': ['could not read this export root (%s)' % type(exc).__name__], 'warnings': []})
                continue
            report['exports'].append(_root_report(index))
    finally:
        source.close()
    report['ok'] = all(not e['errors'] for e in report['exports'])
    return report


def _human(report: dict) -> List[str]:
    exports = report['exports']
    lines = ['%s: %s' % (display_name(report['path_name']), 'OK' if report['ok'] else 'ERRORS')]
    if report['error']:
        lines.append('  error: %s' % display_name(report['error']['message']))
    for e in exports:
        c = e['counts']
        lines.append('- %s: %s, extension %s (%s), exported %s' % (
            display_name(e['root'] or '(top of the archive)'), e['status'] or 'status not recorded',
            e['extension_version'] or '?', e['version_support'] or '?', e['export_time'] or 'at an unknown time'))
        lines.append('  %d jobs, %d moodboards, %d profiles, %d likes; images %d of %d present, %d missing' % (
            c['jobs'], c['moodboards'], c['profiles'], c['likes'], c['images_present'], c['images_expected'],
            c['images_missing']))
        for level in ('errors', 'warnings'):
            for text in e[level]:
                lines.append('  %s: %s' % (level[:-1], display_name(text, 400)))
    return lines


def run(path: str, as_json: bool = False, out=None, err=None, limits: Limits = Limits()) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        report = check_path(path, limits)
    except FileNotFoundError:
        print('--check: no file or folder at %s' % display_name(path), file=err)
        return 2
    if as_json:
        out.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    else:
        out.write('\n'.join(_human(report)) + '\n')
    out.flush()
    return 0 if report['ok'] else 1


def usage_problem(args) -> Optional[str]:
    """Why --check/--json cannot run with the other options given, or None."""
    if args.json and args.check is None:
        return '--json needs --check PATH'
    if args.check is not None and (args.paths or args.library or args.hosted or args.open):
        return '--check takes one PATH and cannot be combined with other paths, --library, --hosted or --open'
    return None
