"""Hosted mode: catalog validation and refresh, the verified ZIP cache, downloads and their faults, deleting.

Runs against tests/fake_blob.py with invented exports only. The fake container's
query strings (read-only, and one that may write and delete) stand in for real
SAS tokens and must never leak.
"""
import concurrent.futures
import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest import mock

from mjviewer import catalog as catalog_module
from mjviewer import server as server_module
from mjviewer.catalog import (BlobStore, Catalog, CatalogError, Published, ZipCache, parse_catalog)
from mjviewer.model import parse_date
from mjviewer.server import PIL_IMAGE
from tests import fixtures as fx
from tests.fake_blob import REDIRECT_TARGET, FakeBlob
from tests.fake_google import FakeGoogle
from tests.hosted_harness import Hosted

RETRY = b'{"retry": true}'


def age(path: str, seconds: float) -> None:
    """Make a cached ZIP look last used that long ago (eviction and pruning go by modification time)."""
    then = time.time() - seconds
    os.utime(path, (then, then))

SHA = hashlib.sha256(b'x').hexdigest()


def good_entry(**changes):
    entry = {'id': SHA[:16], 'blob': 'exports/%s.zip' % SHA, 'name': 'mj_export_x.zip', 'label': None,
             'sha256': SHA, 'size': 1000, 'published_at': '2026-04-01T10:00:00Z', 'published_by': 'p@example.com',
             'summary': {'jobs': 3, 'moodboards': 1, 'profiles': 2, 'images': 10, 'export_time': 1774000000000,
                         'extension_version': '0.3.1', 'status': 'complete'}}
    entry.update(changes)
    return entry


def catalog_bytes(entries, **top) -> bytes:
    data = {'version': 1, 'updated_at': '2026-04-01T10:00:00Z', 'exports': entries}
    data.update(top)
    return json.dumps(data).encode()


def nested(levels: int):
    """A text inside that many lists."""
    value = 'x'
    for _ in range(levels):
        value = [value]
    return value


class ParseCatalogTests(unittest.TestCase):
    def test_valid_entry_is_normalized(self):
        entries, skipped, updated = parse_catalog(catalog_bytes([good_entry(label='Team export', unknown='kept out')]))
        self.assertEqual(skipped, [])
        self.assertEqual(updated, '2026-04-01T10:00:00.000Z')
        entry = entries[0]
        self.assertEqual(entry['label'], 'Team export')
        self.assertEqual(entry['published_at'], '2026-04-01T10:00:00.000Z')
        self.assertEqual(entry['summary']['export_time'], '2026-03-20T09:46:40.000Z')
        self.assertNotIn('unknown', entry)

    def test_bad_entries_are_skipped_with_a_reason_that_quotes_nothing(self):
        other = hashlib.sha256(b'y').hexdigest()
        cases = [
            ('not an object', 'not an object'),
            (good_entry(sha256=SHA.upper(), id=SHA[:16].upper(), blob='exports/%s.zip' % SHA.upper()), 'lowercase'),
            (good_entry(sha256=SHA[:63]), 'sha256'),
            (good_entry(id=other[:16]), 'id'),
            (good_entry(id='../../../etc/pass'), 'id'),
            (good_entry(blob='exports/../catalog.json'), 'blob'),
            (good_entry(blob='../exports/%s.zip' % SHA), 'blob'),
            (good_entry(blob='exports/%s.zip' % other), 'blob'),
            (good_entry(blob='https://elsewhere.example/x.zip'), 'blob'),
            (good_entry(size=0), 'size'), (good_entry(size=-5), 'size'), (good_entry(size='1000'), 'size'),
            (good_entry(size=True), 'size'), (good_entry(size=catalog_module.MAX_EXPORT_BYTES + 1), 'size'),
            (good_entry(name=''), 'name'), (good_entry(name='a/b.zip'), 'name'), (good_entry(name='a\\b.zip'), 'name'),
            (good_entry(name='x\u0000.zip'), 'name'), (good_entry(name='x' * 300), 'name'),
            (good_entry(label=5), 'label'), (good_entry(label='bad\nlabel'), 'label'),
            (good_entry(published_at='yesterday'), 'published_at'), (good_entry(published_by=['x']), 'published_by'),
            (good_entry(summary=[]), 'summary'), (good_entry(summary={'jobs': -1}), 'summary.jobs'),
            (good_entry(summary={'images': '10'}), 'summary.images'), (good_entry(summary={'status': 5}), 'summary.status'),
        ]
        raw = catalog_bytes([case for case, _ in cases] + [good_entry(), good_entry(name='dup.zip')])
        entries, skipped, _ = parse_catalog(raw)
        self.assertEqual([e['name'] for e in entries], ['mj_export_x.zip'])
        reasons = {s['index']: s['reason'] for s in skipped}
        for n, (_, expect) in enumerate(cases):
            with self.subTest(n=n):
                self.assertIn(expect, reasons[n])
        self.assertIn('duplicate id', reasons[len(cases) + 1])
        for reason in reasons.values():
            for leak in ('etc/pass', 'elsewhere', 'catalog.json', 'yesterday', 'bad\nlabel'):
                self.assertNotIn(leak, reason)

    def test_huge_numbers_and_unexpected_errors_skip_one_entry_only(self):
        huge = good_entry(summary=dict(good_entry()['summary'], export_time=10 ** 400))
        entries, skipped, _ = parse_catalog(catalog_bytes([huge]))
        self.assertEqual((skipped, entries[0]['summary']['export_time']), ([], None))
        other = hashlib.sha256(b'y').hexdigest()
        second = good_entry(id=other[:16], sha256=other, blob='exports/%s.zip' % other, name='second.zip')
        real = catalog_module._entry
        with mock.patch.object(catalog_module, '_entry', side_effect=lambda item: (
                real(item) if item['name'] != 'second.zip' else 1 / 0)):
            entries, skipped, _ = parse_catalog(catalog_bytes([good_entry(), second]))
        self.assertEqual([e['name'] for e in entries], ['mj_export_x.zip'])
        self.assertEqual(skipped, [{'index': 1, 'reason': 'unreadable entry (ZeroDivisionError)'}])

    def test_unusable_catalogs_raise(self):
        for raw in (b'', b'not json', b'[]', b'{"version": 2, "exports": []}', b'{"version": true, "exports": []}',
                    b'{"version": "1", "exports": []}', b'{"version": 1}', b'{"version": 1, "exports": {}}',
                    b'{"version": 1, "exports": [NaN]}', b'\xff\xfe', b'[' * 100000):
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(CatalogError) as caught:
                    parse_catalog(raw)
                self.assertEqual(caught.exception.code, 'bad_catalog')

    def test_only_catalogs_that_can_be_written_back_are_read(self):
        """A number too large for a float would be written back as Infinity, and nesting past the limit cannot be
        written with indentation: such a catalog is refused like invalid JSON, wherever the value is."""
        huge = catalog_bytes([good_entry()], x_scale='HUGE')
        for raw, says in ((huge.replace(b'"HUGE"', b'1e400'), 'holds a number too large to be written back'),
                          (huge.replace(b'"HUGE"', b'-2.5E999'), 'holds a number too large to be written back'),
                          (catalog_bytes([good_entry()], x_deep=nested(64)), 'is nested more than 64 levels deep'),
                          (catalog_bytes([good_entry(x_deep=nested(62))]), 'is nested more than 64 levels deep')):
            with self.subTest(says=says, size=len(raw)):
                with self.assertRaises(CatalogError) as caught:
                    parse_catalog(raw)
                self.assertEqual((caught.exception.code, caught.exception.message),
                                 ('bad_catalog', 'catalog.json %s' % says))
        for raw in (huge.replace(b'"HUGE"', b'1e308'), catalog_bytes([good_entry()], x_deep=nested(63)),
                    catalog_bytes([good_entry(x_deep=nested(61))])):  # up to the limit: read, and written back alike
            with self.subTest(size=len(raw)):
                self.assertEqual(json.loads(catalog_module.dump_catalog(catalog_module._load(raw))), json.loads(raw))

    def test_entry_limit(self):
        old = catalog_module.MAX_ENTRIES
        catalog_module.MAX_ENTRIES = 2
        try:
            shas = [hashlib.sha256(bytes([i])).hexdigest() for i in range(3)]
            entries, skipped, _ = parse_catalog(catalog_bytes([good_entry(id=s[:16], sha256=s, blob='exports/%s.zip' % s)
                                                               for s in shas]))
        finally:
            catalog_module.MAX_ENTRIES = old
        self.assertEqual(len(entries), 2)
        self.assertIn('more than 2 entries', skipped[-1]['reason'])

    def test_owner_is_an_email_address_kept_in_lower_case(self):
        for value, expect in (('Alice@Example.COM', 'alice@example.com'), (None, None), ('', None),
                              ('a.b+tag@sub.example.org', 'a.b+tag@sub.example.org')):
            with self.subTest(owner=value):
                entries, skipped, _ = parse_catalog(catalog_bytes([good_entry(owner=value)]))
                self.assertEqual((skipped, entries[0]['owner']), ([], expect))
        entries, _, _ = parse_catalog(catalog_bytes([good_entry()]))  # a catalog from before owners were recorded
        self.assertIsNone(entries[0]['owner'])
        bad = ['alice', 'alice@', '@example.com', 'a@b@example.com', 'alice@example', 'al ice@example.com',
               'alice@example.com\n', 'ålice@example.com', 'alice@exa_mple.com', 5, True, ['alice@example.com'],
               'a' * 250 + '@example.com', 'K@example.com']  # the Kelvin sign would lower-case to a plain k
        entries, skipped, _ = parse_catalog(catalog_bytes([good_entry(owner=value) for value in bad]))
        self.assertEqual(entries, [])
        self.assertEqual([s['reason'] for s in skipped], ['owner is not an email address'] * len(bad))

    def test_tombstones_are_validated_and_never_hide_an_entry(self):
        other = hashlib.sha256(b'y').hexdigest()
        stones = [{'id': SHA[:16], 'sha256': SHA, 'size': 1000, 'owner': 'p@example.com',
                   'deleted_at': '2026-05-01T10:00:00Z', 'deleted_by': 'p@example.com'},
                  {'id': other[:16], 'sha256': other, 'deleted_at': '2026-05-02T10:00:00.000Z', 'x_more': [1]},
                  {'id': SHA[:15], 'sha256': SHA, 'deleted_at': '2026-05-01T10:00:00Z'},
                  {'id': SHA[:16], 'sha256': SHA.upper(), 'deleted_at': '2026-05-01T10:00:00Z'},
                  {'id': SHA[:16], 'sha256': SHA, 'deleted_at': 'soon'},
                  {'id': SHA[:16], 'sha256': SHA},
                  'not an object', None]
        deleted = []
        entries, skipped, _ = parse_catalog(catalog_bytes([good_entry()], deleted=stones), deleted)
        moment = datetime(2026, 5, 1, 10, tzinfo=timezone.utc).timestamp()
        self.assertEqual(deleted, [{'id': SHA[:16], 'sha256': SHA, 'deleted_ts': moment},
                                   {'id': other[:16], 'sha256': other, 'deleted_ts': moment + 86400}])
        self.assertEqual(([e['id'] for e in entries], skipped), ([SHA[:16]], []))
        for odd in ({}, 'x', None, 5):  # a deleted field that is not a list is ignored
            with self.subTest(deleted=odd):
                found = []
                self.assertEqual(len(parse_catalog(catalog_bytes([good_entry()], deleted=odd), found)[0]), 1)
                self.assertEqual(found, [])


class BlobTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mjv-catalog-')
        self.base = self._tmp.name
        self.blob = FakeBlob(os.path.join(self.base, 'blob'))
        self.logs = []

    def tearDown(self):
        self.blob.stop()
        self._tmp.cleanup()

    def log(self, message):
        self.logs.append(message)

    def assert_no_secret(self, *texts):
        for text in texts:
            text = text if isinstance(text, str) else json.dumps(text)
            for secret in (self.blob.secret, self.blob.query, self.blob.write_secret, self.blob.write_query, 'sig='):
                self.assertNotIn(secret, text)


class CatalogRefreshTests(BlobTestCase):
    def test_etag_refresh_and_keep_last_good(self):
        entry = self.blob.put_export(b'PK\x05\x06' + b'\0' * 18)
        self.blob.write_catalog([entry])
        clock = [100.0]
        cat = Catalog(BlobStore(self.blob.url), ttl_s=60, log=self.log, clock=lambda: clock[0])
        self.assertEqual(cat.health(), {'exports': 0, 'ok': False, 'age_s': None})
        self.assertEqual([e['id'] for e in cat.entries()], [entry['id']])
        self.assertEqual(self.blob.count('catalog.json'), 1)
        clock[0] += 30
        cat.entries()  # fresh: no request
        self.assertEqual(self.blob.count('catalog.json'), 1)
        self.assertEqual(cat.health()['age_s'], 30.0)
        clock[0] += 31
        cat.entries()  # stale: conditional request, 304
        self.assertEqual(self.blob.count('catalog.json', status=304), 1)
        self.assertTrue(self.blob.requests[-1]['if_none_match'])
        self.assertEqual(cat.health(), {'exports': 1, 'ok': True, 'age_s': 0.0})
        # Errors keep the last good catalog and are reported, without the query string.
        for n, (fault, text) in enumerate((('500', 'HTTP 500'), ('403', 'HTTP 403'), ('404', 'HTTP 404')), 1):
            with self.subTest(fault=fault):
                self.blob.faults['catalog.json'] = fault
                clock[0] += 61
                self.assertEqual([e['id'] for e in cat.entries()], [entry['id']])
                status = cat.status()
                self.assertFalse(status['ok'])
                self.assertIn(text, status['error'])
                self.assertEqual(cat.health()['age_s'], 61.0 * n)  # the age of the copy still served
        self.blob.faults.clear()
        self.blob.put('catalog.json', b'{"version": 1, "exports": "nope"}')
        clock[0] += 61
        self.assertEqual(len(cat.entries()), 1)
        self.assertIn('no exports list', cat.status()['error'])
        # A new valid catalog replaces it; skipped entries are reported, never fetched.
        second = self.blob.put_export(b'PK\x05\x06' + b'\1' * 18, name='second.zip')
        self.blob.write_catalog([entry, second, dict(second, blob='exports/../catalog.json')])
        clock[0] += 61
        self.assertEqual([e['name'] for e in cat.entries()], ['mj_export_synthetic.zip', 'second.zip'])
        self.assertEqual(cat.status()['skipped'][0]['index'], 2)
        self.assertTrue(cat.status()['ok'])
        self.assertEqual([r['name'] for r in self.blob.requests if r['name'] != 'catalog.json'], [])
        self.assert_no_secret('\n'.join(self.logs), cat.status())

    def test_an_unexpected_error_keeps_the_last_good_catalog_and_the_ttl(self):
        entry = self.blob.put_export(b'PK\x05\x06' + b'\0' * 18)
        self.blob.write_catalog([entry])
        clock = [0.0]
        cat = Catalog(BlobStore(self.blob.url), ttl_s=60, log=self.log, clock=lambda: clock[0])
        self.assertEqual(len(cat.entries()), 1)
        self.blob.write_catalog([entry, dict(entry, name='other.zip')])  # a change, so the next fetch is a 200
        clock[0] += 61
        with mock.patch.object(catalog_module, 'parse_catalog', side_effect=OverflowError):
            for _ in range(5):
                self.assertEqual(len(cat.entries()), 1)
        self.assertEqual(self.blob.count('catalog.json'), 2)  # one attempt per TTL, not one per request
        self.assertEqual(cat.health(), {'exports': 1, 'ok': False, 'age_s': 61.0})
        self.assertIn('could not be read (OverflowError)', cat.status()['error'])

    def test_a_catalog_not_read_for_too_long_is_outdated(self):
        """After max(5 minutes, 5 x MJV_CATALOG_TTL_S) without a good answer (a 200 that parses, or a 304), counted
        from the start until the first one."""
        self.blob.write_catalog([])
        for ttl, limit in ((0, 300), (60, 300), (600, 3000)):
            with self.subTest(ttl=ttl):
                clock = [5000.0]
                cat = Catalog(BlobStore(self.blob.url), ttl_s=ttl, clock=lambda: clock[0])
                self.assertEqual(cat.unread_limit_s, limit)
                for answer in (200, 304):
                    self.blob.faults['catalog.json'] = '500'
                    clock[0] += limit - 1
                    cat.entries()
                    self.assertFalse(cat.outdated())
                    clock[0] += 1
                    cat.entries()
                    self.assertTrue(cat.outdated())
                    self.blob.faults.clear()
                    clock[0] += ttl
                    cat.entries()
                    self.assertFalse(cat.outdated())
                    self.assertEqual(self.blob.requests[-1]['status'], answer)
                self.blob.put('catalog.json', b'{"version": 1}')  # read, but not a catalog: no good read either
                clock[0] += limit
                cat.entries()
                self.assertTrue(cat.outdated())
                self.blob.write_catalog([])

    def test_a_catalog_over_the_size_cap_is_refused(self):
        self.blob.put('catalog.json', b' ' * (catalog_module.MAX_CATALOG_BYTES + 1))
        cat = Catalog(BlobStore(self.blob.url), ttl_s=60)
        self.assertEqual(cat.entries(), [])
        self.assertIn('larger than %d bytes' % catalog_module.MAX_CATALOG_BYTES, cat.status()['error'])

    def test_redirects_are_never_followed(self):
        self.blob.write_catalog([])
        self.blob.faults['catalog.json'] = '302'
        cat = Catalog(BlobStore(self.blob.url), ttl_s=60)
        cat.entries()
        self.assertIn('redirect (HTTP 302), which is not followed', cat.status()['error'])
        self.assertEqual(self.blob.count(REDIRECT_TARGET), 0)

    def test_scrub_blanks_the_query_string_in_every_form(self):
        store = BlobStore(self.blob.url)
        sig = self.blob.secret
        forms = (sig, urllib.parse.quote(sig, safe=''), urllib.parse.quote_plus(sig), self.blob.query)
        for text in ('GET %s failed' % self.blob.url, 'sig=%s' % sig, 'quoted %s' % forms[1], 'plus %s' % forms[2]):
            with self.subTest(text=text[:20]):
                scrubbed = store.scrub(text)
                self.assertIn('***', scrubbed)
                for form in forms:
                    self.assertNotIn(form, scrubbed)
        self.assertEqual(store.scrub('nothing secret here'), 'nothing secret here')

    def test_unreachable_storage(self):
        store = BlobStore('http://127.0.0.1:9/exports?sp=rl&sig=' + 'S' * 30, timeout=2)
        cat = Catalog(store, ttl_s=0, log=self.log)
        self.assertEqual(cat.entries(), [])
        self.assertIn('unreachable', cat.status()['error'])
        self.assertNotIn('S' * 30, cat.status()['error'] + '\n'.join(self.logs))

    def test_concurrent_refreshes_share_one_request(self):
        self.blob.write_catalog([])
        self.blob.faults['catalog.json'] = 'slow'
        self.blob.slow_delay = 0.3
        cat = Catalog(BlobStore(self.blob.url), ttl_s=60)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: cat.entries(), range(8)))
        self.assertEqual(self.blob.count('catalog.json'), 1)

    def test_a_stale_catalog_holds_up_only_the_request_that_refreshes_it(self):
        entry = self.blob.put_export(b'PK\x05\x06' + b'\0' * 18)
        self.blob.write_catalog([entry])
        clock = [0.0]
        cat = Catalog(BlobStore(self.blob.url), ttl_s=60, clock=lambda: clock[0])
        self.assertEqual(len(cat.entries()), 1)
        self.blob.faults['catalog.json'] = 'slow'
        self.blob.slow_delay = 1.0
        clock[0] += 61
        leader = threading.Thread(target=cat.entries)
        leader.start()
        for _ in range(100):
            if self.blob.count('catalog.json') == 2:
                break
            time.sleep(0.02)
        started = time.monotonic()
        self.assertEqual(len(cat.entries()), 1)  # the last good copy, at once
        self.assertLess(time.monotonic() - started, 0.5)
        leader.join(10)
        self.assertFalse(leader.is_alive())


