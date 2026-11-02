"""End-to-end test of deleting a published export in the hosted UI, in Chromium (opt-in).

Run from viewer/:   MJV_E2E=1 python3 -m unittest tests.test_e2e_delete -v

Like tests/test_e2e_browser.py it needs Playwright for Python and its Chromium
build, and is skipped without MJV_E2E=1, without Playwright, or when Chromium
cannot start. A hosted viewer with deleting turned on (and a stated 7-day
recoverable copy) runs on 127.0.0.1 against tests/fake_google.py (signed in as
alice@example.com) and tests/fake_blob.py, which holds three invented exports:
one alice owns, one bob@example.com owns and one without an owner. The browser aborts every request that is not for
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
# What the dialog says when storage refuses to write catalog.json.
REFUSED = 'The viewer could not update its list right now. Nothing was deleted. Try again in a few minutes.'


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
            'net::ERR_NETWORK_CHANGED', 'status of 401', 'status of 403', 'status of 409', 'status of 502'))], [])
        self.assertEqual(self.aborted, [])


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class DeleteBrowserTests(_BrowserCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-delete-')
        cls.blob = FakeBlob(os.path.join(cls._tmp.name, 'blob'))
        cls.google = FakeGoogle(email='alice@example.com', hd='example.com')
        cls.hosted = Hosted(cls._tmp.name, cls.blob, cls.google, env={'MJV_CATALOG_WRITE_URL': cls.blob.write_url,
                                                                      'MJV_SOFT_DELETE_DAYS': '7'})
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
        self.blob.faults.clear()
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

    def dialog_open(self):
        return self.page.evaluate("() => Boolean((document.querySelector('dialog.confirm') || {}).open)")

    def refuse_slowly(self, seconds=1.5):
        """The next write of catalog.json takes a while, then storage refuses it: nothing is deleted."""
        def slow_then_refuse(name, method):
            if name == 'catalog.json':
                time.sleep(seconds)
                self.blob.write_faults['catalog.json'] = '403'
        self.blob.before_write = slow_then_refuse

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
                             'It disappears from this viewer within about a minute. You cannot undo this here.',
                             "The viewer's storage keeps a recoverable copy for 7 days, then deletes it. "
                             'Copies shared in other ways are not affected.'):
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

    def test_failures_are_said_in_plain_words_and_keep_the_dialog(self):
        """Each failure gets a plain sentence; what storage answered goes to the server log only."""
        self.card(self.mine).locator('button', has_text='Delete…').click()
        button = self.dialog().locator('button', has_text='Delete export')
        error = self.dialog().locator('.confirm__error')
        tamper = lambda route: route.continue_(headers=dict(route.request.headers, **{'x-mjv-token': 'x' * 43}))
        bob_owns_it = [dict(self.mine, owner='bob@example.com'), self.theirs, self.unowned]
        for case, says in (('storage refuses', REFUSED),
                           ('no clear answer', 'The viewer could not confirm whether the export was deleted. '
                                               'Reload the page to check.'),
                           ('signed in elsewhere', 'Your sign-in changed. Reload the page and try again.'),
                           ('another owner', 'Could not delete: only the owner of this export or an administrator '
                                             'can delete it')):
            with self.subTest(case=case):
                if case == 'storage refuses':
                    self.blob.write_faults['catalog.json'] = '403'
                elif case == 'no clear answer':
                    self.blob.write_faults['catalog.json'] = '500'
                elif case == 'signed in elsewhere':
                    self.page.route('**/api/catalog/*/delete', tamper)
                else:
                    self.blob.write_catalog(bob_owns_it)
                button.click()
                self.page.wait_for_function('t => (document.querySelector(".confirm__error:not([hidden])") || {})'
                                            '.textContent === t', arg=says, timeout=15000)
                self.assertTrue(button.is_enabled())
                self.assertEqual(self.active('a.textContent'), 'Cancel')
                self.blob.write_faults.clear()
                self.page.unroute('**/api/catalog/*/delete')
                self.assertIn(self.mine['id'], self.listed())
        log = self.hosted.log.getvalue()
        for detail in ('(storage_error): catalog.json: storage refused the request (HTTP 403)',
                       '(delete_unconfirmed): catalog.json: storage answered HTTP 500', '(not_owner): only the owner'):
            self.assertIn('could not delete %s for alice@example.com %s' % (self.mine['id'], detail), log)
        self.assertNotIn('HTTP', self.dialog().inner_text())
        # Trying again once all is well works from the same dialog.
        self.blob.write_catalog([self.mine, self.theirs, self.unowned])
        button.click()
        self.page.wait_for_selector('text=Deleted Coast studies.', timeout=15000)
        self.assertEqual(self.listed(), [self.theirs['id'], self.unowned['id']])

    def test_the_dialog_stays_open_while_deleting_and_shows_a_failure(self):
        """Chromium closes a modal dialog on a repeated Esc without a cancelable event; while the request runs the
        dialog opens again, so that a failure is shown in it rather than nowhere."""
        self.refuse_slowly()
        self.card(self.mine).locator('button', has_text='Delete…').click()
        self.dialog().locator('button', has_text='Delete export').click()
        for _ in range(3):
            self.page.wait_for_timeout(200)
            self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(200)
        self.assertTrue(self.dialog_open())
        self.assertEqual(self.dialog().locator('button', has_text='Deleting…').count(), 1)
        error = self.dialog().locator('.confirm__error')
        error.wait_for(timeout=15000)
        self.assertEqual(error.inner_text(), REFUSED)
        self.assertTrue(self.dialog_open())
        self.assertEqual(self.listed(), [self.mine['id'], self.theirs['id'], self.unowned['id']])
        self.page.keyboard.press('Escape')  # nothing runs now: Esc cancels
        self.dialog().wait_for(state='detached', timeout=15000)
        self.assertEqual(self.page.locator('.xcard--catalog').count(), 3)

    def test_a_failure_after_the_dialog_is_gone_is_shown_on_the_page(self):
        self.refuse_slowly()
        self.card(self.mine).locator('button', has_text='Delete…').click()
        self.dialog().locator('button', has_text='Delete export').click()
        self.page.wait_for_timeout(300)
        self.page.evaluate("() => document.querySelector('dialog.confirm').remove()")
        self.page.locator('.callout--err', has_text=REFUSED).wait_for(timeout=15000)
        self.assertEqual(self.dialog().count(), 0)
        self.assertFalse(self.page.evaluate("() => document.documentElement.classList.contains('is-locked')"))
        self.assertEqual(self.listed(), [self.mine['id'], self.theirs['id'], self.unowned['id']])

    def test_the_only_export_opening_leaves_an_open_dialog_alone(self):
        """With one published export the first visit opens it and goes to its overview once it is ready, unless a
        delete dialog is open by then: the dialog and the start page stay."""
        only = self.blob.put_export(fx.export_zip_bytes(fx.gallery_export(30), root='mj_export_only_20260430'),
                                    name='mj_export_only_20260430.zip', label='Only one', owner='alice@example.com')
        self.blob.write_catalog([only])
        self.blob.faults[only['blob']] = 'slow'
        self.blob.chunk, self.blob.slow_delay = 512, 0.05
        self.page.reload()
        self.card(only).locator('button', has_text='Delete…').click()
        self.dialog().wait_for(timeout=15000)
        # Downloaded and ready: its card links to it, or (what must not happen) the overview is shown.
        self.page.wait_for_function("() => location.hash.includes('/overview') || document.querySelector('.xcard__link')",
                                    timeout=60000)
        self.page.wait_for_timeout(500)
        self.assertNotIn('/overview', self.page.evaluate('location.hash'))
        self.assertTrue(self.dialog_open())
        self.dialog().locator('button', has_text='Cancel').click()
        self.dialog().wait_for(state='detached', timeout=15000)
        self.assertNotIn('/overview', self.page.evaluate('location.hash'))
        self.assertEqual(self.listed(), [only['id']])

    def test_the_dialog_says_what_the_storage_keeps_as_the_deployment_states_it(self):
        for days, says in ((None, "The storage's own settings decide whether a recoverable copy is kept."),
                           ('1', "The viewer's storage keeps a recoverable copy for 1 day, then deletes it.")):
            with self.subTest(days=days):
                settings = {'MJV_CATALOG_WRITE_URL': self.blob.write_url,
                            'MJV_CACHE_DIR': os.path.join(self._tmp.name, 'cache-days-%s' % days)}
                if days:
                    settings['MJV_SOFT_DELETE_DAYS'] = days
                other = Hosted(self._tmp.name, self.blob, self.google, env=settings)
                self.addCleanup(other.stop)
                self.page.goto(other.origin + '/')
                self.card(self.mine).locator('button', has_text='Delete…').click()
                self.dialog().wait_for(timeout=15000)
                self.assertIn(says + ' Copies shared in other ways are not affected.', self.dialog().inner_text())
                self.page.keyboard.press('Escape')
                self.dialog().wait_for(state='detached', timeout=15000)
        self.assertEqual(self.listed(), [self.mine['id'], self.theirs['id'], self.unowned['id']])

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
