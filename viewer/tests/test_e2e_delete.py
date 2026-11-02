"""End-to-end test of deleting a published export in the hosted UI, in Chromium (opt-in).

Run from viewer/:   MJV_E2E=1 python3 -m unittest tests.test_e2e_delete -v

Like tests/test_e2e_browser.py it needs Playwright for Python and its Chromium
build, and is skipped without MJV_E2E=1, without Playwright, or when Chromium
cannot start. A hosted viewer with deleting turned on runs on 127.0.0.1 against
tests/fake_google.py (signed in as alice@example.com) and tests/fake_blob.py,
which holds three invented exports: one alice owns, one bob@example.com owns and
one without an owner. The browser aborts every request that is not for
127.0.0.1. A local-mode run checks that none of this shows there.
"""
import os
import tempfile
import threading
import time
import unittest
import urllib.parse

from mjviewer.server import ViewerApp, make_server
from tests import fixtures as fx
from tests.fake_blob import FakeBlob
from tests.fake_google import FakeGoogle
from tests.hosted_harness import Hosted

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None

ENABLED = os.environ.get('MJV_E2E') == '1'
WIDTH, HEIGHT = 1280, 800
LOCAL = ('127.0.0.1', 'localhost')


class _BrowserCase(unittest.TestCase):
    @classmethod
    def start_browser(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:  # Chromium not installed for Playwright
            cls.playwright.stop()
            raise unittest.SkipTest('Chromium for Playwright is unavailable: %s' % str(exc).splitlines()[0])

    @classmethod
    def stop_browser(cls):
        cls.browser.close()
        cls.playwright.stop()

    def new_page(self):
        self.context = self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT})
        self.aborted, self.console = [], []
        self.context.route('**/*', self._only_local)
        page = self.context.new_page()
        page.on('console', lambda m: self.console.append(m.text) if m.type == 'error' else None)
        page.on('pageerror', lambda e: self.console.append(str(e)))
        return page

    def _only_local(self, route):
        if (urllib.parse.urlsplit(route.request.url).hostname or '') in LOCAL:
            route.continue_()
        else:
            self.aborted.append(route.request.url)
            route.abort()

    def tearDown(self):
        self.context.close()
        # No script errors or CSP refusals; failed requests the UI handles (an ended session, an export not open yet,
        # a storage failure it reports) and the machine's network changing under Chromium are expected.
        self.assertEqual([m for m in self.console if not any(s in m for s in (
            'net::ERR_NETWORK_CHANGED', 'status of 401', 'status of 409', 'status of 502'))], [])
        self.assertEqual(self.aborted, [])


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class DeleteBrowserTests(_BrowserCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-delete-')
        cls.blob = FakeBlob(os.path.join(cls._tmp.name, 'blob'))
        cls.google = FakeGoogle(email='alice@example.com', hd='example.com')
        cls.hosted = Hosted(cls._tmp.name, cls.blob, cls.google, env={'MJV_CATALOG_WRITE_URL': cls.blob.write_url})
        cls.zips = {'mine': fx.export_zip_bytes(fx.gallery_export(12), root='mj_export_alice_20260430'),
                    'theirs': fx.export_zip_bytes(fx.standard_export()),
                    'unowned': fx.export_zip_bytes(fx.gallery_export(3))}
        try:
            cls.start_browser()
        except unittest.SkipTest:
            cls._stop()
            raise

    @classmethod
    def _stop(cls):
        cls.hosted.stop()
        cls.blob.stop()
        cls.google.stop()
        cls._tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.stop_browser()
        cls._stop()

    def setUp(self):
        self.google.reset()
        self.blob.write_faults.clear()
        self.blob.before_write = None
        self.mine = self.blob.put_export(self.zips['mine'], name='mj_export_alice_20260430.zip',
                                         label='Coast studies', owner='alice@example.com',
                                         summary={'jobs': 12, 'moodboards': 2, 'profiles': 2, 'images': 48,
                                                  'export_time': None, 'extension_version': '0.3.1', 'status': 'complete'})
        self.theirs = self.blob.put_export(self.zips['theirs'], name='mj_export_bob_20260430.zip', owner='bob@example.com')
        self.unowned = self.blob.put_export(self.zips['unowned'], name='mj_export_unowned_20260430.zip')
        self.blob.write_catalog([self.mine, self.theirs, self.unowned])
        self.page = self.new_page()
        self.page.goto(self.hosted.origin + '/')
        self.page.wait_for_selector('.xcard--catalog', timeout=30000)

    def card(self, entry):
        return self.page.locator('.xcard--catalog').filter(has_text=entry['name'])

    def dialog(self):
        return self.page.locator('dialog.confirm')

    def listed(self):
        return [e['id'] for e in self.blob.read_json('catalog.json')['exports']]

    def active(self, script):
        return self.page.evaluate('() => { const a = document.activeElement; return %s; }' % script)

    def test_the_owner_deletes_an_export_after_confirming(self):
        self.assertEqual(self.page.locator('.xcard--catalog').count(), 3)
        self.assertEqual(self.card(self.mine).locator('.xcard__owner').inner_text().split(), ['Owner', 'alice@example.com'])
        self.assertEqual(self.card(self.theirs).locator('.xcard__owner').inner_text().split(), ['Owner', 'bob@example.com'])
        self.assertEqual(self.card(self.unowned).locator('.xcard__owner').inner_text(), 'No owner recorded')
        for entry, count in ((self.mine, 1), (self.theirs, 0), (self.unowned, 0)):
            self.assertEqual(self.card(entry).locator('button', has_text='Delete…').count(), count)
        button = self.card(self.mine).locator('button', has_text='Delete…')
        # Esc and Cancel close the dialog and delete nothing; focus starts on Cancel and returns to the button.
        for close in ('Escape', 'Cancel'):
            with self.subTest(close=close):
                button.click()
                self.dialog().wait_for(timeout=15000)
                text = self.dialog().inner_text()
                for part in ('Delete this export?', 'Coast studies', 'mj_export_alice_20260430.zip',
                             '12 jobs · 48 images · 2 moodboards · 2 profiles', 'alice@example.com',
                             'disappears from this viewer for everyone at once', 'cannot undo this here',
                             'recoverable copy for 7 days'):
                    self.assertIn(part, text)
                self.assertEqual(self.active('a.textContent'), 'Cancel')
                if close == 'Escape':
                    self.page.keyboard.press('Escape')
                else:
                    self.dialog().locator('button', has_text='Cancel').click()
                self.dialog().wait_for(state='detached', timeout=15000)
                self.assertTrue(self.active("a.classList.contains('xcard__delete')"))
                self.assertEqual(self.listed(), [self.mine['id'], self.theirs['id'], self.unowned['id']])
        button.click()
        self.dialog().locator('button', has_text='Delete export').click()
        self.page.wait_for_selector('text=Deleted Coast studies.', timeout=15000)
        self.dialog().wait_for(state='detached', timeout=15000)
        self.page.wait_for_function("() => document.querySelectorAll('.xcard--catalog').length === 2", timeout=15000)
        self.assertEqual(self.card(self.mine).count(), 0)
        self.assertEqual(self.listed(), [self.theirs['id'], self.unowned['id']])
        self.assertIsNone(self.blob.file(self.mine['blob']))
        self.assertTrue(self.active("a.classList.contains('page-title')"))
        self.assertEqual(self.page.locator('#announcer').inner_text(), 'Deleted Coast studies')

    def test_a_failure_shows_the_servers_message_and_keeps_the_dialog(self):
        self.blob.write_faults['catalog.json'] = '500'
        self.card(self.mine).locator('button', has_text='Delete…').click()
        self.dialog().locator('button', has_text='Delete export').click()
        error = self.dialog().locator('.confirm__error')
        error.wait_for(timeout=15000)
        self.assertEqual(error.inner_text(),
                         'Could not delete: catalog.json: storage answered HTTP 500; nothing was deleted')
        self.assertTrue(self.dialog().locator('button', has_text='Delete export').is_enabled())
        self.assertEqual(self.active('a.textContent'), 'Cancel')
        self.assertEqual(self.listed(), [self.mine['id'], self.theirs['id'], self.unowned['id']])
        # Trying again once storage answers works from the same dialog.
        self.blob.write_faults.clear()
        self.dialog().locator('button', has_text='Delete export').click()
        self.page.wait_for_selector('text=Deleted Coast studies.', timeout=15000)
        self.assertEqual(self.listed(), [self.theirs['id'], self.unowned['id']])

    def test_a_deleted_export_open_in_this_tab_returns_to_the_start_page(self):
        # Open it (the card then links to it, the link covering the card), and come back.
        self.card(self.mine).locator('button', has_text='Open').click()
        self.page.wait_for_function('id => location.hash.includes(id)', arg='/x/%s/overview' % self.mine['id'],
                                    timeout=30000)
        self.page.wait_for_selector('.stats', timeout=30000)
        self.page.go_back()
        self.card(self.mine).locator('.xcard__link').wait_for(timeout=15000)
        # The deletion takes a moment; meanwhile the user goes forward to the export again.
        self.blob.before_write = lambda name, method: time.sleep(1.5) if name == 'catalog.json' else None
        self.card(self.mine).locator('button', has_text='Delete…').click()
        self.dialog().locator('button', has_text='Delete export').click()
        self.page.go_forward()
        self.page.wait_for_selector('text=Deleted Coast studies.', timeout=30000)
        self.assertIn(self.page.evaluate('location.hash'), ('#/', ''))
        self.assertEqual(self.page.locator('.page-title').inner_text(), 'Exports')
        self.assertEqual(self.dialog().count(), 0)
        self.page.wait_for_function("() => document.querySelectorAll('.xcard--catalog').length === 2", timeout=15000)
        self.assertEqual(self.listed(), [self.theirs['id'], self.unowned['id']])


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class LocalModeTests(_BrowserCase):
    """Local mode has no owners and no deleting."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-local-')
        cls.app = ViewerApp(cache_dir=os.path.join(cls._tmp.name, 'cache'))
        for n, export in enumerate((fx.standard_export(), fx.gallery_export(4))):
            cls.app.registry.open(fx.write_zip(export, os.path.join(cls._tmp.name, '%d.zip' % n)))
        cls.server = make_server(cls.app, '127.0.0.1', 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        try:
            cls.start_browser()
        except unittest.SkipTest:
            cls._stop()
            raise

    @classmethod
    def _stop(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()
        cls._tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.stop_browser()
        cls._stop()

    def test_the_start_page_shows_no_owner_and_no_delete(self):
        page = self.new_page()
        page.goto('http://127.0.0.1:%d/' % self.server.port)
        page.wait_for_selector('.xcard', timeout=15000)
        self.assertEqual(page.locator('.xcard').count(), 2)
        self.assertEqual(page.locator('.xcard__owner, .xcard__delete, dialog.confirm').count(), 0)
        self.assertEqual(page.locator('button', has_text='Delete').count(), 0)


if __name__ == '__main__':
    unittest.main()