class BlobWriteTests(BlobTestCase):
    def test_conditional_writes_and_deletes(self):
        store = BlobStore(self.blob.url, write_url=self.blob.write_url)
        self.assertTrue(store.writable)
        self.blob.write_catalog([])
        _, etag, _ = store.get('catalog.json', 'catalog.json')
        body = b'{"version": 1, "exports": []}\n'
        new = store.put('catalog.json', body, 'catalog.json', if_match=etag)
        self.assertTrue(new and new != etag)
        for condition in ({'if_match': etag}, {'if_none_match': '*'}):  # changed since it was read; exists already
            with self.subTest(condition=condition):
                with self.assertRaises(CatalogError) as caught:
                    store.put('catalog.json', b'{}', 'catalog.json', **condition)
                self.assertEqual(caught.exception.code, 'changed')
        self.assertEqual(self.blob.read_json('catalog.json'), {'version': 1, 'exports': []})
        self.assertTrue(store.put('new.json', b'{}', 'new.json', if_none_match='*'))
        self.assertNotEqual(store.put('catalog.json', body, 'catalog.json', if_match=new), new)  # same bytes, new ETag
        puts = [r for r in self.blob.requests if r['method'] == 'PUT']
        self.assertEqual([r['status'] for r in puts], [201, 412, 412, 201, 201])
        self.assertEqual({r['content_type'] for r in puts}, {'application/json'})
        self.assertTrue(store.delete('new.json', 'new.json'))
        self.assertFalse(store.delete('new.json', 'new.json'))  # already gone
        self.assertEqual([r['status'] for r in self.blob.requests if r['method'] == 'DELETE'], [202, 404])
        # Reads always carry the read-only query string; writes and deletes the other one.
        self.assertEqual({(r['method'], r['sas']) for r in self.blob.requests},
                         {('GET', 'read'), ('PUT', 'write'), ('DELETE', 'write')})

    def test_a_read_only_query_string_never_writes(self):
        self.blob.write_catalog([])
        before = self.blob.read_json('catalog.json')
        store = BlobStore(self.blob.url)  # no write access configured: nothing is even sent
        self.assertFalse(store.writable)
        for call in (lambda s: s.put('catalog.json', b'{}', 'catalog.json', if_match='"x"'),
                     lambda s: s.delete('catalog.json', 'catalog.json')):
            with self.assertRaises(CatalogError) as caught:
                call(store)
            self.assertEqual(caught.exception.code, 'read_only')
        self.assertEqual(self.blob.requests, [])
        store = BlobStore(self.blob.url, write_url=self.blob.url)  # the read-only query string used to write: refused
        for call in (lambda s: s.put('catalog.json', b'{}', 'catalog.json', if_match='"x"'),
                     lambda s: s.delete('catalog.json', 'catalog.json')):
            with self.assertRaises(CatalogError) as caught:
                call(store)
            self.assertEqual(caught.exception.code, 'forbidden')
            self.assertIn('write access may have expired', caught.exception.message)
        self.assertEqual([r['status'] for r in self.blob.requests], [403, 403])
        self.assertEqual(self.blob.read_json('catalog.json'), before)

    def test_write_faults_and_redirects(self):
        store = BlobStore(self.blob.url, write_url=self.blob.write_url)
        self.blob.write_catalog([])
        # A 412 to a DELETE that sent no condition is no sign of a new upload (storage also answers 412 for a lease).
        for fault, put_code, delete_code in (('302', 'redirected', 'redirected'), ('500', 'storage_error', 'storage_error'),
                                             ('403', 'forbidden', 'forbidden'), ('409', 'changed', 'storage_error'),
                                             ('412', 'changed', 'storage_error'), ('lease', 'changed', 'storage_error')):
            with self.subTest(fault=fault):
                self.blob.write_faults['catalog.json'] = fault
                for call, code in ((lambda: store.put('catalog.json', b'{}', 'catalog.json', if_match='"x"'), put_code),
                                   (lambda: store.delete('catalog.json', 'catalog.json'), delete_code)):
                    with self.assertRaises(CatalogError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, code)
                    self.assert_no_secret(caught.exception.message, repr(caught.exception))
        # With If-Unmodified-Since, a 412 means written since, unless storage names another reason.
        for fault, code, says in (('412', 'changed', 'catalog.json was written again since'),
                                  ('lease', 'storage_error', 'catalog.json: storage answered HTTP 412 (LeaseIdMissing)')):
            with self.subTest(fault=fault, condition=True):
                self.blob.write_faults['catalog.json'] = fault
                with self.assertRaises(CatalogError) as caught:
                    store.delete('catalog.json', 'catalog.json', unmodified_since=time.time())
                self.assertEqual((caught.exception.code, caught.exception.message), (code, says))
        self.blob.write_faults.clear()
        self.assertEqual(self.blob.count(REDIRECT_TARGET), 0)
        self.assertIsNotNone(self.blob.file('catalog.json'))
        unreachable = BlobStore('http://127.0.0.1:9/c?sp=rl&sig=' + 'R' * 30, timeout=2,
                                write_url='http://127.0.0.1:9/c?sp=rwdl&sig=' + 'W' * 30)
        for call in (lambda: unreachable.put('catalog.json', b'{}', 'catalog.json'),
                     lambda: unreachable.delete('catalog.json', 'catalog.json')):
            with self.assertRaises(CatalogError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'unreachable')
            self.assertNotIn('W' * 30, caught.exception.message)

    def test_scrub_blanks_both_query_strings(self):
        store = BlobStore(self.blob.url, write_url=self.blob.write_url)
        for query in (self.blob.query, self.blob.write_query):
            sig = dict(urllib.parse.parse_qsl(query))['sig']
            forms = (sig, urllib.parse.quote(sig, safe=''), urllib.parse.quote_plus(sig), query)
            for text in ('%s?%s failed' % (self.blob.base_url, query), 'sig=%s' % sig, 'quoted %s' % forms[1]):
                with self.subTest(text=text[:24]):
                    scrubbed = store.scrub(text)
                    self.assertIn('***', scrubbed)
                    for form in forms:
                        self.assertNotIn(form, scrubbed)


class ZipCacheTests(BlobTestCase):
    def setUp(self):
        super().setUp()
        self.cache_dir = os.path.join(self.base, 'cache')
        self.store = BlobStore(self.blob.url)
        self.stop = threading.Event()

    def cache(self, max_bytes=10 ** 9):
        return ZipCache(self.cache_dir, max_bytes, log=self.log)

    def entry(self, data: bytes, **kwargs) -> dict:
        return parse_catalog(catalog_bytes([self.blob.put_export(data, **kwargs)]))[0][0]

    def leftovers(self):
        folder = os.path.join(self.cache_dir, 'zips')
        return sorted(n for n in os.listdir(folder) if n.startswith('.')) if os.path.isdir(folder) else []

    def test_download_verifies_and_publishes_atomically(self):
        data = os.urandom(300_000)
        entry = self.entry(data)
        cache = self.cache()
        seen = []
        cache.download(entry, self.store, seen.append, self.stop)
        path = cache.path(entry['sha256'])
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), data)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o444)
        self.assertEqual(seen[-1], len(data))
        self.assertTrue(cache.is_verified(entry['sha256'], len(data)))
        self.assertEqual(self.leftovers(), [])
        # Another process sees the file but must verify it once before use.
        other = self.cache()
        self.assertTrue(other.present(entry['sha256']))
        self.assertFalse(other.is_verified(entry['sha256'], len(data)))
        self.assertTrue(other.verify(entry['sha256'], len(data), lambda n: None, self.stop))
        self.assertTrue(other.is_verified(entry['sha256'], len(data)))

    def test_download_faults_leave_nothing_behind(self):
        data = os.urandom(200_000)
        cases = {
            '403': ('forbidden', None), '404': ('not_found', None), '500': ('storage_error', None),
            'truncate': ('truncated', None),
        }
        cache = self.cache()
        for fault, (code, _) in cases.items():
            with self.subTest(fault=fault):
                entry = self.entry(data + fault.encode())
                self.blob.faults[entry['blob']] = fault
                with self.assertRaises(CatalogError) as caught:
                    cache.download(entry, self.store, lambda n: None, self.stop)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(cache.present(entry['sha256']))
                self.assertEqual(self.leftovers(), [])
                self.assert_no_secret(caught.exception.message, str(caught.exception), repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)
        # The bytes do not match the published hash, or the size is wrong.
        wrong = self.entry(data, sha256=hashlib.sha256(b'other').hexdigest())
        with self.assertRaises(CatalogError) as caught:
            cache.download(wrong, self.store, lambda n: None, self.stop)
        self.assertEqual(caught.exception.code, 'sha_mismatch')
        for size in (len(data) - 1, len(data) + 1):
            entry = self.entry(data + b'size', size=size)
            with self.assertRaises(CatalogError) as caught:
                cache.download(entry, self.store, lambda n: None, self.stop)
            self.assertEqual(caught.exception.code, 'size_mismatch')
        # A Content-Length that differs from the catalog stops the download before a byte is kept.
        seen = []
        entry = self.entry(data + b'short', size=len(data) + 6)
        with self.assertRaises(CatalogError) as caught:
            cache.download(entry, self.store, seen.append, self.stop)
        self.assertEqual((caught.exception.code, seen), ('size_mismatch', []))
        self.assertEqual(self.leftovers(), [])
        self.assertEqual([n for n in os.listdir(os.path.join(self.cache_dir, 'zips'))], [])

    def test_unreachable_storage_is_described_without_the_url(self):
        cache = self.cache()
        store = BlobStore('http://127.0.0.1:9/exports?sp=rl&sig=' + 'Q' * 30, timeout=2)
        with self.assertRaises(CatalogError) as caught:
            cache.download(self.entry(b'abc' * 100), store, lambda n: None, self.stop)
        self.assertEqual(caught.exception.code, 'unreachable')
        self.assertNotIn('Q' * 30, caught.exception.message)
        self.assertNotIn('127.0.0.1', caught.exception.message)
        self.assertEqual(self.leftovers(), [])

    def test_locks_are_exclusive_and_stale_ones_are_broken(self):
        first, second = self.cache(), self.cache()
        self.assertTrue(first.try_lock(SHA))
        self.assertFalse(second.try_lock(SHA))
        self.assertTrue(second.locked_elsewhere(SHA))
        second.unlock(SHA)  # not its lock: kept
        self.assertFalse(second.try_lock(SHA))
        first.unlock(SHA)
        self.assertTrue(second.try_lock(SHA))
        lock = os.path.join(self.cache_dir, 'zips', '.dl-%s.lock' % SHA)
        old = time.time() - catalog_module.LOCK_STALE_S - 5
        os.utime(lock, (old, old))  # its owner died
        self.assertFalse(first.locked_elsewhere(SHA))
        self.assertTrue(first.try_lock(SHA))
        first.unlock(SHA)
        self.assertFalse(os.path.exists(lock))

    def test_an_unwritable_cache_fails_cleanly(self):
        if os.geteuid() == 0:
            self.skipTest('root can write anywhere')
        os.makedirs(self.cache_dir)
        os.chmod(self.cache_dir, 0o555)
        try:
            with self.assertRaises(CatalogError) as caught:
                self.cache().try_lock(SHA)
        finally:
            os.chmod(self.cache_dir, 0o755)
        self.assertEqual(caught.exception.code, 'cache_unwritable')

    def test_cleanup_removes_only_abandoned_files(self):
        folder = os.path.join(self.cache_dir, 'zips')
        os.makedirs(folder)
        names = {'.dl-%s-aaaa-1.part' % SHA: 3600, '.dl-%s-bbbb-2.part' % SHA: 5, '.dl-%s.lock' % SHA: 3600,
                 '.dl-%s.lock' % SHA[::-1]: 5, '%s.zip' % SHA: 99999}
        for name, age in names.items():
            path = os.path.join(folder, name)
            with open(path, 'wb') as handle:
                handle.write(b'x')
            os.utime(path, (time.time() - age, time.time() - age))
        self.assertEqual(self.cache().cleanup(), 2)
        self.assertEqual(sorted(os.listdir(folder)), sorted(['.dl-%s-bbbb-2.part' % SHA, '.dl-%s.lock' % SHA[::-1],
                                                             '%s.zip' % SHA]))

    def test_lru_eviction_keeps_files_in_use(self):
        cache = self.cache(max_bytes=250_000)
        entries = [self.entry(os.urandom(100_000), name='%d.zip' % i) for i in range(3)]
        for n, entry in enumerate(entries[:2]):
            cache.download(entry, self.store, lambda n: None, self.stop)
            age(cache.path(entry['sha256']), catalog_module.PRUNE_MIN_AGE_S + 100 - n)
        cache.touch(entries[0]['sha256'])  # used most recently: entries[1] is the least recently used
        self.assertEqual(cache.make_room(100_000, keep=entries[2]['sha256'], in_use=set()), [entries[1]['sha256']])
        self.assertTrue(cache.present(entries[0]['sha256']))
        self.assertFalse(cache.present(entries[1]['sha256']))
        cache.download(entries[2], self.store, lambda n: None, self.stop)
        # Everything else in use, here or (recently touched) anywhere: nothing is evicted and the download is
        # refused, so the cache never grows past its limit.
        for in_use in ({entries[0]['sha256'], entries[2]['sha256']}, set()):
            with self.subTest(in_use=len(in_use)):
                with self.assertRaises(CatalogError) as caught:
                    cache.make_room(100_000, keep='f' * 64, in_use=in_use)
                self.assertEqual(caught.exception.code, 'cache_full')
                self.assertTrue(cache.present(entries[0]['sha256']) and cache.present(entries[2]['sha256']))
        # Downloads under way (of any instance) count as well.
        for entry in (entries[0], entries[2]):
            age(cache.path(entry['sha256']), catalog_module.PRUNE_MIN_AGE_S + 10)
        self.assertEqual(len(cache.make_room(100_000, keep='f' * 64, in_use=set(), reserved=100_000)), 2)
        with self.assertRaises(CatalogError) as caught:
            cache.make_room(300_000, keep='f' * 64, in_use=set())
        self.assertEqual(caught.exception.code, 'too_large_for_cache')

    def test_downloads_under_way_are_listed_from_their_locks(self):
        first, second = self.cache(), self.cache()
        self.assertTrue(first.try_lock(SHA))
        self.assertEqual(second.active_downloads(), {SHA})
        os.utime(first._lock_path(SHA), (time.time() - catalog_module.LOCK_STALE_S - 5,) * 2)
        self.assertEqual(second.active_downloads(), set())

    def test_a_replaced_file_is_verified_again(self):
        data = os.urandom(50_000)
        entry = self.entry(data)
        cache = self.cache()
        cache.download(entry, self.store, lambda n: None, self.stop)
        path = cache.path(entry['sha256'])
        self.assertTrue(cache.is_verified(entry['sha256'], len(data)))
        for content, good in ((data, True), (bytes(len(data)), False)):
            with self.subTest(good=good):
                other = path + '.new'
                with open(other, 'wb') as handle:
                    handle.write(content)
                os.replace(other, path)  # another process put a new file (a new inode) in its place
                self.assertFalse(cache.is_verified(entry['sha256'], len(data)))
                self.assertEqual(cache.verify(entry['sha256'], len(data), lambda n: None, self.stop), good)
                self.assertEqual(cache.is_verified(entry['sha256'], len(data)), good)

    def test_links_in_the_cache_are_never_followed(self):
        outside = os.path.join(self.base, 'outside')
        os.makedirs(outside)
        targets = []
        for n in range(2):
            target = os.path.join(outside, 'private-%d' % n)
            with open(target, 'wb') as handle:
                handle.write(b'x' * 64)
            os.chmod(target, 0o600)
            targets.append(target)
        folder = os.path.join(self.cache_dir, 'zips')
        os.makedirs(folder)
        lock = os.path.join(folder, '.dl-%s.lock' % SHA)
        os.symlink(targets[0], lock)
        old = time.time() - 3600
        os.utime(lock, (old, old), follow_symlinks=False)
        os.symlink(targets[1], os.path.join(folder, '%s.zip' % SHA))
        cache = self.cache()
        self.assertFalse(cache.present(SHA))  # a link is never taken for a cached ZIP
        self.assertFalse(cache.verify(SHA, 64, lambda n: None, self.stop))
        self.assertEqual(cache.cleanup(), 1)
        cache.discard(SHA)
        self.assertEqual(os.listdir(folder), [])
        self.assertEqual([stat.S_IMODE(os.stat(t).st_mode) for t in targets], [0o600, 0o600])

    def test_a_stream_without_content_length_is_still_counted(self):
        data = os.urandom(100_000)
        cache = self.cache()
        exact = self.entry(data)
        self.blob.faults[exact['blob']] = 'nolength'
        cache.download(exact, self.store, lambda n: None, self.stop)
        self.assertTrue(cache.is_verified(exact['sha256'], len(data)))
        for extra, code in ((-1, 'size_mismatch'), (1, 'truncated')):
            with self.subTest(code=code):
                content = data + code.encode()
                entry = self.entry(content, size=len(content) + extra)
                self.blob.faults[entry['blob']] = 'nolength'
                with self.assertRaises(CatalogError) as caught:
                    cache.download(entry, self.store, lambda n: None, self.stop)
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.leftovers(), [])

    def test_prune_deletes_unlisted_zips(self):
        cache = self.cache()
        kept, dropped, recent = (self.entry(os.urandom(1000), name='%s.zip' % n) for n in ('kept', 'dropped', 'recent'))
        for entry in (kept, dropped, recent):
            cache.download(entry, self.store, lambda n: None, self.stop)
        old = time.time() - catalog_module.PRUNE_MIN_AGE_S - 10
        for entry in (kept, dropped):
            os.utime(cache.path(entry['sha256']), (old, old))
        removed = cache.prune({kept['sha256']}, in_use=set())
        self.assertEqual(removed, [dropped['sha256']])
        self.assertTrue(cache.present(recent['sha256']))  # used recently: another instance may list it already


