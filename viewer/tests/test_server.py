"""HTTP API: security rules, endpoints, uploads, files and thumbnails."""
import contextlib
import http.client
import io
import json
import os
import socket
import struct
import tempfile
import time
import unittest
import urllib.parse
import zipfile
from unittest import mock

from mjviewer import server as server_module
from mjviewer.__main__ import main
from mjviewer.server import CSP
from tests import fixtures as fx
from tests.server_harness import LiveServer, ServerTestCase

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


class SecurityTests(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = fx.write_zip(fx.standard_export(), os.path.join(cls.data, 'standard.zip'))
        status, body = cls.live.post_json('/api/exports/open', {'path': path})
        assert status == 200, body
        cls.export_id = body['exports'][0]['id']

    def assert_security_headers(self, headers):
        self.assertEqual(headers['Content-Security-Policy'], CSP)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(headers['Cross-Origin-Resource-Policy'], 'same-origin')

    def test_index_injects_token_and_sets_strict_cookie(self):
        status, headers, body = self.live.request('GET', '/', cookie=False)
        self.assertEqual(status, 200)
        self.assertIn(('content="%s"' % self.live.app.token).encode(), body)
        self.assertNotIn(b'{{MJV_TOKEN}}', body)
        cookie = headers['Set-Cookie']
        self.assertTrue(cookie.startswith('mjv_%d=%s;' % (self.live.port, self.live.app.token)))
        for flag in ('HttpOnly', 'SameSite=Strict', 'Path=/'):
            self.assertIn(flag, cookie)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_security_headers_on_every_kind_of_response(self):
        file_url = self.live.get_json('/api/exports/%s/jobs?limit=1' % self.export_id)[1]['items'][0]['images'][0]['url']
        for path, cookie, expected in [('/', False, 200), ('/static/app.js', False, 200), ('/api/health', True, 200),
                                       ('/api/nope', True, 404), ('/api/health', False, 403), (file_url, True, 200),
                                       ('/files/x/y', True, 404), ('/missing.css', False, 404)]:
            with self.subTest(path=path, cookie=cookie):
                status, headers, _ = self.live.request('GET', path, cookie=cookie)
                self.assertEqual(status, expected)
                self.assert_security_headers(headers)
        status, headers, _ = self.live.request('GET', '/api/health')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertTrue(headers['Content-Type'].startswith('application/json'))

    def test_foreign_host_header_is_rejected(self):
        for host in ('evil.example', 'evil.example:%d' % self.live.port, 'localhost.evil.example', '127.0.0.2', ''):
            for path in ('/', '/api/health', '/static/app.js'):
                with self.subTest(host=host, path=path):
                    status, headers, body = self.live.request('GET', path, host=host)
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(body)['error']['code'], 'forbidden_host')
                    self.assert_security_headers(headers)
        for host in ('localhost:1234', 'LOCALHOST', '127.0.0.1', '[::1]:9'):
            with self.subTest(host=host):
                self.assertEqual(self.live.request('GET', '/api/health', host=host)[0], 200)

    def test_allow_host_option(self):
        with tempfile.TemporaryDirectory() as base:
            live = LiveServer(base, allow_hosts=['viewer.lan'])
            try:
                self.assertEqual(live.request('GET', '/api/health', host='viewer.lan:80')[0], 200)
                self.assertEqual(live.request('GET', '/api/health', host='other.lan')[0], 403)
            finally:
                live.stop()

    def test_cookie_is_required_for_api_and_files(self):
        file_url = self.live.get_json('/api/exports/%s/jobs?limit=1' % self.export_id)[1]['items'][0]['images'][0]['url']
        for path in ('/api/health', '/api/exports', file_url):
            for cookie in (False, 'mjv_%d=wrong' % self.live.port, 'mjv_1=%s' % self.live.app.token):
                with self.subTest(path=path, cookie=cookie):
                    status, _, body = self.live.request('GET', path, cookie=cookie)
                    self.assertEqual(status, 403)
                    self.assertEqual(json.loads(body)['error']['code'], 'forbidden')
        status, _, _ = self.live.request('GET', '/api/health', cookie='other=1; %s; x=y' % self.live.cookie)
        self.assertEqual(status, 200)

    def test_post_needs_token_header_and_same_origin(self):
        payload = {'path': os.path.join(self.data, 'standard.zip')}
        self.assertEqual(self.live.post_json('/api/exports/open', payload, token=False)[0], 403)
        self.assertEqual(self.live.post_json('/api/exports/open', payload, token='wrong')[0], 403)
        self.assertEqual(self.live.post_json('/api/exports/open', payload, cookie=False)[0], 403)
        status, body = self.live.post_json('/api/exports/open', payload, headers={'Origin': 'http://evil.example'})
        self.assertEqual((status, body['error']['code']), (403, 'forbidden_origin'))
        origin = {'Origin': 'http://127.0.0.1:%d' % self.live.port}
        self.assertEqual(self.live.post_json('/api/exports/open', payload, headers=origin)[0], 200)

    def test_unknown_ids_and_paths(self):
        prefix = '/files/%s/' % self.export_id
        for path in (prefix + 'deadbeefdeadbeefdead', prefix + '..%2F..%2Fjobs.jsonl', prefix + 'images%2Fjob-grid-a_0.png',
                     '/files/%s/x/y' % self.export_id, '/files/000000000000/abc', '/api/exports/nope/overview',
                     '/api/exports/%s/jobs/nope' % self.export_id, '/api/exports/%s/moodboards/nope' % self.export_id):
            with self.subTest(path=path):
                status, _, body = self.live.request('GET', path)
                self.assertEqual(status, 404)
                self.assertIn('error', json.loads(body))
        for path in ('/static/../mjviewer/server.py', '/static/%2e%2e/%2e%2e/etc/passwd', '/static/'):
            with self.subTest(path=path):
                self.assertEqual(self.live.request('GET', path)[0], 404)

    def test_request_target_must_be_origin_form(self):
        for path in ('http://127.0.0.1/api/health', '*'):
            with self.subTest(path=path):
                status, headers, body = self.live.request('GET', path)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)['error']['code'], 'bad_request')
                self.assert_security_headers(headers)
        # '//host/api/...' is never routed to the API (http.server may collapse it to a client-side route).
        status, headers, body = self.live.request('GET', '//127.0.0.1/api/health')
        self.assertIn(status, (200, 400))
        self.assertNotIn(b'"api":1', body)

    def test_client_disconnects_are_not_reported_as_errors(self):
        # A browser that drops a keep-alive connection resets the socket while the server waits
        # for the next request line; that must not print a traceback.
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            for partial in (b'', b'GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n'):
                client = socket.create_connection(('127.0.0.1', self.live.port), timeout=5)
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                if partial:
                    client.sendall(partial)
                time.sleep(0.05)
                client.close()  # SO_LINGER 0: the close is a reset
            time.sleep(0.3)
            status, _ = self.live.get_json('/api/health')
        self.assertEqual(status, 200)
        self.assertNotIn('Traceback', buffer.getvalue())

    def test_a_get_with_a_body_closes_the_connection(self):
        host = '127.0.0.1:%d' % self.live.port
        inner = 'GET /favicon.ico HTTP/1.1\r\nHost: %s\r\n\r\n' % host
        for path, extra in (('/api/health', 'Content-Length: %d\r\n' % len(inner)),
                            ('/', 'Content-Length: %d\r\n' % len(inner)),
                            ('/api/health', 'Content-Length: %d\r\nContent-Length: 0\r\n' % len(inner))):
            with self.subTest(path=path, extra=extra):
                request = 'GET %s HTTP/1.1\r\nHost: %s\r\nCookie: %s\r\n%s\r\n%s' % (
                    path, host, self.live.cookie, extra, inner)
                with socket.create_connection(('127.0.0.1', self.live.port), timeout=5) as sock:
                    sock.sendall(request.encode())
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
                self.assertEqual((data.count(b'HTTP/1.1 '), closed), (1, True))

    def test_wrong_methods(self):
        self.assertEqual(self.live.request('DELETE', '/api/exports', token=True)[0], 405)
        self.assertEqual(self.live.request('POST', '/api/health', body=b'{}', token=True)[0], 405)
        self.assertEqual(self.live.request('GET', '/api/exports/open')[0], 405)

    def test_logs_never_contain_search_terms(self):
        self.live.request('GET', '/api/exports/%s/jobs?q=supersecretpromptword' % self.export_id)
        self.live.request('GET', '/api/exports/%s/jobs?q=supersecretpromptword&limit=bad' % self.export_id)
        self.assertNotIn('supersecretpromptword', self.live.log.getvalue())
        self.assertIn('/api/exports/%s/jobs' % self.export_id, self.live.log.getvalue())


