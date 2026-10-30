"""ZIP and folder access: layouts, name safety, bombs, links and odd entries."""
import io
import os
import tempfile
import unittest

from mjviewer.model import ExportIndex
from mjviewer.sources import (FileStat, FolderSource, Limits, MemberError, RootView, SourceError, TooLarge,
                              ZipSource, detect_image_type, open_source, unsafe_reason)
from tests import fixtures as fx

ROOT = fx.ROOT


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mjv-src-')
        self.tmp = self._tmp.name
        self.sources = []

    def tearDown(self):
        for source in self.sources:
            source.close()
        self._tmp.cleanup()

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def load(self, path, root=None, limits=Limits()):
        source = open_source(path, limits)
        self.sources.append(source)
        roots = source.roots()
        chosen = roots[0] if root is None else root
        index = ExportIndex(source.view(chosen), 'e' * 12,
                            {'type': source.kind, 'name': source.name, 'path': source.path}, limits)
        return source, index


class CountingZipSource:
    """Counts the bytes each member stream hands out (to prove caps hold)."""

    def __init__(self, source):
        from collections import Counter
        self.by_name = Counter()
        self.opened = Counter()
        original = source.open_member
        outer = self

        def open_member(name):
            stream = original(name)
            outer.opened[name] += 1

            class Counted:
                def read(self, n=-1):
                    data = stream.read(n)
                    outer.by_name[name] += len(data)
                    return data

                def close(self):
                    stream.close()

            return Counted()

        source.open_member = open_member


class ZipLayoutTests(TempDirCase):
    def test_top_folder_root(self):
        path = fx.write_zip(fx.standard_export(), os.path.join(self.tmp, 'a.zip'))
        source, index = self.load(path)
        self.assertEqual(source.roots(), [ROOT])
        self.assertEqual(index.summary['counts']['jobs'], 9)
        self.assertEqual(index.summary['counts']['images_present'], 33)
        self.assertEqual(index.summary['label'], ROOT)

    def test_files_at_zip_top(self):
        path = fx.write_zip(fx.standard_export(), os.path.join(self.tmp, 'top.zip'), root='')
        source, index = self.load(path)
        self.assertEqual(source.roots(), [''])
        self.assertEqual(index.summary['counts']['images_present'], 33)
        self.assertEqual(index.summary['label'], 'top')

    def test_nested_root(self):
        root = 'outer/inner/' + ROOT
        path = fx.write_zip(fx.standard_export(), os.path.join(self.tmp, 'n.zip'), root=root)
        source, index = self.load(path)
        self.assertEqual(source.roots(), [root])
        self.assertEqual(index.summary['counts']['images_present'], 33)
        self.assertEqual(index.overview()['files']['unreferenced'], [])

    def test_several_roots(self):
        first = fx.standard_export()
        second = fx.standard_export()
        second.jobs = second.jobs[:2]
        second.sync_counts()
        members = {'one/' + k: v for k, v in first.members().items()}
        members.update({'two/' + k: v for k, v in second.members().items()})
        path = self.write('multi.zip', fx.zip_bytes(members))
        source = open_source(path)
        self.sources.append(source)
        self.assertEqual(source.roots(), ['one', 'two'])
        jobs = [ExportIndex(source.view(r), r.ljust(12, 'x'), {'type': 'zip', 'name': 'm', 'path': path}).summary['counts']['jobs']
                for r in source.roots()]
        self.assertEqual(jobs, [9, 2])

    def test_macos_metadata_entries_are_ignored(self):
        extra = [('__MACOSX/%s/._jobs.jsonl' % ROOT, b'junk'), ('__MACOSX/%s/export_meta.json' % ROOT, b'{}'),
                 ('%s/images/._job-grid-a_0.png' % ROOT, b'junk'), ('%s/.DS_Store' % ROOT, b'junk')]
        path = fx.write_zip(fx.standard_export(), os.path.join(self.tmp, 'mac.zip'), extra=extra)
        source, index = self.load(path)
        self.assertEqual(source.roots(), [ROOT])
        self.assertEqual(source.issues, [])
        self.assertEqual(source.ignored, 4)
        self.assertEqual(index.overview()['files']['unreferenced'], [])