class FlowTestCase(BlobTestCase):
    """Hosted instances on the fake container, driven through the HTTP API."""

    def setUp(self):
        super().setUp()
        self.google = FakeGoogle()
        self.instances = []

    def tearDown(self):
        for hosted in self.instances:
            hosted.stop()
        self.google.stop()
        super().tearDown()

    def start(self, cache='cache', **env):
        hosted = Hosted(self.base, self.blob, self.google, env=dict({'MJV_CACHE_DIR': os.path.join(self.base, cache)},
                                                                    **env))
        self.instances.append(hosted)
        return hosted, hosted.session()

    def publish(self, export=None, **kwargs):
        entry = self.blob.put_export(fx.export_zip_bytes(export or fx.standard_export()), **kwargs)
        return entry

    def item(self, hosted, session, cid):
        status, _, body = hosted.get('/api/catalog/%s' % cid, session)
        self.assertEqual(status, 200)
        return json.loads(body)

    def wait(self, hosted, session, cid, states=('ready', 'failed'), timeout=30):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            item = self.item(hosted, session, cid)
            seen.append(item)
            if item['state'] in states:
                return item, seen
            time.sleep(0.02)
        raise AssertionError('state stayed %s' % seen[-1]['state'])

    def zips(self, cache='cache'):
        folder = os.path.join(self.base, cache, 'zips')
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def open(self, hosted, session, cid, retry=False):
        return hosted.post('/api/catalog/%s/open' % cid, session, body=RETRY if retry else b'{}')

    def slow(self, entry, chunk=4096, delay=0.02):
        self.blob.faults[entry['blob']] = 'slow'
        self.blob.chunk, self.blob.slow_delay = chunk, delay


