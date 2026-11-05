"""python3 -m mjviewer --check PATH [--json], on synthetic exports."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from mjviewer import check as check_module
from mjviewer.__main__ import main
from tests import fixtures as fx


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


class CheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mjv-check-')
        self.base = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_0_4_1_export_with_short_link_pictures(self):
        path = fx.write_zip(fx.short_link_export(), os.path.join(self.base, 'short.zip'))
        status, out, err = run('--check', path, '--json')
        self.assertEqual((status, err), (0, ''))
        root = json.loads(out)['exports'][0]
        self.assertEqual((root['extension_version'], root['version_support']), ('0.4.1', 'supported'))
        self.assertEqual((root['counts']['images_expected'], root['counts']['images_present'], root['counts']['images_missing']), (35, 35, 0))
        status, out, err = run('--check', path)
        self.assertEqual(status, 0)
        self.assertEqual([line.strip() for line in out.splitlines() if line.strip().startswith(('warning', 'error'))],
                         ['warning: Export completed with failures: See the failure list.',
                          'warning: 1 failure(s) reported by the exporter'])

    def test_valid_zip(self):
        path = fx.write_zip(fx.standard_export(), os.path.join(self.base, 'good.zip'))
        before = os.stat(path).st_mtime_ns
        status, out, err = run('--check', path, '--json')
        self.assertEqual((status, err), (0, ''))
        report = json.loads(out)
        self.assertEqual(set(report), {'ok', 'path_name', 'exports', 'error'})
        self.assertEqual((report['ok'], report['path_name'], report['error']), (True, 'good.zip', None))
        root = report['exports'][0]
        self.assertEqual(set(root), {'root', 'status', 'extension_version', 'version_support', 'export_time', 'counts',
                                     'selection', 'errors', 'warnings'})
        self.assertIsNone(root['selection'])  # the designer did not choose what to export
        self.assertEqual((root['root'], root['status'], root['extension_version'], root['version_support']),
                         (fx.ROOT, 'complete', '0.3.1', 'supported'))
        self.assertEqual(root['export_time'], '2026-03-20T12:00:00.000Z')
        self.assertEqual(root['counts'], {'jobs': 9, 'moodboards': 2, 'profiles': 2, 'likes': 1, 'images_expected': 33,
                                          'images_present': 33, 'images_missing': 0})
        self.assertEqual((root['errors'], root['warnings']), ([], []))
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        status, out, _ = run('--check', path)
        self.assertEqual(status, 0)
        self.assertIn('good.zip: OK', out)
        self.assertIn('9 jobs, 2 moodboards, 2 profiles, 1 likes; images 33 of 33 present, 0 missing', out)

    def test_a_selection_is_described_and_what_was_left_out_is_no_problem(self):
        path = fx.write_zip(fx.selection_export(), os.path.join(self.base, 'chosen.zip'))
        status, out, err = run('--check', path, '--json')
        self.assertEqual((status, err), (0, ''))
        root = json.loads(out)['exports'][0]
        sentence = ('The designer chose what to export: 2 of 4 moodboards and 10 of 25 generations. 13 generations and '
                    '2 moodboards were left out; 2 generations appeared after choosing and were not exported.')
        self.assertEqual(root['selection'], {
            'chosen_at': '2026-03-19T09:00:00.000Z', 'moodboards': {'available': 4, 'exported': 2, 'left_out': 2},
            'jobs': {'available': 25, 'exported': 10, 'left_out': 13, 'not_reviewed': 2}, 'text': sentence})
        self.assertNotIn('board-left', out)  # numbers only: the report names no moodboard
        self.assertEqual((root['extension_version'], root['version_support']), ('0.4.0', 'supported'))
        self.assertEqual((root['errors'], root['warnings']), ([], []))
        self.assertEqual(root['counts']['jobs'], 10)
        status, out, _ = run('--check', path)
        self.assertEqual(status, 0)
        self.assertIn('\n  ' + sentence + '\n', out)
        # A selection record this viewer cannot read is a warning, never an error.
        export = fx.selection_export()
        export.meta['selection'] = ['not', 'an', 'object']
        path = fx.write_zip(export, os.path.join(self.base, 'odd.zip'))
        status, out, _ = run('--check', path, '--json')
        root = json.loads(out)['exports'][0]
        self.assertEqual((status, root['selection'], root['errors']), (0, None, []))
        self.assertEqual(len(root['warnings']), 1)
        self.assertTrue(root['warnings'][0].startswith('Some selection details are unreadable or do not add up'),
                        root['warnings'])

    def test_errors_and_warnings_exit_1(self):
        export = fx.standard_export()
        export.meta = None  # an interrupted run: no export_meta.json
        export.skip_images.add(export.referenced()[1])
        path = fx.write_zip(export, os.path.join(self.base, 'partial.zip'))
        status, out, _ = run('--check', path, '--json')
        self.assertEqual(status, 1)
        report = json.loads(out)
        self.assertFalse(report['ok'])
        root = report['exports'][0]
        self.assertTrue(any(e.startswith('export_meta.json is absent') for e in root['errors']), root['errors'])
        self.assertTrue(root['warnings'])
        self.assertEqual(root['counts']['images_missing'], 1)
        status, out, _ = run('--check', path)
        self.assertEqual(status, 1)
        self.assertIn('partial.zip: ERRORS', out)
        self.assertIn('error: export_meta.json is absent', out)

    def test_several_roots_and_folders(self):
        members = {}
        for root in ('a', 'b'):
            for name, data in fx.standard_export().members().items():
                members['%s/%s' % (root, name)] = data
        path = os.path.join(self.base, 'two.zip')
        with open(path, 'wb') as handle:
            handle.write(fx.zip_bytes(members))
        status, out, _ = run('--check', path, '--json')
        self.assertEqual(status, 0)
        self.assertEqual([e['root'] for e in json.loads(out)['exports']], ['a', 'b'])
        folder = fx.write_folder(fx.standard_export(), os.path.join(self.base, 'folder'))
        status, out, _ = run('--check', folder, '--json')
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)['path_name'], os.path.basename(folder))

    def test_a_member_whose_crc_fails_at_its_end_is_reported(self):
        name = fx.ROOT + '/images/job-grid-a_0.png'  # a 640x480 PNG: its first bytes stay intact
        data = fx.flip_member_tail(fx.export_zip_bytes(fx.standard_export(), compression=zipfile.ZIP_STORED), name)
        path = os.path.join(self.base, 'damaged.zip')
        with open(path, 'wb') as handle:
            handle.write(data)
        status, out, _ = run('--check', path, '--json')
        root = json.loads(out)['exports'][0]
        self.assertEqual((root['counts']['images_present'], root['counts']['images_expected']), (32, 33))
        self.assertTrue(any(w.startswith('1 file(s) refused or unreadable') for w in root['warnings']), root['warnings'])
        self.assertEqual(status, 0)  # an unreadable image is a warning, as in the overview
        status, out, _ = run('--check', path)
        self.assertIn('images 32 of 33 present', out)

    def test_a_root_that_fails_to_load_is_an_error(self):
        members = {}
        for root in ('a', 'b'):
            for name, data in fx.standard_export().members().items():
                members['%s/%s' % (root, name)] = data
        path = os.path.join(self.base, 'two.zip')
        with open(path, 'wb') as handle:
            handle.write(fx.zip_bytes(members))
        build = check_module.ExportIndex

        def flaky(view, *args, **kwargs):
            if view.root == 'b':
                raise ValueError('a value nothing anticipated')
            return build(view, *args, **kwargs)

        with mock.patch.object(check_module, 'ExportIndex', side_effect=flaky):
            status, out, _ = run('--check', path, '--json')
        self.assertEqual(status, 1)
        report = json.loads(out)
        self.assertEqual([(e['root'], e['errors']) for e in report['exports']],
                         [('a', []), ('b', ['could not read this export root (ValueError)'])])
        self.assertFalse(report['ok'])

    def test_truncated_and_unreadable_zips_say_so(self):
        good = fx.export_zip_bytes(fx.standard_export())
        cut = os.path.join(self.base, 'cut.zip')
        with open(cut, 'wb') as handle:
            handle.write(good[:300])  # an interrupted copy: no end record
        status, out, _ = run('--check', cut, '--json')
        error = json.loads(out)['error']
        self.assertEqual((status, error['code']), (1, 'bad_zip'))
        self.assertIn('truncated', error['message'])
        if os.geteuid() == 0:
            self.skipTest('root can read anything')
        locked = os.path.join(self.base, 'locked.zip')
        with open(locked, 'wb') as handle:
            handle.write(good)
        os.chmod(locked, 0)
        try:
            status, out, _ = run('--check', locked, '--json')
        finally:
            os.chmod(locked, 0o644)
        error = json.loads(out)['error']
        self.assertEqual((status, error['code']), (1, 'unreadable'))
        self.assertIn('Permission denied', error['message'])
        self.assertNotIn(self.base, error['message'])

    def test_unreadable_sources_exit_1(self):
        not_zip = os.path.join(self.base, 'notes.zip')
        with open(not_zip, 'w') as handle:
            handle.write('not a zip')
        empty = fx.write_zip(fx.standard_export(), os.path.join(self.base, 'x.zip'))
        os.makedirs(os.path.join(self.base, 'empty-folder'))
        for path, code in ((not_zip, 'not_a_zip'), (os.path.join(self.base, 'empty-folder'), 'no_export_root')):
            with self.subTest(path=os.path.basename(path)):
                status, out, _ = run('--check', path, '--json')
                self.assertEqual(status, 1)
                report = json.loads(out)
                self.assertEqual((report['ok'], report['exports'], report['error']['code']), (False, [], code))
        self.assertTrue(os.path.exists(empty))

    def test_usage_problems_exit_2(self):
        path = fx.write_zip(fx.standard_export(), os.path.join(self.base, 'good.zip'))
        for argv in (['--check', os.path.join(self.base, 'missing.zip')], ['--json'], ['--check', path, path],
                     ['--check', path, '--hosted'], ['--check', path, '--library', self.base],
                     ['--check', path, '--open']):
            with self.subTest(argv=argv):
                status, out, err = run(*argv)
                self.assertEqual(status, 2)
                self.assertEqual(out, '')
                self.assertTrue(err)
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            main(['--check'])
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