class ZipSafetyTests(TempDirCase):
    UNSAFE = ['../evil.png', '/abs.png', ROOT + '/images/back\\slash.png', 'C:/drive.png',
              ROOT + '/images/ctl\x01.png', ROOT + '/./images/dot.png', ROOT + '//images/empty.png',
              ROOT + '/images/../../escape.png']

    def test_unsafe_names_are_refused_and_reported(self):
        export = fx.standard_export()
        export.jobs[0]['image_files'] += ['../evil.png', 'images/../../escape.png', '/abs.png']
        export.skip_images.update(['../evil.png', 'images/../../escape.png', '/abs.png'])
        evil = b'EVIL' + fx.png_bytes(5, 5)
        path = fx.write_zip(export, os.path.join(self.tmp, 'evil.zip'), extra=[(n, evil) for n in self.UNSAFE])
        source, index = self.load(path)
        reported = {issue.raw for issue in source.issues if issue.code == 'unsafe_name'}
        self.assertEqual(reported, set(self.UNSAFE))
        view = source.view(ROOT)
        for rel in ('../evil.png', '/abs.png', 'images/../../escape.png', 'images/back\\slash.png'):
            with self.assertRaises(MemberError):
                view.stat(rel)
            with self.assertRaises(MemberError):
                view.open(rel)
        overview = index.overview()
        unreadable = [item['path'] for item in overview['files']['unreadable']]
        for rel in ('../evil.png', '/abs.png', 'images/../../escape.png', '../../escape.png', 'C:/drive.png'):
            self.assertTrue(any(rel in p for p in unreadable), rel)
        refs = index.job_detail('job-grid-a')['images'][4:]
        self.assertEqual([r['status'] for r in refs], ['unsupported'] * 3)
        self.assertTrue(all(r['url'] is None for r in refs))
        for rec in index.file_ids.values():
            self.assertIsNone(unsafe_reason(rec.rel))
            self.assertNotIn(b'EVIL', view.read_capped(rec.rel, 10 ** 7)[:4])
        self.assertIn('files_unreadable', {c['id'] for c in overview['checks']})

    def test_symlink_member_is_skipped(self):
        export = fx.standard_export()
        export.skip_images.add('images/job-grid-a_1.png')
        path = fx.write_zip(export, os.path.join(self.tmp, 'link.zip'),
                            extra=[(fx.symlink_info(ROOT + '/images/job-grid-a_1.png'), b'/etc/passwd')])
        source, index = self.load(path)
        self.assertEqual([i.code for i in source.issues], ['symlink'])
        ref = index.job_detail('job-grid-a')['images'][1]
        self.assertEqual(ref['status'], 'unsupported')
        self.assertIsNone(ref['url'])
        self.assertIn('symbolic link', ref['reason'])
        self.assertNotIn('images/job-grid-a_1.png', [r.rel for r in index.file_ids.values()])

    def test_metadata_bomb_with_honest_size_is_refused_unread(self):
        export = fx.standard_export()
        export.raw['export_meta.json'] = b'{"pad": "' + b' ' * 2_000_000 + b'"}'
        path = fx.write_zip(export, os.path.join(self.tmp, 'bomb.zip'))
        import zipfile
        with zipfile.ZipFile(path) as archive:  # it really is a bomb: tiny compressed, huge expanded
            self.assertLess(archive.getinfo(ROOT + '/export_meta.json').compress_size, 50_000)
        source = ZipSource(path, Limits(max_meta_bytes=1024))
        self.sources.append(source)
        counter = CountingZipSource(source)
        index = ExportIndex(source.view(ROOT), 'b' * 12, {'type': 'zip', 'name': 'b', 'path': path},
                            Limits(max_meta_bytes=1024))
        self.assertEqual(index.file_states['export_meta.json']['state'], 'too_large')
        self.assertIsNone(index.meta)
        self.assertEqual(index.summary['counts']['jobs'], 9)  # the rest still loads
        self.assertEqual(counter.opened[ROOT + '/export_meta.json'], 0)  # refused from its declared size
        self.assertEqual(counter.by_name[ROOT + '/export_meta.json'], 0)
        self.assertEqual([c['level'] for c in index.checks if c['id'] == 'export_meta'], ['error'])

    def test_metadata_bomb_with_lying_size_is_refused_after_cap(self):
        export = fx.standard_export()
        export.raw['export_meta.json'] = b'{"pad": "' + b' ' * 2_000_000 + b'"}'
        data = fx.export_zip_bytes(export)
        data = fx.patch_central_directory(data, ROOT + '/export_meta.json', file_size=100)
        path = self.write('liar.zip', data)
        limits = Limits(max_meta_bytes=1024)
        source = ZipSource(path, limits)
        self.sources.append(source)
        counter = CountingZipSource(source)
        index = ExportIndex(source.view(ROOT), 'l' * 12, {'type': 'zip', 'name': 'l', 'path': path}, limits)
        self.assertIn(index.file_states['export_meta.json']['state'], ('unreadable', 'too_large'))
        self.assertIsNone(index.meta)
        self.assertEqual(counter.opened[ROOT + '/export_meta.json'], 1)
        self.assertLessEqual(counter.by_name[ROOT + '/export_meta.json'], 1025)

    def test_jsonl_bomb_is_refused(self):
        export = fx.standard_export()
        export.raw['jobs.jsonl'] = fx.jsonl(export.jobs) + b' ' * 3_000_000 + b'\n'
        path = fx.write_zip(export, os.path.join(self.tmp, 'jbomb.zip'))
        source, index = self.load(path, limits=Limits(max_jsonl_bytes=64 * 1024))
        self.assertEqual(index.file_states['jobs.jsonl']['state'], 'too_large')
        self.assertEqual(index.summary['counts']['jobs'], 0)
        self.assertEqual([c['level'] for c in index.checks if c['id'] == 'file:jobs.jsonl'], ['error'])

    def test_streaming_cap_holds_when_declared_size_lies(self):
        class Endless:
            """8 MB behind a 10-byte declaration: a missing cap fails fast instead of hanging."""
            consumed = 0

            def read(self, n=-1):
                n = min(n if n >= 0 else 10 ** 6, 8_000_000 - Endless.consumed)
                Endless.consumed += n
                return b'x' * n

            def close(self):
                pass

        class LyingView(RootView):
            def stat(self, rel):
                return FileStat(10, '0')  # declares 10 bytes, streams forever

            def open(self, rel):
                return Endless()

        with self.assertRaises(TooLarge):
            LyingView().read_capped('jobs.jsonl', 1000)
        self.assertLessEqual(Endless.consumed, 1001)

    def test_encrypted_and_unsupported_compression_are_reported(self):
        data = fx.export_zip_bytes(fx.standard_export())
        data = fx.patch_central_directory(data, ROOT + '/images/job-grid-a_2.png', flag_bits=1)
        data = fx.patch_central_directory(data, ROOT + '/images/job-grid-a_3.png', compress_type=99)
        source, index = self.load(self.write('odd.zip', data))
        self.assertEqual(sorted(i.code for i in source.issues), ['compression', 'encrypted'])
        refs = index.job_detail('job-grid-a')['images']
        self.assertEqual([r['status'] for r in refs], ['present', 'present', 'unsupported', 'unsupported'])
        self.assertIn('encrypted', refs[2]['reason'])
        self.assertIn('compression', refs[3]['reason'])
        reasons = ' '.join(i['reason'] for i in index.overview()['files']['unreadable'])
        self.assertIn('encrypted', reasons)

    def test_corrupt_archives_are_source_errors(self):
        import struct
        good = fx.export_zip_bytes(fx.standard_export())
        cd_offset = struct.unpack('<4s4H2LH', good[-22:])[6]
        cases = {
            'garbage.zip': b'PK\x03\x04' + b'\x00garbage' * 50,
            'truncated.zip': good[:len(good) // 2],
            'bad_directory.zip': good[:cd_offset] + b'\xff' * 46 + good[cd_offset + 46:],
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(SourceError) as caught:
                    open_source(self.write(name, data)).close()
                self.assertIn(caught.exception.code, ('bad_zip', 'not_a_zip'))
                self.assertTrue(caught.exception.message)

    def test_entry_count_cap(self):
        path = fx.write_zip(fx.standard_export(), os.path.join(self.tmp, 'many.zip'))
        with self.assertRaises(SourceError) as caught:
            ZipSource(path, Limits(max_entries=10))
        self.assertEqual(caught.exception.code, 'too_many_entries')

    def test_concurrent_reads_are_consistent(self):
        import threading
        export = fx.standard_export()
        path = fx.write_zip(export, os.path.join(self.tmp, 'threads.zip'))
        source, index = self.load(path)
        view = source.view(ROOT)
        expected = export.members()['images/job-grid-a_0.png']
        errors = []

        def worker():
            for _ in range(5):
                if b''.join(view.iter_chunks('images/job-grid-a_0.png', chunk_size=4096)) != expected:
                    errors.append('mismatch')

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class FolderTests(TempDirCase):
    def test_root_discovery(self):
        outer = os.path.join(self.tmp, 'outer')
        folder = fx.write_folder(fx.standard_export(), outer)
        self.assertEqual(FolderSource(outer).roots(), [ROOT])
        self.assertEqual(FolderSource(folder).roots(), [''])

    def test_symlink_escaping_the_root_is_refused(self):
        folder = fx.write_folder(fx.standard_export(), self.tmp)
        secret = self.write('secret.png', fx.png_bytes(3, 3))
        target = os.path.join(folder, 'images', 'job-grid-a_1.png')
        os.unlink(target)
        os.symlink(secret, target)
        source, index = self.load(folder)
        ref = index.job_detail('job-grid-a')['images'][1]
        self.assertEqual(ref['status'], 'unsupported')
        self.assertIsNone(ref['url'])
        self.assertIn('outside the export folder', ref['reason'])
        with self.assertRaises(MemberError) as caught:
            source.view('').open('images/job-grid-a_1.png')
        self.assertEqual(caught.exception.code, 'escapes_root')
        self.assertTrue(any('job-grid-a_1.png' in i['path'] for i in index.overview()['files']['unreadable']))

    def test_symlink_inside_the_root_is_allowed(self):
        folder = fx.write_folder(fx.standard_export(), self.tmp)
        target = os.path.join(folder, 'images', 'job-grid-a_2.png')
        os.unlink(target)
        os.symlink('job-grid-a_3.png', target)
        source, index = self.load(folder)
        ref = index.job_detail('job-grid-a')['images'][2]
        self.assertEqual(ref['status'], 'present')

    def test_metadata_symlink_escape_is_not_read(self):
        export = fx.standard_export()
        folder = fx.write_folder(export, self.tmp)
        outside = self.write('outside.jsonl', fx.jsonl(export.jobs))
        os.unlink(os.path.join(folder, 'jobs.jsonl'))
        os.symlink(outside, os.path.join(folder, 'jobs.jsonl'))
        source, index = self.load(folder)
        self.assertEqual(index.file_states['jobs.jsonl']['state'], 'unreadable')
        self.assertEqual(index.summary['counts']['jobs'], 0)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'needs mkfifo')
    def test_fifo_is_never_opened(self):
        folder = fx.write_folder(fx.standard_export(), self.tmp)
        target = os.path.join(folder, 'images', 'job-grid-a_1.png')
        os.unlink(target)
        os.mkfifo(target)
        source, index = self.load(folder)  # would hang if the FIFO were opened for reading
        ref = index.job_detail('job-grid-a')['images'][1]
        self.assertEqual(ref['status'], 'unsupported')
        self.assertIn('regular file', ref['reason'])

    def test_open_source_errors(self):
        with self.assertRaises(SourceError) as caught:
            open_source(os.path.join(self.tmp, 'nope'))
        self.assertEqual(caught.exception.code, 'not_found')
        with self.assertRaises(SourceError) as caught:
            open_source(self.write('notes.txt', b'hello'))
        self.assertEqual(caught.exception.code, 'not_a_zip')


class RulesTests(unittest.TestCase):
    def test_unsafe_reason(self):
        for name in ['', '/a', 'C:/a', 'a\\b', 'a/../b', '..', 'a/./b', 'a//b', 'a\x00b', 'a\x1fb', 'a/', 5, None]:
            self.assertIsNotNone(unsafe_reason(name), repr(name))
        for name in ['images/a_0.png', 'jobs.jsonl', 'a/b c/d.png', 'images/ünïcode_0.jpeg', 'x..y.png']:
            self.assertIsNone(unsafe_reason(name), name)

    def test_detect_image_type(self):
        self.assertEqual(detect_image_type(fx.JPEG_8X8[:64]), 'jpeg')
        self.assertEqual(detect_image_type(fx.png_bytes()[:64]), 'png')
        self.assertEqual(detect_image_type(b'GIF89a' + b'\x00' * 20), 'gif')
        self.assertEqual(detect_image_type(b'RIFF\x10\x00\x00\x00WEBPVP8 '), 'webp')
        self.assertEqual(detect_image_type(b'\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf'), 'avif')
        self.assertEqual(detect_image_type(b'\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic'), None)
        self.assertIsNone(detect_image_type(fx.SVG_BYTES[:64]))
        self.assertIsNone(detect_image_type(fx.TEXT_BYTES))


if __name__ == '__main__':
    unittest.main()
