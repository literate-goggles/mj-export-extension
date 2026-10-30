"""The viewer must never modify an export: crawl everything, then compare."""
import hashlib
import json
import os
import tempfile
import unittest
import urllib.parse

from tests import fixtures as fx
from tests.server_harness import LiveServer


def snapshot(root):
    """Every path under root with content hash, size, mtime and mode (links by target)."""
    state = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        info = os.lstat(folder)
        state[os.path.relpath(folder, root) + '/'] = ('dir', info.st_mtime_ns, info.st_mode)
        for name in files + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            if os.path.islink(path):
                state[os.path.relpath(path, root)] = ('link', os.readlink(path), info.st_mtime_ns)
                continue
            with open(path, 'rb') as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            state[os.path.relpath(path, root)] = (digest, info.st_size, info.st_mtime_ns, info.st_mode)
    return state


def file_urls(value, found):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('url', 'thumb_url') and isinstance(item, str) and item.startswith('/files/'):
                found.add(item)
            else:
                file_urls(item, found)
    elif isinstance(value, list):
        for item in value:
            file_urls(item, found)
    return found


class PreservationTests(unittest.TestCase):
    def test_crawling_every_endpoint_and_file_changes_nothing(self):
        with tempfile.TemporaryDirectory(prefix='mjv-preserve-') as base:
            data = os.path.join(base, 'exports')
            os.makedirs(data)
            export = fx.standard_export()
            export.files['images/stray.png'] = fx.png_bytes(2, 2)
            export.files['images/job-grid-c_1.png'] = fx.TEXT_BYTES
            zip_path = fx.write_zip(export, os.path.join(data, 'standard.zip'))
            folder = fx.write_folder(export, os.path.join(data, 'unzipped'))
            os.symlink('job-grid-a_3.png', os.path.join(folder, 'images', 'job-grid-a_2.png.alias'))
            before = snapshot(data)

            live = LiveServer(os.path.join(base, 'viewer'))
            self.assertFalse(live.app.cache_dir.startswith(data))
            requested = 0
            try:
                ids = []
                for path in (zip_path, folder):
                    status, body = live.post_json('/api/exports/open', {'path': path})
                    self.assertEqual(status, 200, body)
                    ids += [e['id'] for e in body['exports']]
                self.assertEqual(len(set(ids)), 2)
                for path in ('/', '/api/health', '/api/exports'):
                    self.assertEqual(live.request('GET', path)[0], 200)
                urls = set()
                for eid in ids:
                    api = '/api/exports/%s' % eid
                    payloads = [live.get_json(api + '/overview')[1], live.get_json(api + '/moodboards')[1],
                                live.get_json(api + '/profiles')[1], live.get_json(api + '/likes')[1]]
                    offset = 0
                    while True:
                        page = live.get_json(api + '/jobs?limit=4&offset=%d' % offset)[1]
                        payloads.append(page)
                        for card in page['items']:
                            payloads.append(live.get_json(api + '/jobs/' + urllib.parse.quote(card['job_id'], safe=''))[1])
                        offset += 4
                        if offset >= page['total']:
                            break
                    for query in ('q=fox', 'kind=upscale', 'version=v7', 'moodboard=board-2&links=explicit',
                                  'profile=code%3Aucode9', 'date_from=2026-03-12&date_to=2026-03-13', 'sort=oldest'):
                        payloads.append(live.get_json(api + '/jobs?' + query)[1])
                    for board in payloads[1]['items']:
                        payloads.append(live.get_json(api + '/moodboards/' + urllib.parse.quote(board['id'], safe=''))[1])
                    for payload in payloads:
                        self.assertNotIn('error', payload)
                        file_urls(payload, urls)
                index = live.app.registry.get(ids[0])
                urls.add('/files/%s/%s' % (ids[0], index.files['images/job-grid-c_1.png'].file_id))  # the 415 one
                for url in sorted(urls):
                    status, _, _ = live.request('GET', url)
                    self.assertIn(status, (200, 415), url)
                    requested += 1
            finally:
                live.stop()
            self.assertGreater(requested, 100)
            after = snapshot(data)
            self.assertEqual(sorted(after), sorted(before))
            self.assertEqual(after, before)
            cache_files = [os.path.join(f, n) for f, _, names in os.walk(live.app.cache_dir) for n in names]
            self.assertTrue(all(not p.startswith(data) for p in cache_files))

    def test_cache_directory_inside_an_export_folder_is_refused(self):
        with tempfile.TemporaryDirectory(prefix='mjv-preserve-') as base:
            folder = fx.write_folder(fx.standard_export(), base)
            live = LiveServer(os.path.join(base, 'viewer'), cache_dir=os.path.join(folder, 'cache'))
            try:
                status, body = live.post_json('/api/exports/open', {'path': folder})
                self.assertEqual((status, body['error']['code']), (400, 'cache_inside_export'))
            finally:
                live.stop()
            self.assertFalse(os.path.exists(os.path.join(folder, 'cache')))


if __name__ == '__main__':
    unittest.main()