class DownloadFlowTests(FlowTestCase):
    """Through the HTTP API: open, poll, ready, faults, concurrency and two instances."""

    def test_open_download_verify_and_view(self):
        entry = self.publish(label='Team export', name='mj_export_team.zip')
        self.blob.write_catalog([entry])
        hosted, session = self.start()
        listing = json.loads(hosted.get('/api/exports', session)[2])
        item = listing['catalog']['items'][0]
        self.assertEqual((item['state'], item['label'], item['name'], item['exports']),
                         ('absent', 'Team export', 'mj_export_team.zip', []))
        self.assertTrue(listing['catalog']['ok'])
        self.assertEqual(listing['exports'], [])
        # A GET of an export that is not open never starts a download.
        status, _, body = hosted.get('/api/exports/%s/overview' % entry['id'], session)
        self.assertEqual(status, 409)
        answer = json.loads(body)
        self.assertEqual((answer['error']['code'], answer['catalog']['state']), ('export_not_ready', 'absent'))
        self.assertEqual(hosted.get('/files/%s/0123456789abcdef0123' % entry['id'], session)[0], 409)
        self.assertEqual(self.blob.count(entry['blob']), 0)
        status, _, body = hosted.post('/api/catalog/%s/open' % entry['id'], session)
        self.assertEqual(status, 202)
        item, _ = self.wait(hosted, session, entry['id'])
        self.assertEqual(item['state'], 'ready', item)
        self.assertEqual(self.blob.count(entry['blob']), 1)
        summary = item['exports'][0]
        self.assertEqual((summary['id'], summary['label']), (entry['id'], 'Team export'))
        self.assertEqual(summary['source'], {'type': 'catalog', 'name': 'mj_export_team.zip', 'path': None})
        overview = json.loads(hosted.get('/api/exports/%s/overview' % entry['id'], session)[2])
        self.assertEqual(overview['counts']['observed']['jobs'], 9)
        jobs = json.loads(hosted.get('/api/exports/%s/jobs?limit=1' % entry['id'], session)[2])
        status, headers, image = hosted.get(jobs['items'][0]['images'][0]['thumb_url'], session)
        self.assertEqual(status, 200)
        self.assertTrue(headers['Content-Type'].startswith('image/'))
        # A browser keeps images for 10 minutes at most: a deleted export's are gone from its cache by then.
        status, original, _ = hosted.get(jobs['items'][0]['images'][0]['url'], session)
        self.assertEqual([status, headers['Cache-Control'], original['Cache-Control']],
                         [200, 'private, max-age=600', 'private, max-age=600'])
        # Opening again reuses everything; nothing exposes the cache path.
        self.assertEqual(hosted.post('/api/catalog/%s/open' % entry['id'], session)[0], 200)
        self.assertEqual(self.blob.count(entry['blob']), 1)
        for path in ('/api/exports', '/api/exports/%s/overview' % entry['id'], '/api/catalog/%s' % entry['id']):
            self.assertNotIn(hosted.config.cache_dir.encode(), hosted.get(path, session)[2])
        self.assertEqual(self.zips(), ['%s.zip' % entry['sha256']])

    def test_faults_fail_with_a_reason_and_can_be_retried(self):
        entry = self.publish()
        broken = self.blob.put_export(b'PK\x03\x04 not really a zip at all', name='broken.zip')
        self.blob.write_catalog([entry, broken])
        hosted, session = self.start()
        for fault, code in (('403', 'forbidden'), ('500', 'storage_error'), ('truncate', 'truncated'),
                            ('404', 'not_found'), ('302', 'redirected')):
            with self.subTest(fault=fault):
                self.blob.faults[entry['blob']] = fault
                self.open(hosted, session, entry['id'], retry=True)
                item, _ = self.wait(hosted, session, entry['id'])
                self.assertEqual((item['state'], item['error']['code']), ('failed', code))
                self.assert_no_secret(item)
                self.assertEqual(self.zips(), [])
                # A failed export answers 409 with the reason; GET does not retry it.
                status, _, body = hosted.get('/api/exports/%s/jobs' % entry['id'], session)
                self.assertEqual((status, json.loads(body)['catalog']['state']), (409, 'failed'))
        self.assertEqual(self.blob.count(REDIRECT_TARGET), 0)  # the redirect was not followed
        self.blob.faults.clear()
        self.open(hosted, session, entry['id'], retry=True)
        self.assertEqual(self.wait(hosted, session, entry['id'])[0]['state'], 'ready')
        hosted.post('/api/catalog/%s/open' % broken['id'], session)
        item, _ = self.wait(hosted, session, broken['id'])
        self.assertEqual((item['state'], item['error']['code']), ('failed', 'bad_zip'))  # a ZIP start, no end record
        self.assertIn('broken.zip', item['error']['message'])
        self.assertNotIn(broken['sha256'], item['error']['message'])
        self.assertNotIn(hosted.config.cache_dir, item['error']['message'])
        log = hosted.log.getvalue()
        self.assert_no_secret(log)
        self.assertIn('prepare %s failed: the published ZIP: storage refused the request (HTTP 403)' % entry['id'], log)

    def test_a_failure_starts_again_only_on_request(self):
        """A poll that reaches an instance without the failure must never restart a broken download (E2E-1)."""
        data = fx.export_zip_bytes(fx.standard_export())
        lying = self.blob.put_export(data, name='lying.zip', sha256=hashlib.sha256(b'other').hexdigest())
        self.blob.write_catalog([lying])
        hosted, session = self.start()
        status, _, body = self.open(hosted, session, lying['id'])
        self.assertEqual(status, 202)
        item, _ = self.wait(hosted, session, lying['id'])
        self.assertEqual((item['state'], item['error']['code']), ('failed', 'sha_mismatch'))
        self.assertEqual(self.blob.count(lying['blob']), 1)
        for body in (b'{}', b'', b'{"retry": false}', b'{"retry": "yes"}'):
            with self.subTest(body=body):
                status, _, answer = hosted.post('/api/catalog/%s/open' % lying['id'], session, body=body)
                self.assertEqual((status, json.loads(answer)['state']), (200, 'failed'))
        time.sleep(0.3)
        self.assertEqual(self.blob.count(lying['blob']), 1)  # nothing started again
        status, _, answer = self.open(hosted, session, lying['id'], retry=True)
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(hosted, session, lying['id'])[0]['state'], 'failed')
        self.assertEqual(self.blob.count(lying['blob']), 2)
        for body, code in ((b'[1]', 400), (b'not json', 400), (b'{"retry": true, "x": "' + b'a' * 5000 + b'"}', 413)):
            with self.subTest(body=body[:12]):
                self.assertEqual(hosted.post('/api/catalog/%s/open' % lying['id'], session, body=body)[0], code)

    def test_requests_never_wait_for_a_download(self):
        entry = self.publish(fx.gallery_export(60))
        self.blob.write_catalog([entry])
        self.slow(entry, chunk=1024, delay=0.05)
        hosted, session = self.start()
        self.open(hosted, session, entry['id'])
        self.wait(hosted, session, entry['id'], states=('downloading',))
        for path in ('/api/exports/%s/overview' % entry['id'], '/files/%s/0123456789abcdef0123' % entry['id']):
            with self.subTest(path=path):
                started = time.monotonic()
                status, _, body = hosted.get(path, session)
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertEqual((status, json.loads(body)['catalog']['state']), (409, 'downloading'))
        self.blob.slow_delay = 0  # let it finish quickly

    def test_closing_removes_the_temporary_file_and_the_lock(self):
        entry = self.publish(fx.gallery_export(60))
        self.blob.write_catalog([entry])
        self.slow(entry, chunk=1024, delay=0.05)
        hosted, session = self.start()
        self.open(hosted, session, entry['id'])
        deadline = time.time() + 20
        while not any(n.endswith('.part') for n in self.zips()) and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(any(n.endswith('.part') for n in self.zips()))  # the download streams into the cache folder
        self.assertTrue(any(n.endswith('.lock') for n in self.zips()))
        hosted.stop()  # what SIGTERM leads to: it waits for the download to stop
        self.instances.remove(hosted)
        self.assertEqual(self.zips(), [])
        self.assertIn('the server is stopping', hosted.log.getvalue())

    def test_sha_mismatch_and_slow_progress(self):
        export = fx.gallery_export(40)
        data = fx.export_zip_bytes(export)
        lying = self.blob.put_export(data, name='lying.zip', sha256=hashlib.sha256(b'other').hexdigest())
        slow = self.publish(export, name='slow.zip')
        self.blob.write_catalog([lying, slow])
        self.blob.faults[slow['blob']] = 'slow'
        self.blob.chunk, self.blob.slow_delay = 4096, 0.01
        hosted, session = self.start()
        hosted.post('/api/catalog/%s/open' % lying['id'], session)
        item, _ = self.wait(hosted, session, lying['id'])
        self.assertEqual(item['error']['code'], 'sha_mismatch')
        hosted.post('/api/catalog/%s/open' % slow['id'], session)
        item, seen = self.wait(hosted, session, slow['id'])
        self.assertEqual(item['state'], 'ready')
        partial = [s['progress']['done'] for s in seen if s['state'] == 'downloading' and s['progress']]
        self.assertTrue(any(0 < done < slow['size'] for done in partial), partial[:5])
        self.assertTrue(all(s['progress']['total'] == slow['size'] for s in seen if s['progress']))
        self.assertEqual(self.zips(), ['%s.zip' % slow['sha256']])

    def test_concurrent_opens_download_once(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        self.blob.faults[entry['blob']] = 'slow'
        hosted, session = self.start()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            statuses = list(pool.map(lambda _: hosted.post('/api/catalog/%s/open' % entry['id'], session)[0], range(8)))
        self.assertTrue(set(statuses) <= {200, 202})
        self.assertEqual(self.wait(hosted, session, entry['id'])[0]['state'], 'ready')
        self.assertEqual(self.blob.count(entry['blob']), 1)

    def test_two_instances_share_one_cache(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        self.blob.faults[entry['blob']] = 'slow'
        self.blob.chunk, self.blob.slow_delay = 8192, 0.01
        first, first_session = self.start()
        second, second_session = self.start()
        self.assertNotEqual(first.port, second.port)
        first.post('/api/catalog/%s/open' % entry['id'], first_session)
        # While the first instance downloads, the second waits for its file and shows its progress.
        self.wait(second, second_session, entry['id'], states=('downloading',))
        status, _, body = second.post('/api/catalog/%s/open' % entry['id'], second_session)
        self.assertEqual((status, json.loads(body)['state'] in ('queued', 'downloading')), (202, True))
        _, seen = self.wait(second, second_session, entry['id'])
        done = [s['progress']['done'] for s in seen if s['state'] == 'downloading']
        self.assertNotIn('waiting', [s['state'] for s in seen])  # its own state while it waits is shown as downloading
        self.assertTrue(done and max(done) > 0, seen[:3])
        both = [self.wait(h, s, entry['id'])[0] for h, s in ((first, first_session), (second, second_session))]
        self.assertEqual([i['state'] for i in both], ['ready', 'ready'])
        self.assertEqual(self.blob.count(entry['blob']), 1)
        self.assertEqual(self.zips(), ['%s.zip' % entry['sha256']])
        # Thumbnails written by one instance are reused by the other (same ids, same cache).
        jobs = json.loads(first.get('/api/exports/%s/jobs?limit=1' % entry['id'], first_session)[2])
        url = jobs['items'][0]['images'][0]['thumb_url']
        self.assertEqual(first.get(url, first_session)[2], second.get(url, second_session)[2])

    def test_an_instance_opens_a_shared_copy_on_request(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        first, first_session = self.start()
        first.post('/api/catalog/%s/open' % entry['id'], first_session)
        self.wait(first, first_session, entry['id'])
        second, second_session = self.start()
        self.assertEqual(self.item(second, second_session, entry['id'])['state'], 'cached')
        # A request routed to the other instance verifies and opens the shared copy (no download).
        status, _, body = second.get('/api/exports/%s/overview' % entry['id'], second_session)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.blob.count(entry['blob']), 1)
        self.assertEqual(json.loads(body)['summary']['id'], entry['id'])

    def test_a_corrupt_shared_copy_is_downloaded_again(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        folder = os.path.join(self.base, 'cache', 'zips')
        os.makedirs(folder)
        with open(os.path.join(folder, '%s.zip' % entry['sha256']), 'wb') as handle:
            handle.write(b'\0' * entry['size'])
        hosted, session = self.start()
        hosted.post('/api/catalog/%s/open' % entry['id'], session)
        self.assertEqual(self.wait(hosted, session, entry['id'])[0]['state'], 'ready')
        self.assertEqual(self.blob.count(entry['blob']), 1)
        self.assertIn('did not match its SHA-256', hosted.log.getvalue())

    def test_several_roots_and_unpublishing(self):
        members = {}
        for root in ('first_root', 'second_root'):
            for name, data in fx.standard_export().members().items():
                members['%s/%s' % (root, name)] = data
        multi = self.blob.put_export(fx.zip_bytes(members), name='two.zip', label='Two roots')
        single = self.publish(name='one.zip')
        self.blob.write_catalog([multi, single])
        hosted, session = self.start()
        for entry in (multi, single):
            hosted.post('/api/catalog/%s/open' % entry['id'], session)
            self.wait(hosted, session, entry['id'])
        item = self.item(hosted, session, multi['id'])
        self.assertEqual([e['id'] for e in item['exports']], ['%s-1' % multi['id'], '%s-2' % multi['id']])
        self.assertEqual([e['label'] for e in item['exports']], ['Two roots › first_root', 'Two roots › second_root'])
        self.assertEqual(hosted.get('/api/exports/%s-2/overview' % multi['id'], session)[0], 200)
        self.assertEqual(hosted.get('/api/exports/%s-3/overview' % multi['id'], session)[0], 404)
        self.assertEqual(hosted.get('/api/exports/%s/overview' % multi['id'], session)[0], 404)
        # Unpublished: no longer served, even though it is open here.
        self.blob.write_catalog([single])
        self.assertEqual(hosted.get('/api/exports/%s-1/overview' % multi['id'], session)[0], 404)
        listing = json.loads(hosted.get('/api/exports', session)[2])
        self.assertEqual([i['id'] for i in listing['catalog']['items']], [single['id']])
        self.assertEqual([e['id'] for e in listing['exports']], [single['id']])

    def test_unpublished_zips_and_thumbnails_are_deleted(self):
        keep, drop = self.publish(name='keep.zip'), self.publish(fx.gallery_export(12), name='drop.zip')
        self.blob.write_catalog([keep, drop])
        first, session = self.start()
        for entry in (keep, drop):
            first.post('/api/catalog/%s/open' % entry['id'], session)
            self.wait(first, session, entry['id'])
            jobs = json.loads(first.get('/api/exports/%s/jobs?limit=1' % entry['id'], session)[2])
            first.get(jobs['items'][0]['images'][0]['thumb_url'], session)
        thumbs = os.path.join(self.base, 'cache', 'thumbs')
        if PIL_IMAGE is not None:  # without Pillow there are no thumbnails to delete (CI runs both ways)
            self.assertIn(drop['id'], os.listdir(thumbs))
        self.blob.write_catalog([keep])
        second, second_session = self.start()  # another instance, where nothing is open
        # The maintenance pass (every minute, also while nobody uses the viewer) on each instance:
        first.app.maintain()
        second.app.maintain()
        self.assertEqual(first.get('/api/exports/%s/overview' % drop['id'], session)[0], 404)
        self.assertIsNone(first.app.registry.get(drop['id']))  # closed where it was open
        self.assertEqual(len(self.zips()), 2)  # used in the last 10 minutes: kept for now
        if PIL_IMAGE is not None:
            self.assertNotIn(drop['id'], os.listdir(thumbs))
            self.assertIn(keep['id'], os.listdir(thumbs))
        for entry in (keep, drop):
            age(os.path.join(self.base, 'cache', 'zips', '%s.zip' % entry['sha256']), catalog_module.PRUNE_MIN_AGE_S + 10)
        first.app.maintain()  # without any catalog change (the storage answers 304)
        self.assertEqual(self.zips(), ['%s.zip' % keep['sha256']])
        self.assertIn('no longer published', first.log.getvalue())
        self.assertEqual(first.get('/api/exports/%s/overview' % keep['id'], session)[0], 200)

    def test_nothing_is_deleted_before_a_catalog_was_read(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        first, session = self.start()
        self.open(first, session, entry['id'])
        self.wait(first, session, entry['id'])
        age(os.path.join(self.base, 'cache', 'zips', '%s.zip' % entry['sha256']), catalog_module.PRUNE_MIN_AGE_S + 10)
        self.blob.faults['catalog.json'] = '500'
        second = Hosted(self.base, self.blob, self.google, env={'MJV_CACHE_DIR': os.path.join(self.base, 'cache')})
        self.instances.append(second)
        second.app.maintain()
        self.assertFalse(second.app.catalog.loaded)
        self.assertEqual(self.zips(), ['%s.zip' % entry['sha256']])

    def test_opening_past_the_cache_limit_evicts_and_closes_the_least_recently_used(self):
        first, second = self.publish(name='first.zip'), self.publish(fx.gallery_export(200), name='second.zip')
        self.blob.write_catalog([first, second])
        limit = 0.001  # the smallest MJV_CACHE_MAX_GB: room for either ZIP, not for both
        self.assertTrue(max(first['size'], second['size']) <= limit * 1024 ** 3 < first['size'] + second['size'])
        hosted, session = self.start(MJV_CACHE_MAX_GB=str(limit))
        self.open(hosted, session, first['id'])
        self.assertEqual(self.wait(hosted, session, first['id'])[0]['state'], 'ready')
        # Used in the last 10 minutes: never evicted, so the second one cannot fit yet.
        self.open(hosted, session, second['id'])
        item, _ = self.wait(hosted, session, second['id'])
        self.assertEqual((item['state'], item['error']['code']), ('failed', 'cache_full'))
        self.assertEqual(self.zips(), ['%s.zip' % first['sha256']])
        age(os.path.join(self.base, 'cache', 'zips', '%s.zip' % first['sha256']), catalog_module.PRUNE_MIN_AGE_S + 10)
        self.open(hosted, session, second['id'], retry=True)
        self.assertEqual(self.wait(hosted, session, second['id'])[0]['state'], 'ready')
        self.assertEqual(self.zips(), ['%s.zip' % second['sha256']])
        self.assertIn('evicted %s' % first['sha256'][:16], hosted.log.getvalue())
        # The evicted export is closed here; asking for it again says it is not ready (the UI opens it again).
        self.assertEqual(self.item(hosted, session, first['id'])['state'], 'absent')
        self.assertEqual(hosted.get('/api/exports/%s/overview' % first['id'], session)[0], 409)

    def test_one_unreadable_root_does_not_hide_the_others(self):
        members = {}
        for root in ('a_good', 'b_bad'):
            for name, data in fx.standard_export().members().items():
                members['%s/%s' % (root, name)] = data
        entry = self.blob.put_export(fx.zip_bytes(members), name='two.zip')
        self.blob.write_catalog([entry])
        hosted, session = self.start()
        build = server_module.ExportIndex

        def flaky(view, *args, **kwargs):
            if view.root == 'b_bad':
                raise ValueError('a value nothing anticipated')
            return build(view, *args, **kwargs)

        with mock.patch.object(server_module, 'ExportIndex', side_effect=flaky), \
                contextlib.redirect_stderr(io.StringIO()):
            self.open(hosted, session, entry['id'])
            item, _ = self.wait(hosted, session, entry['id'])
        self.assertEqual(item['state'], 'ready', item)
        self.assertEqual([e['id'] for e in item['exports']], ['%s-1' % entry['id']])
        self.assertEqual([(f['root'], f['code']) for f in item['failed_roots']], [('b_bad', 'load_failed')])
        self.assertIn('ValueError', item['failed_roots'][0]['message'])
        self.assertEqual(hosted.get('/api/exports/%s-1/overview' % entry['id'], session)[0], 200)
        self.assertEqual(hosted.get('/api/exports/%s-2/overview' % entry['id'], session)[0], 404)
        listing = json.loads(hosted.get('/api/exports', session)[2])
        self.assertEqual([e['id'] for e in listing['exports']], ['%s-1' % entry['id']])

    def test_an_unexpected_error_is_logged_with_its_traceback(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        hosted, session = self.start()
        with mock.patch.object(hosted.app.cache, 'download', side_effect=KeyError('surprise')):
            self.open(hosted, session, entry['id'])
            item, _ = self.wait(hosted, session, entry['id'])
        self.assertEqual(item['error'], {'code': 'internal', 'message': 'could not prepare this export (KeyError)'})
        log = hosted.log.getvalue()
        self.assertIn('Traceback (most recent call last)', log)
        self.assertIn("KeyError: 'surprise'", log)
        self.assert_no_secret(log)

    def test_the_query_string_never_leaks(self):
        entry = self.publish()
        self.blob.write_catalog([entry])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            hosted, session = self.start()
            responses = []
            for fault in ('403', '500', 'truncate', None):
                self.blob.faults.pop(entry['blob'], None)
                if fault:
                    self.blob.faults[entry['blob']] = fault
                responses.append(self.open(hosted, session, entry['id'], retry=True))
                self.wait(hosted, session, entry['id'])
                for path in ('/api/exports', '/api/catalog/%s' % entry['id'], '/api/exports/%s/overview' % entry['id'],
                             '/api/health', '/healthz', '/'):
                    responses.append(hosted.get(path, session))
            self.blob.faults['catalog.json'] = '403'
            responses.append(hosted.get('/api/exports', session))
            responses.append(hosted.get('/healthz'))
        texts = [hosted.log.getvalue(), stderr.getvalue()]
        for _, headers, body in responses:
            texts.append(str(headers))
            texts.append(body.decode('utf-8', 'replace'))
        prep = hosted.app.published._prep(entry['id'])
        texts.append(json.dumps(prep.error))
        texts.append(repr(hosted.app.store) + repr(hosted.config))
        self.assert_no_secret(*texts)
        self.assertIn('HTTP 403', hosted.log.getvalue())


class DeleteFlowTests(FlowTestCase):
    """Deleting through the HTTP API: the conditional rewrite of catalog.json, the ZIP in storage, and every
    instance's copies. The signed-in account is alice@example.com, who owns the exports she deletes."""

    def start(self, cache='cache', **env):
        env.setdefault('MJV_CATALOG_WRITE_URL', self.blob.write_url)
        return super().start(cache, **env)

    def publish(self, export=None, **kwargs):
        kwargs.setdefault('owner', 'alice@example.com')
        return super().publish(export, **kwargs)

    def delete(self, hosted, session, entry):
        status, _, body = hosted.post('/api/catalog/%s/delete' % entry['id'], session,
                                      body=json.dumps({'confirm': entry['id']}).encode())
        return status, json.loads(body)

    def stored(self):
        return self.blob.read_json('catalog.json')

    def thumbs(self):
        folder = os.path.join(self.base, 'cache', 'thumbs')
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def view(self, hosted, session, entry):
        """Open an export and request one thumbnail, so that it has a cached ZIP and a thumbnail folder."""
        self.open(hosted, session, entry['id'])
        self.assertEqual(self.wait(hosted, session, entry['id'])[0]['state'], 'ready')
        jobs = json.loads(hosted.get('/api/exports/%s/jobs?limit=1' % entry['id'], session)[2])
        self.assertEqual(hosted.get(jobs['items'][0]['images'][0]['thumb_url'], session)[0], 200)

    def test_a_concurrent_writer_is_never_overwritten_and_unknown_fields_survive(self):
        mine = dict(self.publish(name='mine.zip'), x_note={'kept': True})
        other = dict(self.publish(fx.gallery_export(6), name='other.zip', owner='bob@example.com'), x_note='kept too')
        earlier = {'id': 'f' * 16, 'sha256': 'f' * 64, 'size': 99, 'owner': None, 'deleted_at': '2026-01-01T00:00:00Z',
                   'deleted_by': 'carol@example.com', 'x_reason': 'invented'}
        self.blob.write_catalog([mine, other], x_publisher={'tool': 'invented', 'n': 2}, deleted=[earlier])
        hosted, session = self.start()
        late = self.publish(fx.gallery_export(7), name='late.zip')
        written = []

        def another_writer(name, method):  # publishes an export between the viewer's read and its write, once
            if name == 'catalog.json' and not written:
                written.append(method)
                data = self.stored()
                data['exports'].append(late)
                data['x_added_meanwhile'] = [1, 2]
                self.blob.put('catalog.json', json.dumps(data).encode())

        self.blob.before_write = another_writer
        started = time.time()
        self.assertEqual(self.delete(hosted, session, mine), (200, {'deleted': mine['id']}))
        puts = [r for r in self.blob.requests if r['method'] == 'PUT']
        self.assertEqual([r['status'] for r in puts], [412, 201])
        self.assertTrue(all(r['if_match'] and r['if_none_match'] is None and r['sas'] == 'write' for r in puts))
        self.assertNotEqual(puts[0]['if_match'], puts[1]['if_match'])  # read again before writing again
        data = self.stored()
        self.assertEqual([e['id'] for e in data['exports']], [other['id'], late['id']])
        self.assertEqual(data['exports'][0]['x_note'], 'kept too')
        self.assertEqual((data['x_publisher'], data['x_added_meanwhile']), ({'tool': 'invented', 'n': 2}, [1, 2]))
        self.assertEqual(data['deleted'][0], earlier)
        stone = data['deleted'][1]
        self.assertEqual(set(stone), {'id', 'sha256', 'size', 'owner', 'deleted_at', 'deleted_by'})
        self.assertEqual((stone['id'], stone['sha256'], stone['size'], stone['owner'], stone['deleted_by']),
                         (mine['id'], mine['sha256'], mine['size'], 'alice@example.com', 'alice@example.com'))
        self.assertTrue(started - 1 <= parse_date(stone['deleted_at']).timestamp() <= time.time())
        self.assertEqual(data['updated_at'], stone['deleted_at'])
        self.assertIsNone(self.blob.file(mine['blob']))
        self.assertIsNotNone(self.blob.file(other['blob']))
        self.assertEqual(self.blob.count(mine['blob'], status=202, method='DELETE'), 1)

    def test_a_catalog_that_keeps_changing_is_left_alone(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        self.blob.before_write = lambda name, method: self.blob.write_catalog([mine], x_touched=time.time())
        status, answer = self.delete(hosted, session, mine)
        self.assertEqual((status, answer['error']['code']), (409, 'catalog_busy'))
        self.assertIn('nothing was deleted', answer['error']['message'])
        self.assertEqual(self.blob.count('catalog.json', status=412, method='PUT'), catalog_module.WRITE_ATTEMPTS)
        self.assertEqual(([e['id'] for e in self.stored()['exports']], 'deleted' in self.stored()), ([mine['id']], False))
        self.assertIsNotNone(self.blob.file(mine['blob']))
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 0)

    def test_a_zip_already_gone_from_storage_is_fine(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        self.blob.remove(mine['blob'])
        self.assertEqual(self.delete(hosted, session, mine)[0], 200)
        self.assertEqual(self.stored()['exports'], [])
        self.assertEqual(self.blob.count(mine['blob'], status=404, method='DELETE'), 1)
        hosted.app.maintain()  # nothing left to try again
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 1)
        self.assertNotIn('still in storage', hosted.log.getvalue())

    def test_storage_errors_delete_nothing_and_quote_no_secret(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        before = self.stored()
        for faults, fault, text in ((self.blob.faults, '500', 'HTTP 500'), (self.blob.faults, '403', 'HTTP 403'),
                                    (self.blob.write_faults, '403', 'write access may have expired'),
                                    (self.blob.write_faults, '404', 'HTTP 404'),
                                    (self.blob.write_faults, '302', 'redirect')):
            with self.subTest(fault=fault, write=faults is self.blob.write_faults):
                faults['catalog.json'] = fault
                try:
                    status, answer = self.delete(hosted, session, mine)
                finally:
                    faults.clear()
                self.assertEqual((status, answer['error']['code']), (502, 'storage_error'))
                self.assertIn(text, answer['error']['message'])
                self.assertIn('nothing was deleted', answer['error']['message'])
                self.assertIn('could not delete %s for alice@example.com (storage_error): %s'
                              % (mine['id'], answer['error']['message']), hosted.log.getvalue())
                self.assert_no_secret(answer)
                self.assertEqual(self.stored(), before)
                self.assertIsNotNone(self.blob.file(mine['blob']))
        huge = json.dumps({'version': 1, 'exports': [mine], 'x_publisher': {'scale': 'HUGE'}}).encode()
        for raw, text in ((b'{not json', 'not valid JSON'),
                          (json.dumps({'version': 1, 'exports': [mine], 'deleted': {'not': 'a list'}}).encode(),
                           'deleted field that is not a list'),
                          (huge.replace(b'"HUGE"', b'1e400'), 'number too large to be written back'),
                          (json.dumps({'version': 1, 'exports': [mine], 'x_deep': nested(80)}).encode(),
                           'nested more than 64 levels deep')):
            with self.subTest(text=text):
                self.blob.put('catalog.json', raw)
                status, answer = self.delete(hosted, session, mine)
                self.assertEqual((status, answer['error']['code']), (502, 'storage_error'))
                self.assertIn(text, answer['error']['message'])
                self.assertIn('nothing was deleted', answer['error']['message'])
                with open(self.blob.file('catalog.json'), 'rb') as handle:
                    self.assertEqual(handle.read(), raw)
        self.assertEqual(self.blob.count(REDIRECT_TARGET), 0)
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 0)
        self.assert_no_secret(hosted.log.getvalue())

    def test_a_write_without_a_clear_answer_is_checked_before_answering(self):
        """Storage may store catalog.json and give no answer (a timeout, a reset) or a 5xx: the server reads it
        again, and answers by whether this deletion's record is there."""
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        before = self.stored()
        lose_the_read = lambda name, method: self.blob.faults.update({'catalog.json': '500'})
        for before_write, says in ((None, 'catalog.json read again does not show the deletion'),
                                   (lose_the_read, 'reading catalog.json again failed: '
                                                   'catalog.json: storage answered HTTP 500')):
            with self.subTest(says=says):
                self.blob.write_faults['catalog.json'], self.blob.before_write = '500', before_write
                try:
                    status, answer = self.delete(hosted, session, mine)
                finally:
                    self.blob.write_faults.clear()
                    self.blob.faults.clear()
                    self.blob.before_write = None
                self.assertEqual((status, answer['error']['code']), (502, 'delete_unconfirmed'))
                self.assertEqual(answer['error']['message'], 'catalog.json: storage answered HTTP 500; could not confirm '
                                 'whether the export was deleted (%s): reload the page to check' % says)
                self.assertEqual(self.stored(), before)
                self.assertIsNotNone(self.blob.file(mine['blob']))
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 0)
        # Stored, but the answer never arrived: deleted all the same, and answered so.
        self.blob.write_faults['catalog.json'] = 'drop'
        try:
            self.assertEqual(self.delete(hosted, session, mine), (200, {'deleted': mine['id']}))
        finally:
            self.blob.write_faults.clear()
        self.assertEqual([(s['id'], s['deleted_by']) for s in self.stored()['deleted']], [(mine['id'], 'alice@example.com')])
        self.assertEqual(self.stored()['exports'], [])
        self.assertIsNone(self.blob.file(mine['blob']))
        listing = json.loads(hosted.get('/api/exports', session)[2])
        self.assertEqual(listing['catalog']['items'], [])
        log = hosted.log.getvalue()
        for line in ('catalog: no clear answer to the write of catalog.json (catalog.json: storage unreachable',
                     'catalog: the write of catalog.json was stored', 'deleted %s by alice@example.com' % mine['id']):
            self.assertEqual(log.count(line), 1, line)
        self.assert_no_secret(log)

    def test_a_catalog_that_would_not_read_back_is_never_written(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        for patch, says in ((mock.patch.object(catalog_module, 'dump_catalog', side_effect=ValueError('Out of range')),
                             '(ValueError)'),
                            (mock.patch.object(catalog_module, 'dump_catalog',
                                               return_value=b'{"version": 1, "exports": [], "x": NaN}\n'),
                             '(catalog.json is not valid JSON)')):
            with self.subTest(says=says), patch:
                status, answer = self.delete(hosted, session, mine)
                self.assertEqual((status, answer['error']['code']), (502, 'storage_error'))
                self.assertEqual(answer['error']['message'],
                                 'catalog.json could not be written back %s; nothing was deleted' % says)
        self.assertEqual(self.blob.count('catalog.json', method='PUT'), 0)
        self.assertEqual([e['id'] for e in self.stored()['exports']], [mine['id']])
        self.assertIsNotNone(self.blob.file(mine['blob']))

    def test_a_step_after_the_write_that_fails_is_logged_and_the_deletion_stands(self):
        """Once catalog.json no longer lists the export it is deleted: serving the new catalog here, deleting the
        ZIP from storage and closing the export here each only log a failure, and the next steps still run."""
        for n, (where, step) in enumerate((('catalog', 'adopt'), ('published', '_delete_blob'),
                                           ('published', '_purge'))):
            with self.subTest(step=step):
                mine = self.publish(fx.gallery_export(4 + n), name='%d.zip' % n)
                self.blob.write_catalog([mine])
                cache = 'cache-%d' % n
                hosted, session = self.start(cache=cache, MJV_CATALOG_TTL_S='600')
                self.view(hosted, session, mine)
                with mock.patch.object(getattr(hosted.app, where), step, side_effect=RuntimeError('invented')):
                    self.assertEqual(self.delete(hosted, session, mine), (200, {'deleted': mine['id']}))
                log = hosted.log.getvalue()
                self.assertIn('deleted %s by alice@example.com' % mine['id'], log)
                self.assertEqual(log.count('catalog: after deleting %s, ' % mine['id']), 1)
                self.assertIn('(RuntimeError)', log)
                self.assertEqual(self.stored()['exports'], [])
                listing = json.loads(hosted.get('/api/exports', session)[2])  # the copy that failed is read again
                self.assertEqual(listing['catalog']['items'], [])
                hosted.app.maintain()  # what failed, the next maintenance pass does
                self.assertIsNone(self.blob.file(mine['blob']))
                self.assertIsNone(hosted.app.registry.get(mine['id']))
                self.assertEqual(self.zips(cache), [])
                self.assert_no_secret(log)

    def test_the_deleting_instance_closes_the_export_and_deletes_its_files_at_once(self):
        mine, keep = self.publish(fx.gallery_export(12), name='mine.zip'), self.publish(name='keep.zip')
        self.blob.write_catalog([mine, keep])
        hosted, session = self.start(MJV_CATALOG_TTL_S='600')
        for entry in (mine, keep):
            self.view(hosted, session, entry)
        if PIL_IMAGE is not None:  # without Pillow there are no thumbnails (CI runs both ways)
            self.assertEqual(self.thumbs(), sorted([mine['id'], keep['id']]))
        reads = self.blob.count('catalog.json', method='GET')
        logged = len(hosted.log.getvalue())
        self.assertEqual(self.delete(hosted, session, mine)[0], 200)
        # One line records the deletion and who made it; nothing else is said about it.
        self.assertEqual(hosted.log.getvalue()[logged:].splitlines(),
                         ['deleted %s by alice@example.com' % mine['id'], 'POST /api/catalog/%s/delete 200' % mine['id']])
        self.assertIsNone(hosted.app.registry.get(mine['id']))
        self.assertIsNone(hosted.app.published._prep(mine['id']))
        self.assertEqual(self.zips(), ['%s.zip' % keep['sha256']])  # used a moment ago, deleted all the same
        if PIL_IMAGE is not None:
            self.assertEqual(self.thumbs(), [keep['id']])
        # The new catalog is in use at once, without waiting for the next refresh (MJV_CATALOG_TTL_S).
        listing = json.loads(hosted.get('/api/exports', session)[2])
        self.assertEqual([i['id'] for i in listing['catalog']['items']], [keep['id']])
        self.assertEqual([e['id'] for e in listing['exports']], [keep['id']])
        self.assertEqual(hosted.get('/api/exports/%s/overview' % mine['id'], session)[0], 404)
        self.assertEqual(hosted.get('/api/catalog/%s' % mine['id'], session)[0], 404)
        self.assertEqual(self.blob.count('catalog.json', method='GET'), reads + 1)  # only the fresh read before writing
        self.assertEqual(self.delete(hosted, session, mine)[1]['error']['code'], 'unknown_catalog_entry')

    def test_another_instance_catches_up_at_its_next_catalog_refresh(self):
        mine = self.publish(fx.gallery_export(12), name='mine.zip')
        self.blob.write_catalog([mine])
        first, first_session = self.start()
        second, second_session = self.start()  # the same cache folder
        for hosted, session in ((first, first_session), (second, second_session)):
            self.view(hosted, session, mine)
        self.assertEqual(self.delete(first, first_session, mine)[0], 200)
        self.assertEqual(self.zips(), [])  # the shared copy is gone at once
        self.assertIsNotNone(second.app.registry.get(mine['id']))  # still open there until it reads the catalog again
        # Files the other instance writes meanwhile (a thumbnail, a download it finishes) go as well.
        zips, thumbs = os.path.join(self.base, 'cache', 'zips'), os.path.join(self.base, 'cache', 'thumbs')

        def leave_files():
            os.makedirs(os.path.join(thumbs, mine['id'], 'ab'), exist_ok=True)
            with open(os.path.join(thumbs, mine['id'], 'ab', 'thumb'), 'wb') as handle:
                handle.write(b'x')
            with open(os.path.join(zips, '%s.zip' % mine['sha256']), 'wb') as handle:
                handle.write(b'left behind')

        leave_files()
        listing = json.loads(second.get('/api/exports', second_session)[2])  # any request reads the catalog (TTL 0)
        self.assertEqual(listing['catalog']['items'], [])
        self.assertIsNone(second.app.registry.get(mine['id']))
        self.assertEqual((self.zips(), self.thumbs()), ([], []))
        self.assertIn('removed %s from this instance: it was deleted' % mine['id'], second.log.getvalue())
        self.assertNotIn('by alice@example.com', second.log.getvalue())  # only the deleting instance says who
        leave_files()
        second.app.maintain()  # and every minute, also while nobody uses the viewer
        self.assertEqual((self.zips(), self.thumbs()), ([], []))

    def test_an_instance_that_cannot_read_the_catalog_stops_serving_exports(self):
        """Another instance follows a deletion when it next reads catalog.json successfully. While its reads fail it
        keeps its last good copy, which still lists the export, for max(5 minutes, 5 x MJV_CATALOG_TTL_S) at most;
        then it closes the exports open there and answers 503 for the list and every export route, until a read
        succeeds."""
        mine = self.publish(fx.gallery_export(12), name='mine.zip')
        self.blob.write_catalog([mine])
        first, first_session = self.start(MJV_CATALOG_TTL_S='60')
        second, session = self.start(MJV_CATALOG_TTL_S='60')  # the same cache folder
        clock = [time.monotonic()]
        second.app.catalog._clock = lambda: clock[0]
        for hosted, user in ((first, first_session), (second, session)):
            self.view(hosted, user, mine)
        jobs = json.loads(second.get('/api/exports/%s/jobs?limit=1' % mine['id'], session)[2])['items']
        overview = '/api/exports/%s/overview' % mine['id']
        routes = ('/api/exports', overview, jobs[0]['images'][0]['url'], jobs[0]['images'][0]['thumb_url'],
                  '/api/catalog/%s' % mine['id'])
        self.assertEqual(self.delete(first, first_session, mine)[0], 200)
        self.blob.faults['catalog.json'] = '500'  # from now on the second instance cannot read catalog.json
        for _ in range(4):
            clock[0] += 60
            second.app.maintain()
            self.assertEqual(second.get(overview, session)[0], 200)
        clock[0] += 60
        second.app.maintain()  # 5 minutes: closed, also while nobody uses it
        self.assertIsNone(second.app.registry.get(mine['id']))
        unavailable = {'code': 'catalog_unavailable',
                       'message': "The viewer can't reach its list of exports right now. Try again in a few minutes."}
        answers = [second.get(path, session) for path in routes] + [self.open(second, session, mine['id'])]
        self.assertEqual([(status, json.loads(body)['error']) for status, _, body in answers],
                         [(503, unavailable)] * (len(routes) + 1))
        self.assertEqual([second.get(path, session)[0] for path in ('/api/health', '/')], [200, 200])
        self.assertEqual(second.log.getvalue().count('catalog: no successful read for 5 minutes; no export is served '
                                                     'until one succeeds; closed %s' % mine['id']), 1)
        # Once a read succeeds it serves again, and follows the deletion it missed.
        self.blob.faults.clear()
        clock[0] += 60
        self.assertEqual(json.loads(second.get('/api/exports', session)[2])['catalog']['items'], [])
        self.assertEqual(second.get(overview, session)[0], 404)
        self.assertEqual(second.log.getvalue().count('catalog: read successfully again'), 1)

    def test_a_download_under_way_stops_and_leaves_nothing(self):
        mine = self.publish(fx.gallery_export(60))
        self.blob.write_catalog([mine])
        self.slow(mine, chunk=1024, delay=0.05)
        hosted, session = self.start()
        self.open(hosted, session, mine['id'])
        self.wait(hosted, session, mine['id'], states=('downloading',))
        self.assertEqual(self.delete(hosted, session, mine)[0], 200)
        self.assertEqual(self.zips(), [])  # no ZIP, no temporary file, no lock
        self.assertIsNone(hosted.app.published._prep(mine['id']))
        self.assertNotIn('prepare %s failed' % mine['id'], hosted.log.getvalue())
        self.blob.slow_delay = 0

    def test_a_zip_storage_kept_is_deleted_again_by_maintenance(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        clock = [1000.0]
        hosted.app.published._clock = lambda: clock[0]
        self.blob.write_faults[mine['blob']] = '500'
        self.assertEqual(self.delete(hosted, session, mine)[0], 200)  # gone from the viewer; the ZIP follows
        self.assertEqual(self.stored()['exports'], [])
        self.assertIsNotNone(self.blob.file(mine['blob']))
        self.assertEqual(hosted.app.published.zip_deletes_pending(), 1)
        hosted.app.maintain()  # less than a minute later: not yet
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 1)
        clock[0] += catalog_module.BLOB_RETRY_S
        hosted.app.maintain()  # failing again: tried again, the same failure is logged once
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 2)
        self.assertEqual(hosted.log.getvalue().count('the ZIP of %s is still in storage' % mine['id']), 1)
        self.blob.write_faults.clear()
        clock[0] += catalog_module.BLOB_RETRY_S
        hosted.app.maintain()
        self.assertIsNone(self.blob.file(mine['blob']))
        retry = [r for r in self.blob.requests if r['method'] == 'DELETE'][-1]
        self.assertEqual((retry['status'], retry['delete_snapshots']), (202, 'include'))
        self.assertTrue(retry['if_unmodified_since'])  # never a ZIP uploaded again after the deletion
        self.assertIn('deleted the ZIP of %s from storage' % mine['id'], hosted.log.getvalue())
        self.assertEqual(hosted.app.published.zip_deletes_pending(), 0)
        clock[0] += 3600
        hosted.app.maintain()  # once gone, never again
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 3)
        self.assert_no_secret(hosted.log.getvalue())

    def test_a_zip_storage_refuses_to_delete_is_tried_again_until_it_goes(self):
        """A 412 is a ZIP uploaded again only for the DELETE that asks If-Unmodified-Since, and only when storage
        does not name another reason: here a condition nobody sent, then a lease. /healthz counts the ZIP as
        waiting until it is gone."""
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        clock = [1000.0]
        hosted.app.published._clock = lambda: clock[0]
        pending = lambda: json.loads(hosted.get('/healthz')[2])['catalog']['zip_deletes_pending']
        self.assertEqual(pending(), 0)
        self.blob.write_faults[mine['blob']] = '412'
        self.assertEqual(self.delete(hosted, session, mine)[0], 200)
        self.assertEqual(pending(), 1)
        self.blob.write_faults[mine['blob']] = 'lease'
        clock[0] += catalog_module.BLOB_RETRY_S
        hosted.app.maintain()
        deletes = [r for r in self.blob.requests if r['method'] == 'DELETE']
        self.assertEqual([(r['status'], bool(r['if_unmodified_since'])) for r in deletes], [(412, False), (412, True)])
        self.assertEqual(pending(), 1)
        self.blob.write_faults.clear()  # the lease is gone
        clock[0] += catalog_module.BLOB_RETRY_S
        hosted.app.maintain()
        self.assertIsNone(self.blob.file(mine['blob']))
        self.assertEqual(pending(), 0)
        log = hosted.log.getvalue()
        for reason in ('ConditionNotMet', 'LeaseIdMissing'):
            self.assertEqual(log.count('the ZIP of %s is still in storage (the published ZIP: storage answered HTTP 412 '
                                       '(%s)); trying again every minute' % (mine['id'], reason)), 1)
        self.assertNotIn('kept the ZIP', log)
        self.assertIn('deleted the ZIP of %s from storage' % mine['id'], log)

    def test_an_export_published_again_is_left_alone(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        hosted, session = self.start()
        clock = [1000.0]
        hosted.app.published._clock = lambda: clock[0]
        self.blob.write_faults[mine['blob']] = '500'
        self.assertEqual(self.delete(hosted, session, mine)[0], 200)
        self.blob.write_faults.clear()
        stones = self.stored()['deleted']
        # Uploaded again after the deletion, not listed yet: the retry keeps it.
        path = self.blob.file(mine['blob'])
        later = time.time() + 30
        os.utime(path, (later, later))
        clock[0] += catalog_module.BLOB_RETRY_S
        hosted.app.maintain()
        self.assertEqual(self.blob.count(mine['blob'], status=412, method='DELETE'), 1)
        self.assertIsNotNone(self.blob.file(mine['blob']))
        self.assertIn('kept the ZIP of %s' % mine['id'], hosted.log.getvalue())
        self.assertEqual(hosted.app.published.zip_deletes_pending(), 0)  # kept: nothing left to delete
        # Listed again, tombstone and all: it opens, and nothing deletes it or its copy.
        self.blob.write_catalog([mine], deleted=stones)
        self.open(hosted, session, mine['id'])
        self.assertEqual(self.wait(hosted, session, mine['id'])[0]['state'], 'ready')
        clock[0] += catalog_module.BLOB_RETRY_S
        other, _ = self.start()  # an instance that never tried to delete it
        for instance in (hosted, other):
            instance.app.maintain()
        self.assertEqual(self.blob.count(mine['blob'], method='DELETE'), 2)
        self.assertEqual(self.zips(), ['%s.zip' % mine['sha256']])
        self.assertEqual(hosted.get('/api/exports/%s/overview' % mine['id'], session)[0], 200)

    def test_tombstones_from_elsewhere_are_followed_for_30_days(self):
        old, recent = self.publish(name='old.zip'), self.publish(fx.gallery_export(5), name='recent.zip')
        month_ago = time.time() - 31 * 86400
        for entry, uploaded in ((old, month_ago - 60), (recent, time.time() - 7200)):
            os.utime(self.blob.file(entry['blob']), (uploaded, uploaded))
        stamp = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))
        self.blob.write_catalog([], deleted=[
            {'id': old['id'], 'sha256': old['sha256'], 'size': old['size'], 'owner': None,
             'deleted_at': stamp(month_ago), 'deleted_by': 'carol@example.com'},
            {'id': recent['id'], 'sha256': recent['sha256'], 'size': recent['size'], 'owner': 'alice@example.com',
             'deleted_at': stamp(time.time() - 3600), 'deleted_by': 'alice@example.com'}])
        hosted, _ = self.start()
        hosted.app.maintain()
        self.assertEqual(self.blob.count(old['blob'], method='DELETE'), 0)
        self.assertIsNone(self.blob.file(recent['blob']))
        hosted_without_write, _ = self.start(cache='other-cache', MJV_CATALOG_WRITE_URL='')
        hosted_without_write.app.maintain()  # deleting is off there: it never tries, and has nothing waiting
        self.assertEqual([self.blob.count(e['blob'], method='DELETE') for e in (old, recent)], [0, 1])
        self.assertEqual([h.app.published.zip_deletes_pending() for h in (hosted, hosted_without_write)], [0, 0])

    def test_the_write_query_string_never_leaks(self):
        mine = self.publish()
        self.blob.write_catalog([mine])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            hosted, session = self.start()
            responses = []
            for faults, name, fault in ((self.blob.write_faults, 'catalog.json', '403'),
                                        (self.blob.write_faults, 'catalog.json', '500'),
                                        (self.blob.write_faults, 'catalog.json', '302'),
                                        (self.blob.write_faults, mine['blob'], '500'),
                                        (self.blob.write_faults, mine['blob'], '403')):
                faults[name] = fault
                if self.blob.file('catalog.json') and not self.stored()['exports']:
                    self.blob.write_catalog([mine])
                responses.append(hosted.post('/api/catalog/%s/delete' % mine['id'], session,
                                             body=json.dumps({'confirm': mine['id']}).encode()))
                faults.clear()
            hosted.app.maintain()
            for path in ('/api/exports', '/api/health', '/healthz', '/'):
                responses.append(hosted.get(path, session))
        texts = [hosted.log.getvalue(), stderr.getvalue(), repr(hosted.app.store), repr(hosted.config)]
        for _, headers, body in responses:
            texts.extend((str(headers), body.decode('utf-8', 'replace')))
        self.assert_no_secret(*texts)
        self.assertIn('HTTP 403', hosted.log.getvalue() + ''.join(texts))


class PublishedUnitTests(BlobTestCase):
    def test_unknown_ids_resolve_to_nothing(self):
        cat = Catalog(BlobStore(self.blob.url), ttl_s=0)
        published = Published(cat, ZipCache(os.path.join(self.base, 'c'), 10 ** 9), registry=None,
                              thumbs_dir=os.path.join(self.base, 'c', 'thumbs'))
        self.blob.write_catalog([])
        self.assertEqual(published.resolve('0123456789abcdef', 0), (None, None))
        self.assertIsNone(published.item('0123456789abcdef'))
        self.assertEqual(published.resolve('not-an-id', 0), (None, None))


if __name__ == '__main__':
    unittest.main()