class ApiTests(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        export = fx.standard_export()
        export.files['images/job-grid-a_1.png'] = fx.TEXT_BYTES
        cls.export = export
        cls.zip_path = fx.write_zip(export, os.path.join(cls.data, 'standard.zip'))
        status, body = cls.live.post_json('/api/exports/open', {'path': cls.zip_path})
        assert status == 200, body
        cls.summary = body['exports'][0]
        cls.eid = cls.summary['id']

    def api(self, path):
        status, body = self.live.get_json('/api/exports/%s%s' % (self.eid, path))
        self.assertEqual(status, 200, body)
        return body

    def test_health(self):
        status, body = self.live.get_json('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual((body['ok'], body['api']), (True, 1))
        self.assertIsInstance(body['thumbnails'], bool)
        self.assertIsInstance(body['version'], str)

    def test_exports_listing_and_reopen_is_idempotent(self):
        status, again = self.live.post_json('/api/exports/open', {'path': self.zip_path})
        self.assertEqual((status, [e['id'] for e in again['exports']]), (200, [self.eid]))
        status, body = self.live.get_json('/api/exports')
        self.assertEqual([e['id'] for e in body['exports']], [self.eid])
        summary = body['exports'][0]
        self.assertEqual(summary['source']['type'], 'zip')
        self.assertEqual(summary['source']['name'], 'standard.zip')
        self.assertEqual(summary['root'], fx.ROOT)
        self.assertEqual(summary['label'], fx.ROOT)
        self.assertEqual(summary['counts']['images_present'], 32)
        self.assertEqual(body['library'], [])

    def test_overview_contract_keys(self):
        overview = self.api('/overview')
        self.assertEqual(set(overview), {'summary', 'meta', 'scope', 'status', 'feed_exhausted', 'counts', 'failures',
                                         'parent_gaps', 'limitations', 'checks', 'facets', 'unresolved', 'files'})
        self.assertEqual(set(overview['counts']['observed']) >= {'jobs', 'moodboards', 'profiles', 'likes',
                                                                  'images_expected', 'images_present', 'images_missing',
                                                                  'images_unreferenced', 'bytes_present'}, True)
        self.assertEqual(set(overview['facets']), {'kinds', 'versions', 'moodboards', 'profiles', 'date_min', 'date_max'})
        for check in overview['checks']:
            self.assertEqual(set(check), {'id', 'level', 'title', 'detail', 'items'})
            self.assertIn(check['level'], ('ok', 'info', 'warning', 'error'))

    def test_jobs_endpoint_filters_and_errors(self):
        def total(query):
            return self.api('/jobs?' + query)['total']
        self.assertEqual(total(urllib.parse.urlencode({'q': 'red FOX'})), 3)
        self.assertEqual(total('kind=upscale'), 2)
        self.assertEqual(total('version=v7'), 2)
        self.assertEqual(total('moodboard=board-2'), 1)
        self.assertEqual(total('moodboard=board-2&links=explicit'), 0)
        self.assertEqual(total('profile=' + urllib.parse.quote('code:ucode9')), 1)
        self.assertEqual(total('date_from=2026-03-13&date_to=2026-03-13'), 2)
        page = self.api('/jobs?sort=oldest&limit=2&offset=1')
        self.assertEqual([c['job_id'] for c in page['items']], ['job-grid-b', 'job-grid-c'])
        card = page['items'][0]
        self.assertEqual(set(card), {'job_id', 'created_at', 'prompt', 'kind', 'kind_source', 'version', 'width', 'height',
                                     'images', 'moodboards', 'profiles', 'codes', 'parent_id', 'parent_status',
                                     'child_count', 'liked'})
        self.assertEqual(set(card['images'][0]), {'index', 'status', 'url', 'thumb_url', 'width', 'height', 'file',
                                                  'source_url', 'reason'})
        status, body = self.live.get_json('/api/exports/%s/jobs?limit=0' % self.eid)
        self.assertEqual((status, body['error']['code']), (400, 'bad_parameter'))

    def test_job_detail_lineage_navigation(self):
        detail = self.api('/jobs/job-up')
        self.assertEqual([a['job_id'] for a in detail['lineage']['ancestors']], ['job-grid-a', 'job-var'])
        parent = self.api('/jobs/' + detail['lineage']['parent']['id'])
        self.assertEqual([c['job_id'] for c in parent['lineage']['children']], ['job-up'])
        self.assertEqual(detail['raw']['job_id'], 'job-up')
        quoted = self.live.get_json('/api/exports/%s/jobs/%s' % (self.eid, urllib.parse.quote('job-grid-a')))
        self.assertEqual(quoted[0], 200)

    def test_moodboards_profiles_likes(self):
        boards = self.api('/moodboards')
        self.assertEqual([b['id'] for b in boards['items']], ['board-1', 'board-2'])
        self.assertTrue(boards['notes'])
        detail = self.api('/moodboards/board-1')
        self.assertEqual([g['link'] for g in detail['generations']], ['exporter'])
        self.assertEqual(len(detail['references']), 2)
        profiles = self.api('/profiles')
        self.assertEqual(profiles['unmatched_codes'][0]['code'], 'ucode9')
        likes = self.api('/likes')
        self.assertEqual([i['job']['job_id'] for i in likes['items']], ['job-grid-b'])

    def test_files_are_served_by_opaque_id_with_sniffed_type(self):
        card = self.api('/jobs?' + urllib.parse.urlencode({'q': 'whale'}))['items'][0]
        status, headers, body = self.live.request('GET', card['images'][0]['url'])
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(body, fx.JPEG_8X8)
        self.assertEqual(int(headers['Content-Length']), len(fx.JPEG_8X8))
        self.assertTrue(headers['Cache-Control'].startswith('private'))
        self.assertNotIn('job-grid-b', card['images'][0]['url'])

    def test_non_image_file_is_415(self):
        index = self.live.app.registry.get(self.eid)
        rec = index.files['images/job-grid-a_1.png']
        self.assertEqual(rec.status, 'unsupported')
        status, headers, body = self.live.request('GET', '/files/%s/%s' % (self.eid, rec.file_id))
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)['error']['code'], 'unsupported_media')
        self.assertTrue(headers['Content-Type'].startswith('application/json'))

    def test_open_errors_and_server_survives(self):
        notes = os.path.join(self.data, 'notes.txt')
        with open(notes, 'w') as handle:
            handle.write('hello')
        empty = os.path.join(self.data, 'empty_folder')
        os.makedirs(empty, exist_ok=True)
        corrupt = os.path.join(self.data, 'corrupt.zip')
        good = fx.export_zip_bytes(fx.standard_export())
        with open(corrupt, 'wb') as handle:
            handle.write(good[:-22] + b'PK\x05\x06' + b'\xff' * 18)
        cases = [(os.path.join(self.data, 'nope.zip'), 404, 'not_found'), (notes, 400, 'not_a_zip'),
                 (empty, 400, 'no_export_root'), (corrupt, 400, 'bad_zip'), ('', 400, 'bad_request')]
        for path, expected_status, code in cases:
            with self.subTest(path=path):
                status, body = self.live.post_json('/api/exports/open', {'path': path})
                self.assertEqual(status, expected_status, body)
                self.assertTrue(body['error']['message'])
                if code:
                    self.assertEqual(body['error']['code'], code)
        self.assertEqual(self.live.request('POST', '/api/exports/open', body=b'not json', token=True)[0], 400)
        self.assertEqual(self.live.get_json('/api/health')[0], 200)

    def test_truncated_and_unreadable_zips(self):
        good = fx.export_zip_bytes(fx.standard_export())
        cut = os.path.join(self.data, 'cut.zip')
        with open(cut, 'wb') as handle:
            handle.write(good[:len(good) // 2])  # an interrupted download
        status, body = self.live.post_json('/api/exports/open', {'path': cut})
        self.assertEqual((status, body['error']['code']), (400, 'bad_zip'))
        self.assertIn('truncated', body['error']['message'])
        if os.geteuid() == 0:
            self.skipTest('root can read anything')
        locked = os.path.join(self.data, 'locked.zip')
        with open(locked, 'wb') as handle:
            handle.write(good)
        os.chmod(locked, 0)
        try:
            status, body = self.live.post_json('/api/exports/open', {'path': locked})
        finally:
            os.chmod(locked, 0o644)
        self.assertEqual((status, body['error']['code']), (403, 'unreadable'))



class SelectionApiTests(ServerTestCase):
    """An export in which the designer chose what to export (fixtures.selection_export())."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = fx.write_zip(fx.selection_export(), os.path.join(cls.data, 'chosen.zip'))
        status, body = cls.live.post_json('/api/exports/open', {'path': path})
        assert status == 200, body
        cls.eid = body['exports'][0]['id']

    def api(self, path):
        status, body = self.live.get_json('/api/exports/%s%s' % (self.eid, path))
        self.assertEqual(status, 200, body)
        return body

    def test_overview_adds_the_selection_and_reports_left_out_items_as_notes(self):
        overview = self.api('/overview')
        self.assertEqual(set(overview), {'summary', 'meta', 'scope', 'status', 'feed_exhausted', 'counts', 'failures',
                                         'parent_gaps', 'limitations', 'checks', 'facets', 'unresolved', 'files',
                                         'selection'})
        self.assertEqual(overview['selection']['jobs'], {'available': 25, 'exported': 10, 'left_out': 13, 'not_reviewed': 2})
        self.assertEqual(overview['selection']['left_out_moodboard_ids'], ['board-left'])
        levels = {c['id']: c['level'] for c in overview['checks']}
        self.assertEqual([levels[k] for k in ('selection', 'references_left_out', 'parent_gaps_left_out')], ['info'] * 3)
        self.assertEqual((overview['summary']['warnings'], overview['summary']['errors']), (0, 0))
        self.assertEqual(overview['summary']['version_support'], 'supported')  # 0.4.0 writes the selection
        self.assertEqual([g['job_id'] for g in overview['unresolved']['parent_gaps']], ['job-gap'])
        self.assertEqual([g.get('left_out') for g in overview['parent_gaps']], [None, True])  # as the exporter wrote them

    def test_job_routes_label_left_out_references_and_parents(self):
        detail = self.api('/jobs/job-niji')
        self.assertEqual([(r['link'], r['id']) for r in detail['references']], [('left_out', 'board-left')])
        child = self.api('/jobs/job-left-child')
        self.assertEqual(child['lineage']['parent'], {'id': 'job-left-parent', 'status': 'left_out', 'index': 1})
        cards = {c['job_id']: c for c in self.api('/jobs?kind=upscale')['items']}
        self.assertEqual((cards['job-left-child']['parent_status'], cards['job-gap']['parent_status']), ('left_out', 'missing'))
        self.assertEqual(self.api('/jobs?profile=' + urllib.parse.quote('code:mbcodeL'))['total'], 0)

    def test_moodboard_routes_count_left_out_generations(self):
        items = {b['id']: b for b in self.api('/moodboards')['items']}
        self.assertEqual({k: v['generated_left_out'] for k, v in items.items()}, {'board-1': 3, 'board-2': 0})
        board = self.api('/moodboards/board-1')
        self.assertEqual(board['moodboard']['generated_left_out'], 3)
        self.assertEqual(board['notes'][0], "3 more generations made with this moodboard are not in this export because "
                                            "of the designer's choice: left out, or made after choosing.")
        self.assertEqual(self.live.get_json('/api/exports/%s/moodboards/board-left' % self.eid)[0], 404)


class DamagedExportTests(ServerTestCase):
    """Exports that open only in part: each test opens its own, so ApiTests keeps its single export."""

    def test_one_unreadable_root_does_not_hide_the_others(self):
        members = {}
        for root in ('a_bad', 'b_good'):
            for name, data in fx.standard_export().members().items():
                members['%s/%s' % (root, name)] = data
        path = os.path.join(self.data, 'mixed.zip')
        with open(path, 'wb') as handle:
            handle.write(fx.zip_bytes(members))
        build = server_module.ExportIndex

        def flaky(view, *args, **kwargs):
            if view.root == 'a_bad':
                raise ValueError('a value nothing anticipated')
            return build(view, *args, **kwargs)

        with mock.patch.object(server_module, 'ExportIndex', side_effect=flaky), \
                contextlib.redirect_stderr(io.StringIO()):
            status, body = self.live.post_json('/api/exports/open', {'path': path})
        self.assertEqual(status, 200, body)
        self.assertEqual([e['root'] for e in body['exports']], ['b_good'])
        self.assertEqual([(f['root'], f['code']) for f in body['failed']], [('a_bad', 'load_failed')])
        failed = self.live.get_json('/api/exports')[1]['failed']
        self.assertIn({'path': path, 'root': 'a_bad', 'code': 'load_failed',
                       'message': 'could not read export a_bad (ValueError)'}, failed)

    def test_a_member_that_fails_while_streaming_is_reported_and_no_longer_served(self):
        name = fx.ROOT + '/images/job-grid-a_0.png'
        path = os.path.join(self.data, 'damaged.zip')
        with open(path, 'wb') as handle:
            handle.write(fx.flip_member_tail(fx.export_zip_bytes(fx.standard_export(),
                                                                 compression=zipfile.ZIP_STORED), name))
        eid = self.live.post_json('/api/exports/open', {'path': path})[1]['exports'][0]['id']
        self.assertEqual(self.live.get_json('/api/exports/%s/overview' % eid)[1]['summary']['counts']['images_present'], 33)
        image = self.live.get_json('/api/exports/%s/jobs/job-grid-a' % eid)[1]['images'][0]
        self.assertEqual(image['status'], 'present')  # its first bytes are an image
        connection = http.client.HTTPConnection('127.0.0.1', self.live.port, timeout=30)
        connection.request('GET', image['url'], headers={'Host': '127.0.0.1:%d' % self.live.port,
                                                         'Cookie': self.live.cookie})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        with self.assertRaises(http.client.IncompleteRead):
            response.read()  # the stream stops short: the CRC fails at the end
        connection.close()
        status, _, body = self.live.request('GET', image['url'])
        self.assertEqual((status, json.loads(body)['error']['code']), (415, 'unsupported_media'))
        overview = self.live.get_json('/api/exports/%s/overview' % eid)[1]
        self.assertEqual(overview['summary']['counts']['images_present'], 32)
        unreadable = [u for u in overview['files']['unreadable'] if u['path'] == 'images/job-grid-a_0.png']
        self.assertIn('CRC', unreadable[0]['reason'])
        self.assertIn('files_unreadable', [c['id'] for c in overview['checks']])
        self.assertEqual(self.live.get_json('/api/exports/%s/jobs/job-grid-a' % eid)[1]['images'][0]['status'],
                         'unsupported')


class LibraryTests(unittest.TestCase):
    def test_library_lists_zips_and_export_folders(self):
        with tempfile.TemporaryDirectory() as base:
            library = os.path.join(base, 'library')
            os.makedirs(library)
            zip_path = fx.write_zip(fx.standard_export(), os.path.join(library, 'one.zip'))
            fx.write_folder(fx.standard_export(), os.path.join(library, 'unzipped'))
            os.makedirs(os.path.join(library, 'unrelated'))
            with open(os.path.join(library, 'notes.txt'), 'w') as handle:
                handle.write('x')
            live = LiveServer(base, library_dirs=[library])
            try:
                listing = live.get_json('/api/exports')[1]['library']
                self.assertEqual([(i['name'], i['kind'], i['opened']) for i in listing],
                                 [('one.zip', 'zip', False), ('unzipped', 'folder', False)])
                live.post_json('/api/exports/open', {'path': zip_path})
                listing = live.get_json('/api/exports')[1]['library']
                self.assertEqual([i['opened'] for i in listing], [True, False])
            finally:
                live.stop()


class UploadTests(ServerTestCase):
    def uploads(self):
        folder = os.path.join(self.live.app.cache_dir, 'uploads')
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def upload(self, body, name='export.zip', **kwargs):
        kwargs.setdefault('token', True)
        status, headers, data = self.live.request('POST', '/api/exports/upload', body=body,
                                                  headers={'X-Filename': urllib.parse.quote(name),
                                                           'Content-Type': 'application/zip'}, **kwargs)
        return status, json.loads(data)

    def test_upload_opens_export_and_is_content_addressed(self):
        import hashlib
        data = fx.export_zip_bytes(fx.standard_export())
        status, body = self.upload(data, name='my export.zip')
        self.assertEqual(status, 200, body)
        summary = body['exports'][0]
        self.assertEqual((summary['source']['type'], summary['source']['name']), ('upload', 'my export.zip'))
        self.assertEqual(summary['counts']['jobs'], 9)
        self.assertEqual(self.uploads(), [hashlib.sha256(data).hexdigest() + '.zip'])
        status, again = self.upload(data, name='same again.zip')
        self.assertEqual(again['exports'][0]['id'], summary['id'])
        self.assertEqual(self.uploads(), [hashlib.sha256(data).hexdigest() + '.zip'])

    def test_upload_over_the_cap_is_rejected_and_leaves_nothing(self):
        before = self.uploads()
        old = self.live.app.max_upload_bytes
        self.live.app.max_upload_bytes = 1000
        try:
            status, body = self.upload(fx.export_zip_bytes(fx.standard_export())[:5000])
        finally:
            self.live.app.max_upload_bytes = old
        self.assertEqual((status, body['error']['code']), (413, 'too_large'))
        self.assertEqual(self.uploads(), before)

    def test_non_zip_and_rootless_zip_uploads_are_rejected(self):
        before = self.uploads()
        status, body = self.upload(b'definitely not a zip' * 50)
        self.assertEqual((status, body['error']['code']), (400, 'not_a_zip'))
        status, body = self.upload(fx.zip_bytes({'readme.txt': b'hello'}))
        self.assertEqual((status, body['error']['code']), (400, 'no_export_root'))
        self.assertEqual(self.uploads(), before)

    def test_upload_requires_the_token_header(self):
        status, body = self.upload(fx.zip_bytes({'jobs.jsonl': b''}), token=False)
        self.assertEqual(status, 403)


@unittest.skipUnless(Image is not None, 'Pillow is not installed')
class ThumbnailTests(ServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = fx.write_zip(fx.standard_export(), os.path.join(cls.data, 'standard.zip'))
        status, body = cls.live.post_json('/api/exports/open', {'path': path})
        cls.eid = body['exports'][0]['id']
        jobs = cls.live.get_json('/api/exports/%s/jobs?sort=oldest' % cls.eid)[1]['items']
        cls.big = jobs[0]['images'][0]   # 640x480 noise PNG
        cls.small = jobs[0]['images'][1]  # 4x4 PNG

    def test_thumbnail_is_smaller_and_cached(self):
        status, headers, original = self.live.request('GET', self.big['url'])
        self.assertEqual(Image.open(io.BytesIO(original)).size, (640, 480))
        status, headers, thumb = self.live.request('GET', self.big['thumb_url'])
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(thumb)).size, (512, 384))
        self.assertLess(len(thumb), len(original))
        cached = []
        for folder, _, files in os.walk(os.path.join(self.live.app.cache_dir, 'thumbs', self.eid)):
            cached += [os.path.join(folder, f) for f in files if not f.endswith('.orig')]
        self.assertEqual(len(cached), 1)
        renders = self.live.app.thumbs.renders
        status, _, again = self.live.request('GET', self.big['thumb_url'])
        self.assertEqual((status, again), (200, thumb))
        self.assertEqual(self.live.app.thumbs.renders, renders)

    def test_small_image_thumbnail_is_the_original(self):
        _, _, original = self.live.request('GET', self.small['url'])
        status, headers, thumb = self.live.request('GET', self.small['thumb_url'])
        self.assertEqual((status, thumb), (200, original))
        self.assertEqual(headers['Content-Type'], 'image/png')

    def test_concurrent_requests_render_each_thumbnail_once(self):
        import threading
        jobs = self.live.get_json('/api/exports/%s/jobs?sort=oldest' % self.eid)[1]['items']
        targets = [jobs[1]['images'][0]['thumb_url'], jobs[2]['images'][0]['thumb_url']]  # not requested elsewhere
        before = self.live.app.thumbs.renders
        results = []

        def fetch(url):
            status, _, body = self.live.request('GET', url)
            results.append((url, status, body))

        threads = [threading.Thread(target=fetch, args=(targets[i % 2],)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({status for _, status, _ in results}, {200})
        for url in targets:
            self.assertEqual(len({body for u, _, body in results if u == url}), 1)
        self.assertEqual(self.live.app.thumbs.renders - before, 2)


class NoPillowTests(unittest.TestCase):
    """Runs with or without Pillow installed: the viewer is told to do without it."""

    def test_without_pillow_thumbnails_fall_back_to_original_bytes(self):
        for kwargs in ({'pil': None}, {'thumbnails': False}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                with tempfile.TemporaryDirectory() as base:
                    live = LiveServer(base, **kwargs)
                    try:
                        path = fx.write_zip(fx.standard_export(), os.path.join(base, 's.zip'))
                        eid = live.post_json('/api/exports/open', {'path': path})[1]['exports'][0]['id']
                        self.assertFalse(live.get_json('/api/health')[1]['thumbnails'])
                        image = live.get_json('/api/exports/%s/jobs?sort=oldest' % eid)[1]['items'][0]['images'][0]
                        _, _, original = live.request('GET', image['url'])
                        status, headers, thumb = live.request('GET', image['thumb_url'])
                        self.assertEqual((status, thumb), (200, original))
                        self.assertEqual(headers['Content-Type'], 'image/png')
                        self.assertFalse(os.path.exists(os.path.join(base, 'cache', 'thumbs')))
                    finally:
                        live.stop()


class CliTests(unittest.TestCase):
    def test_port_in_use_exits_cleanly(self):
        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen(1)
        try:
            import contextlib
            with tempfile.TemporaryDirectory() as cache, contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                code = main(['--port', str(busy.getsockname()[1]), '--cache-dir', cache, '--no-thumbnails',
                             os.path.join(cache, 'missing.zip')])
            self.assertEqual(code, 2)
            self.assertIn('Could not open', err.getvalue())
            self.assertIn('Cannot listen', err.getvalue())
        finally:
            busy.close()


if __name__ == '__main__':
    unittest.main()
