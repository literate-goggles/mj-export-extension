"""End-to-end test of hiding generations and moodboards in the hosted UI, in Chromium (opt-in).

Run from viewer/:   MJV_E2E=1 python3 -m unittest tests.test_e2e_hide -v

Like tests/test_e2e_delete.py it needs Playwright for Python and its Chromium build, and is skipped without
MJV_E2E=1, without Playwright, or when Chromium cannot start. A hosted viewer with writes on runs on 127.0.0.1
against tests/fake_google.py and tests/fake_blob.py, which holds two invented exports: tests/fixtures.hiding_export()
as a ZIP owned by alice@example.com, and a small one bob@example.com owns; carol@example.com is an administrator.
Each browser context signs in as one of them, and the browser aborts every request that is not for 127.0.0.1. The
items hiding_export() hides carry the marker 'zq' in every id, name and file name, so what anyone else sees while
they are hidden never contains it. A local-mode run checks that none of this shows there.
"""
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from mjviewer import model
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
ALICE, BOB, CAROL = 'alice@example.com', 'bob@example.com', 'carol@example.com'
# hiding_export() as published, and what it hides: both items of fixtures.HIDE, job-zq-solo alone, board-zq alone.
SUMMARY = {'jobs': 17, 'moodboards': 3, 'profiles': 2, 'images': 60, 'export_time': '2026-03-20T12:00:00.000Z',
           'extension_version': '0.4.0', 'status': 'completed_with_failures'}
COUNTS = {'both': {'jobs': 5, 'moodboards': 1, 'images': 18}, 'solo': {'jobs': 1, 'moodboards': 0, 'images': 4},
          'board': {'jobs': 4, 'moodboards': 1, 'images': 14}}
BOARD_JOBS = ('job-zq-exp', 'job-zq-derived', 'job-zq-inferred', 'job-zq-shared')  # what board-zq takes with it
REFUSED = 'The viewer could not update its list right now. Nothing changed. Try again in a few minutes.'
UNCONFIRMED = 'The viewer could not confirm whether this changed. Reload the page to check.'
STORED = "It stays in the stored export, and the owner or an administrator can show it again from the export's overview."
CACHE = 'Browsers that already showed its pictures may keep them for up to 10 minutes.'
GAP = 'The generation this one was made from is not available here, so the chain stops here.'


class _BrowserCase(unittest.TestCase):
    """Chromium with one browser context per signed-in person; every request that is not for 127.0.0.1 is aborted."""

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

    def setUp(self):
        self.contexts, self.aborted, self.console = [], [], []

    def new_page(self):
        context = self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT})
        context.route('**/*', self._only_local)
        self.contexts.append(context)
        page = context.new_page()
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
        for context in self.contexts:
            context.close()
        # No script errors or CSP refusals. Failed requests the UI handles are expected: a hidden item's link (not
        # found, as for an id that never existed), an export not open yet or whose list cannot be applied, a refusal
        # or a storage failure it reports; and the machine's network changing under Chromium.
        self.assertEqual([m for m in self.console if not any(s in m for s in (
            'net::ERR_NETWORK_CHANGED', 'status of 401', 'status of 403', 'status of 404', 'status of 409',
            'status of 502'))], [])
        self.assertEqual(self.aborted, [])

    # ------------------------------------------------------------ helpers

    @staticmethod
    def go(page, hash_):
        """Change the view and wait until the old one is gone (the new one is drawn on hashchange)."""
        page.evaluate("h => { const v = document.querySelector('#view > .page'); if (v) v.dataset.old = '1';"
                      ' location.hash = h; }', hash_)
        page.wait_for_function("() => { const v = document.querySelector('#view > .page'); return v && !v.dataset.old; }",
                               timeout=15000)

    def open_job(self, page, job_id, export_id=None):
        self.go(page, '#/x/%s/job/%s' % (export_id or self.x, job_id))
        page.wait_for_selector('.lineage-facts, .state--error', timeout=30000)

    def open_board(self, page, board_id, export_id=None):
        self.go(page, '#/x/%s/moodboard/%s' % (export_id or self.x, board_id))
        page.wait_for_selector('.legend, .state--error', timeout=30000)

    def open_overview(self, page, export_id=None):
        self.go(page, '#/x/%s/overview' % (export_id or self.x))
        page.wait_for_selector('.stats, .state--error', timeout=30000)

    def open_gallery(self, page, query=''):
        self.go(page, '#/x/%s/gallery%s' % (self.x, query))
        page.wait_for_function("() => /^\\d+ jobs?/.test((document.querySelector('.toolbar__count') || {}).textContent || '')",
                               timeout=30000)
        return int(page.locator('.toolbar__count').inner_text().split()[0])

    @staticmethod
    def title(page):
        return page.locator('.page-title').inner_text()

    @staticmethod
    def dialog(page):
        return page.locator('dialog.confirm')

    @staticmethod
    def active(page, script):
        return page.evaluate('() => { const a = document.activeElement; return %s; }' % script)

    @staticmethod
    def wait_focus(page, selector):
        page.wait_for_function('s => document.activeElement && document.activeElement.matches(s)', arg=selector,
                               timeout=15000)

    @staticmethod
    def wait_announced(page, text):
        page.wait_for_function("t => document.getElementById('announcer').textContent === t", arg=text, timeout=15000)

    @staticmethod
    def text_of(page):
        """Everything the view holds as text, also what is folded away (the raw record)."""
        return page.locator('#main').text_content()

    @staticmethod
    def status_of(page, url):
        """The status the page gets for a same-origin address, with its session (a Secure cookie)."""
        return page.evaluate('u => fetch(u, {cache: "no-store"}).then((r) => r.status)', url)


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class HideBrowserTests(_BrowserCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-hide-')
        cls.blob = FakeBlob(os.path.join(cls._tmp.name, 'blob'))
        cls.google = FakeGoogle(email=ALICE, hd='example.com')
        cls.hosted = Hosted(cls._tmp.name, cls.blob, cls.google, env={'MJV_CATALOG_WRITE_URL': cls.blob.write_url,
                                                                      'MJV_ADMIN_EMAILS': CAROL})
        cls.zips = {'mine': fx.hiding_zip_bytes(),
                    'theirs': fx.export_zip_bytes(fx.gallery_export(6), root='mj_export_bob_20260320'),
                    'two': fx.two_root_hiding_zip()}
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
        super().setUp()
        self.google.reset()
        self.google.email = ALICE
        self.blob.faults.clear()
        self.blob.write_faults.clear()
        self.blob.before_write = None
        self.mine = self.blob.put_export(self.zips['mine'], name='mj_export_hiding_20260320.zip', label='Hiding study',
                                         owner=ALICE, summary=dict(SUMMARY))
        self.theirs = self.blob.put_export(self.zips['theirs'], name='mj_export_bob_20260320.zip', label='Bob study',
                                           owner=BOB)
        self.blob.write_catalog([self.mine, self.theirs])
        self.x = self.mine['id']

    def sign_in(self, email, hosted=None):
        """A new browser context signed in as email (the fake Google approves whoever it is told), on the start page."""
        self.google.email = email
        page = self.new_page()
        page.goto((hosted or self.hosted).origin + '/')
        page.wait_for_selector('.xcard--catalog', timeout=30000)
        self.google.email = ALICE
        return page

    def write_hidden(self, jobs=(), moodboards=(), counts=None):
        """Publish hiding_export() with a hidden list, as the app writes one (alice hid every item)."""
        record = {'jobs': [{'id': j, 'hidden_at': '2026-11-02T09:00:00Z', 'hidden_by': ALICE} for j in jobs],
                  'moodboards': [{'id': b, 'hidden_at': '2026-11-02T09:05:00Z', 'hidden_by': ALICE} for b in moodboards]}
        if counts is not None:
            record['counts'] = counts
        self.blob.write_catalog([dict(self.mine, hidden=record), self.theirs])

    def record(self):
        """The hidden record of alice's export as storage holds it, or None."""
        entry = next(e for e in self.blob.read_json('catalog.json')['exports'] if e['id'] == self.x)
        return entry.get('hidden')

    def hidden_ids(self, kind):
        return [item['id'] for item in (self.record() or {}).get(kind) or []]

    @staticmethod
    def card(page, entry):
        return page.locator('.xcard--catalog').filter(has_text=entry['name'])

    def tab_count(self, page, label):
        return page.locator('#export-tabs .tab', has_text=label).locator('.tab__count').inner_text()

    def assert_not_found(self, page, what='Job'):
        self.assertEqual(self.title(page), '%s not found' % what)
        self.assertIn('no %s with this id in the export' % ('job' if what == 'Job' else 'moodboard'),
                      page.locator('.state--error').inner_text())

    # -------------------------------------------------------------- tests

    def test_the_owner_hides_a_generation_and_everyone_else_stops_seeing_it(self):
        bob = self.sign_in(BOB)
        before = self.open_gallery(bob)
        self.assertEqual(before, 17)
        alice = self.sign_in(ALICE)
        self.open_job(alice, 'job-zq-solo')
        button = alice.locator('.page-head__actions .hide-btn')
        self.assertEqual(button.get_attribute('aria-label'), 'Hide this generation from everyone else')
        self.assertEqual(button.get_attribute('title'), 'Hide this generation from everyone else')
        self.assertEqual(button.inner_text().strip(), 'Hide…')
        # Esc and Cancel close the dialog and change nothing; focus starts on Cancel and returns to the button.
        for close in ('Escape', 'Cancel'):
            with self.subTest(close=close):
                button.click()
                self.dialog(alice).wait_for(timeout=15000)
                text = self.dialog(alice).inner_text()
                for part in ('Hide this generation?', 'qxsolo lantern',
                             'Nobody else will see it in this viewer: its page, the gallery, search and moodboards '
                             'leave it out.',
                             '2 generations made from it (upscales, variations…) stay visible.', STORED, CACHE):
                    self.assertIn(part, text)
                self.assertEqual(self.active(alice, 'a.textContent'), 'Cancel')
                if close == 'Escape':
                    alice.keyboard.press('Escape')
                else:
                    self.dialog(alice).locator('button', has_text='Cancel').click()
                self.dialog(alice).wait_for(state='detached', timeout=15000)
                self.assertTrue(self.active(alice, "a.classList.contains('hide-btn')"))
                self.assertIsNone(self.record())
        button.click()
        confirm = self.dialog(alice).locator('button', has_text='Hide generation')
        self.assertIn('btn--primary', confirm.get_attribute('class'))
        confirm.click()
        banner = alice.locator('.hidden-banner')
        banner.wait_for(timeout=30000)
        self.dialog(alice).wait_for(state='detached', timeout=15000)
        self.wait_focus(alice, '.hidden-banner__title')
        self.wait_announced(alice, 'Hidden from everyone else')
        self.assertEqual(banner.locator('.hidden-banner__title').inner_text(), 'Hidden from everyone else')
        self.assertIn('Hidden by alice@example.com on', banner.inner_text())
        self.assertIn("Only the export's owner and the viewer's administrators can see it.", banner.inner_text())
        self.assertEqual(banner.locator('button', has_text='Show again…').count(), 1)
        self.assertEqual(alice.locator('.hide-btn').count(), 0)
        self.assertEqual([(item['id'], item['hidden_by']) for item in self.record()['jobs']], [('job-zq-solo', ALICE)])
        self.assertEqual(self.record()['counts'], COUNTS['solo'])
        picture = alice.locator('.shot img').first.get_attribute('src')
        self.assertTrue(picture.startswith('/files/%s/' % self.x))
        self.assertEqual(self.status_of(alice, picture), 200)  # its managers still can

        # Everyone else (a fresh page): gone from the gallery, search, its link and its pictures.
        bob.reload()
        bob.wait_for_selector('.toolbar__count', timeout=30000)
        self.assertEqual(self.open_gallery(bob, '?sort=oldest'), before - 1)
        bob.wait_for_function('n => document.querySelector("#export-tabs .tab__count").textContent === n',
                              arg=str(before - 1), timeout=15000)
        self.assertEqual(self.tab_count(bob, 'Gallery'), str(before - 1))
        self.assertEqual(bob.locator('a[href*="/job/job-zq-solo"]').count(), 0)
        child = bob.locator('ul.grid article.card', has=bob.locator('a[href*="/job/job-child-up"]'))
        self.assertEqual(child.locator('.card__lineage .hint').inner_text().strip(), 'Parent not available')
        self.assertEqual(child.locator('.card__lineage .hint').get_attribute('title'),
                         'The generation this one was made from is not available here')
        self.assertEqual(self.open_gallery(bob, '?q=lantern'), 2)  # job-child-up repeats its prompt; and job-niji
        self.assertEqual(bob.locator('a[href*="/job/job-zq-solo"]').count(), 0)
        self.open_job(bob, 'job-zq-solo')
        self.assert_not_found(bob)
        for url in (picture, picture + '?size=thumb'):
            self.assertEqual(self.status_of(bob, url), 404)
        # What was made from it stays, with a neutral gap where its parent was, and its id nowhere.
        for job_id, image in (('job-child-up', 3), ('job-child-v', 2)):
            with self.subTest(child=job_id):
                self.open_job(bob, job_id)
                details = bob.locator('.job-side .kv').first.inner_text()
                self.assertIn('Not available', details)
                self.assertIn('Image %d of the parent' % image, details)
                self.assertNotIn('Not in this export', details)
                self.assertEqual(bob.locator('.trail .mini--unavailable .mini__title').inner_text(), 'Parent not available')
                self.assertEqual(bob.locator('.trail .mini--unavailable .mini__meta').count(), 0)
                self.assertEqual(bob.locator('.lineage-facts').inner_text().strip(), GAP)
                self.assertEqual(bob.locator('.lineage-facts .callout').count(), 0)  # not a warning
                self.assertIn('ids of items not available here are left out',
                              bob.locator('section', has_text='Raw record').inner_text())
                self.assertNotIn('zq', self.text_of(bob))
        # A moodboard picture that is the hidden generation's shows Not available.
        self.open_board(bob, 'board-1')
        slot = bob.locator('.refs .ref', has_text='Reference 3')
        self.assertEqual(slot.locator('.ph--unavailable .ph__label').inner_text(), 'Not available')
        self.assertEqual(slot.locator('.copy-btn, .ref__dims, button.ref__btn').count(), 0)
        self.assertIn('ids and pictures of items not available here are left out',
                      bob.locator('section', has_text='Raw record').inner_text())
        self.assertNotIn('job-zq-solo', self.text_of(bob))
        self.assertEqual(bob.locator('.hide-btn, .hidden-banner, .section--hidden').count(), 0)
        log = self.hosted.log.getvalue()
        self.assertIn('hid a generation in %s by alice@example.com; hidden now: 1 generations, 0 moodboards' % self.x, log)

    def test_hiding_a_moodboard_takes_its_generations_with_it(self):
        alice = self.sign_in(ALICE)
        self.open_board(alice, 'board-zq')
        actions = alice.locator('.page-head__actions')
        self.assertEqual(actions.locator('a', has_text='Show in gallery').count(), 1)
        button = actions.locator('.hide-btn')
        self.assertEqual(button.get_attribute('aria-label'), 'Hide this moodboard from everyone else')
        button.click()
        self.dialog(alice).wait_for(timeout=15000)
        text = self.dialog(alice).inner_text()
        for part in ('Hide this moodboard?', 'Zqboard',
                     'In this viewer, nobody else will see the moodboard, its reference images or the 4 generations '
                     'made with it.',
                     '1 generation made from those (an upscale, a variation…) stays visible.', STORED, CACHE):
            self.assertIn(part, text)
        self.dialog(alice).locator('button', has_text='Hide moodboard').click()
        banner = alice.locator('.hidden-banner')
        banner.wait_for(timeout=30000)
        self.wait_focus(alice, '.hidden-banner__title')
        self.wait_announced(alice, 'Hidden from everyone else')
        self.assertIn('with its reference images and the 4 generations made with it.', banner.inner_text())
        self.assertIn("Only the export's owner and the viewer's administrators can see them.", banner.inner_text())
        # Neither Show in gallery (the gallery is what everyone sees) nor Hide…; its generations are still listed.
        self.assertEqual(alice.locator('.page-head__actions').count(), 0)
        self.assertEqual(alice.locator('.section--gen .grid__item').count(), 4)
        self.assertEqual(self.hidden_ids('moodboards'), ['board-zq'])
        self.assertEqual(self.record()['counts'], COUNTS['board'])

        bob = self.sign_in(BOB)
        self.go(bob, '#/x/%s/moodboards' % self.x)
        bob.wait_for_selector('article.bcard', timeout=30000)
        self.assertEqual(bob.locator('article.bcard .bcard__title').all_inner_texts(),
                         ['Synthetic board one', 'Synthetic board two'])
        self.assertEqual(self.open_gallery(bob), 13)
        bob.wait_for_function("() => document.querySelectorAll('select[name=moodboard] option').length > 1", timeout=15000)
        self.assertEqual(bob.locator('select[name=moodboard] option').evaluate_all('o => o.map((e) => e.value)'),
                         ['', 'board-1', 'board-2'])
        self.assertEqual(bob.locator(', '.join('ul.grid a[href*="/job/%s"]' % j for j in BOARD_JOBS)).count(), 0)
        self.assertEqual(bob.locator('ul.grid a[href*="/job/job-zq-solo"]').count(), 3)  # not hidden: its card, 2 children's
        for job_id in BOARD_JOBS:
            with self.subTest(job=job_id):
                self.open_job(bob, job_id)
                self.assert_not_found(bob)
        self.open_board(bob, 'board-1')
        self.assertEqual(bob.locator('.section--gen a[href*="/job/job-zq-shared"]').count(), 0)
        for name in ('job-zq-shared', 'Zqboard', 'zqmb'):  # not even in its raw generated_job_ids
            self.assertNotIn(name, self.text_of(bob))
        self.open_board(bob, 'board-zq')
        self.assert_not_found(bob, 'Moodboard')
        self.open_job(bob, 'job-exp-var')  # a variation of a generation made with it stays, with a neutral gap
        self.assertEqual(bob.locator('.lineage-facts').inner_text().strip(), GAP)

    def test_the_hidden_section_shows_items_and_shows_them_again(self):
        self.write_hidden(jobs=['job-zq-solo', 'job-zq-absent'], moodboards=['board-zq'], counts=COUNTS['both'])
        alice = self.sign_in(ALICE)
        self.assertEqual(self.card(alice, self.mine).locator('.xcard__hidden').inner_text().strip(),
                         '5 generations and 1 moodboard hidden from others')
        self.open_overview(alice)
        section = alice.locator('.section--hidden')
        section.locator('.hidden-list').first.wait_for(timeout=30000)
        self.assertEqual(section.locator('.section__title .count').inner_text(), '3')
        self.assertIn("Only this export's owner and the viewer's administrators see this.", section.inner_text())
        self.assertIn("Generations hidden with a moodboard are listed on the moodboard's page, not here.",
                      section.inner_text())
        boards = section.locator('ul[aria-label="Hidden moodboards"] > li')
        jobs = section.locator('ul[aria-label="Hidden generations"] > li')
        self.assertEqual((boards.count(), jobs.count()), (1, 2))
        board_row = boards.first.inner_text()
        for part in ('Zqboard', '4 generations hidden with it', 'Hidden by alice@example.com on'):
            self.assertIn(part, board_row)
        self.assertTrue(boards.first.locator('a.hidden-row__title').get_attribute('href').endswith(
            '#/x/%s/moodboard/board-zq' % self.x))
        solo = jobs.filter(has_text='qxsolo lantern')
        absent = jobs.filter(has_text='Not in this export')
        for part in ('Created', 'Hidden by alice@example.com on'):
            self.assertIn(part, solo.inner_text())
        self.assertTrue(solo.locator('a.hidden-row__title').get_attribute('href').endswith('#/x/%s/job/job-zq-solo' % self.x))
        self.assertIn('Generation job-zq-a…', absent.inner_text())

        # Show the generation again: Esc first, then for real.
        show = solo.locator('button', has_text='Show again…')
        show.click()
        self.dialog(alice).wait_for(timeout=15000)
        text = self.dialog(alice).inner_text()
        for part in ('Show this generation again?', 'qxsolo lantern', 'Everyone who can open this export will see it again.'):
            self.assertIn(part, text)
        self.assertIn('btn--danger', self.dialog(alice).locator('button', has_text='Show again').get_attribute('class'))
        alice.keyboard.press('Escape')
        self.dialog(alice).wait_for(state='detached', timeout=15000)
        self.assertEqual(self.active(alice, "a.getAttribute('aria-label')"), 'Show this generation again')
        show.click()
        self.dialog(alice).locator('button', has_text='Show again').click()
        alice.wait_for_function("() => (document.querySelector('.section--hidden .section__title .count') || {})"
                                ".textContent === '2'", timeout=30000)
        self.wait_focus(alice, '.section--hidden .section__title')
        self.wait_announced(alice, 'Shown to everyone again')
        self.assertEqual(self.hidden_ids('jobs'), ['job-zq-absent'])

        # An id that names nothing in the export: showing it again changes nothing for anyone.
        section.locator('ul[aria-label="Hidden generations"] > li').first.locator('button', has_text='Show again…').click()
        self.assertIn('It is not in this export, so nothing changes for anyone.', self.dialog(alice).inner_text())
        self.dialog(alice).locator('button', has_text='Show again').click()
        alice.wait_for_function("() => (document.querySelector('.section--hidden .section__title .count') || {})"
                                ".textContent === '1'", timeout=30000)

        # The moodboard, with its generations.
        section.locator('ul[aria-label="Hidden moodboards"] > li').first.locator('button', has_text='Show again…').click()
        self.dialog(alice).wait_for(timeout=15000)
        text = self.dialog(alice).inner_text()
        for part in ('Show this moodboard again?', 'Zqboard',
                     'Everyone who can open this export will see it again, with its 4 generations.'):
            self.assertIn(part, text)
        self.dialog(alice).locator('button', has_text='Show again').click()
        section.locator('text=Nothing is hidden.').wait_for(timeout=30000)
        self.wait_focus(alice, '.section--hidden .section__title')
        self.assertIsNone(self.record())  # the last item takes the record with it

        bob = self.sign_in(BOB)
        self.open_job(bob, 'job-zq-solo')
        self.assertEqual(self.title(bob), 'qxsolo lantern')
        self.open_board(bob, 'board-zq')
        self.assertEqual(self.title(bob), 'Zqboard')
        self.assertEqual(bob.locator('.section--gen .grid__item').count(), 4)

    def test_a_generation_hidden_with_its_moodboard_can_be_hidden_on_its_own(self):
        self.write_hidden(moodboards=['board-zq'], counts=COUNTS['board'])
        alice = self.sign_in(ALICE)
        self.open_job(alice, 'job-zq-exp')
        banner = alice.locator('.hidden-banner')
        text = banner.inner_text()
        self.assertIn('It is hidden with the moodboard Zqboard (hidden by alice@example.com on', text)
        self.assertIn('It comes back when the moodboard is shown again.', text)
        self.assertTrue(banner.locator('a', has_text='Open the moodboard').get_attribute('href').endswith(
            '#/x/%s/moodboard/board-zq' % self.x))
        self.assertEqual(banner.locator('button', has_text='Show again…').count(), 0)
        self.assertEqual(alice.locator('.hide-btn').count(), 0)
        banner.locator('button', has_text='Hide on its own…').click()
        self.dialog(alice).wait_for(timeout=15000)
        text = self.dialog(alice).inner_text()
        for part in ('Hide this generation on its own?', 'zqexp harbor',
                     'It is hidden now because the moodboard Zqboard is hidden. Hidden on its own, it stays hidden '
                     'when the moodboard is shown again.'):
            self.assertIn(part, text)
        self.dialog(alice).locator('button', has_text='Hide on its own').click()
        alice.wait_for_function("() => (document.querySelector('.hidden-banner__text') || {}).textContent"
                                " && document.querySelector('.hidden-banner__text').textContent.includes('and with the moodboard Zqboard.')",
                                timeout=30000)
        self.wait_focus(alice, '.hidden-banner__title')
        self.wait_announced(alice, 'Hidden on its own')
        self.assertEqual(self.hidden_ids('jobs'), ['job-zq-exp'])
        self.assertEqual(self.record()['counts'], COUNTS['board'])  # nothing new is hidden from anyone

        # The moodboard comes back from its own page's banner, without that generation.
        self.open_board(alice, 'board-zq')
        alice.locator('.hidden-banner button', has_text='Show again…').click()
        self.dialog(alice).wait_for(timeout=15000)
        text = self.dialog(alice).inner_text()
        for part in ('Show this moodboard again?',
                     'Everyone who can open this export will see it again, with 3 of its 4 generations.',
                     '1 of its generations stays hidden: it is hidden on its own or with another hidden moodboard.'):
            self.assertIn(part, text)
        self.dialog(alice).locator('button', has_text='Show again').click()
        alice.locator('.page-head__actions .hide-btn').wait_for(timeout=30000)
        self.assertEqual(alice.locator('.hidden-banner').count(), 0)
        self.assertEqual(alice.locator('.page-head__actions a', has_text='Show in gallery').count(), 1)
        self.wait_focus(alice, 'h1.page-title')
        self.wait_announced(alice, 'Shown to everyone again')
        self.assertEqual((self.hidden_ids('jobs'), self.hidden_ids('moodboards')), (['job-zq-exp'], []))

        bob = self.sign_in(BOB)
        self.open_job(bob, 'job-zq-exp')
        self.assert_not_found(bob)
        self.open_job(bob, 'job-zq-derived')
        self.assertEqual(self.title(bob), 'zqderived kite')

        # The generation comes back from its own banner.
        self.open_job(alice, 'job-zq-exp')
        banner = alice.locator('.hidden-banner')
        self.assertIn('Hidden by alice@example.com on', banner.inner_text())
        banner.locator('button', has_text='Show again…').click()
        self.dialog(alice).wait_for(timeout=15000)
        self.assertIn('Everyone who can open this export will see it again.', self.dialog(alice).inner_text())
        self.dialog(alice).locator('button', has_text='Show again').click()
        alice.locator('.page-head__actions .hide-btn').wait_for(timeout=30000)
        self.assertEqual(alice.locator('.hidden-banner').count(), 0)
        self.wait_focus(alice, 'h1.page-title')
        self.assertIsNone(self.record())
        self.open_job(bob, 'job-zq-exp')
        self.assertEqual(self.title(bob), 'zqexp harbor')

    def test_failures_are_said_in_plain_words_and_keep_the_dialog(self):
        """Each failure gets a plain sentence; what storage answered goes to the server log only."""
        alice = self.sign_in(ALICE)
        self.open_job(alice, 'job-zq-solo')
        alice.locator('.hide-btn').click()
        button = self.dialog(alice).locator('button', has_text='Hide generation')
        bob_owns_it = [dict(self.mine, owner=BOB), self.theirs]
        for case, says in (('storage refuses', REFUSED), ('no clear answer', UNCONFIRMED),
                           ('another owner', 'Could not hide: only the owner of this export or an administrator can '
                                             'hide or show its generations and moodboards')):
            with self.subTest(case=case):
                if case == 'storage refuses':
                    self.blob.write_faults['catalog.json'] = '403'
                elif case == 'no clear answer':
                    self.blob.write_faults['catalog.json'] = '500'
                else:
                    self.blob.write_catalog(bob_owns_it)
                button.click()
                alice.wait_for_function('t => (document.querySelector(".confirm__error:not([hidden])") || {})'
                                        '.textContent === t', arg=says, timeout=30000)
                self.assertTrue(button.is_enabled())
                self.assertEqual(button.inner_text(), 'Hide generation')
                self.assertEqual(self.active(alice, 'a.textContent'), 'Cancel')
                self.blob.write_faults.clear()
                self.assertIsNone(self.record())
        log = self.hosted.log.getvalue()
        for detail in ('(storage_error): catalog.json: storage refused the request (HTTP 403)',
                       '(hide_unconfirmed): catalog.json: storage answered HTTP 500', '(not_owner): only the owner'):
            self.assertIn('could not hide in %s for alice@example.com %s' % (self.x, detail), log)
        self.assertNotIn('HTTP', self.dialog(alice).inner_text())
        # Trying again once all is well works from the same dialog.
        self.blob.write_catalog([self.mine, self.theirs])
        button.click()
        alice.locator('.hidden-banner').wait_for(timeout=30000)
        self.assertEqual(self.hidden_ids('jobs'), ['job-zq-solo'])

    def test_a_failure_after_the_dialog_is_gone_is_shown_on_the_page(self):
        alice = self.sign_in(ALICE)
        self.open_job(alice, 'job-zq-solo')

        def slow_then_refuse(name, method):
            if name == 'catalog.json':
                time.sleep(1.5)
                self.blob.write_faults['catalog.json'] = '403'

        self.blob.before_write = slow_then_refuse
        alice.locator('.hide-btn').click()
        self.dialog(alice).locator('button', has_text='Hide generation').click()
        alice.wait_for_timeout(300)
        alice.evaluate("() => document.querySelector('dialog.confirm').remove()")
        alice.locator('#notices .callout--err', has_text=REFUSED).wait_for(timeout=30000)
        self.assertEqual(self.dialog(alice).count(), 0)
        self.assertIsNone(self.record())
        self.assertEqual(alice.locator('.hide-btn').count(), 1)

    def test_only_owners_and_administrators_see_any_of_it(self):
        self.write_hidden(jobs=['job-zq-solo'], moodboards=['board-zq'], counts=COUNTS['both'])
        theirs = self.theirs['id']

        bob = self.sign_in(BOB)
        card = self.card(bob, self.mine)
        self.assertEqual(card.locator('.xcard__hidden, .xcard__delete').count(), 0)
        self.assertEqual(card.locator('.xcard__count', has_text='Jobs').locator('dd').inner_text(), '12')
        self.open_overview(bob)
        self.assertEqual(bob.locator('.section--hidden').count(), 0)
        self.assertNotIn('zq', self.text_of(bob))
        self.open_job(bob, 'job-grid-a')
        self.assertEqual(bob.locator('.hide-btn, .hidden-banner').count(), 0)
        self.open_job(bob, 'job-zq-solo')
        self.assert_not_found(bob)
        self.open_board(bob, 'board-zq')
        self.assert_not_found(bob, 'Moodboard')
        self.open_job(bob, 'gjob-000', export_id=theirs)  # bob manages his own export
        self.assertEqual(bob.locator('.page-head__actions .hide-btn').count(), 1)

        carol = self.sign_in(CAROL)
        card = self.card(carol, self.mine)
        self.assertEqual(card.locator('.xcard__hidden').inner_text().strip(),
                         '5 generations and 1 moodboard hidden from others')
        self.assertEqual(card.locator('.xcard__count', has_text='Jobs').locator('dd').inner_text(), '12')
        self.assertEqual(self.card(carol, self.theirs).locator('.xcard__hidden').count(), 0)
        card.locator('button', has_text='Delete…').click()
        self.dialog(carol).wait_for(timeout=15000)
        self.assertIn('(plus 5 hidden generations and 1 hidden moodboard)', self.dialog(carol).inner_text())
        carol.keyboard.press('Escape')
        self.dialog(carol).wait_for(state='detached', timeout=15000)
        self.open_overview(carol)
        carol.locator('.section--hidden .hidden-list').first.wait_for(timeout=30000)
        self.assertEqual(carol.locator('.section--hidden .hidden-row').count(), 2)
        self.open_job(carol, 'job-grid-a')
        self.assertEqual(carol.locator('.page-head__actions .hide-btn').count(), 1)
        self.open_job(carol, 'job-zq-solo')
        self.assertEqual(carol.locator('.hidden-banner').count(), 1)
        self.open_job(carol, 'gjob-000', export_id=theirs)
        self.assertEqual(carol.locator('.page-head__actions .hide-btn').count(), 1)

        # With writes off nobody manages anything, and the list still applies to everyone, its owner included.
        off = Hosted(self._tmp.name, self.blob, self.google, env={'MJV_ADMIN_EMAILS': CAROL,
                                                                  'MJV_CACHE_DIR': os.path.join(self._tmp.name, 'cache-off')})
        self.addCleanup(off.stop)
        for email in (ALICE, CAROL):
            with self.subTest(writes='off', user=email):
                page = self.sign_in(email, hosted=off)
                self.assertEqual(self.card(page, self.mine).locator('.xcard__hidden, .xcard__delete').count(), 0)
                self.open_overview(page)
                self.assertEqual(page.locator('.section--hidden').count(), 0)
                self.assertNotIn('zq', self.text_of(page))
                self.open_job(page, 'job-grid-a')
                self.assertEqual(page.locator('.hide-btn').count(), 0)
                self.open_job(page, 'job-zq-solo')
                self.assert_not_found(page)

    def test_a_zip_with_two_roots_is_managed_from_either_root(self):
        """A hidden list belongs to the published ZIP: a hide from one root's page names the catalog entry, and the
        Hidden section of each root's overview lists all of it, with the root that has each item."""
        both = self.blob.put_export(self.zips['two'], name='mj_export_two_roots_20260324.zip', label='Two roots',
                                    owner=ALICE)
        record = {'jobs': [{'id': 'job-zq-solo', 'hidden_at': '2026-11-02T09:00:00Z', 'hidden_by': ALICE}],
                  'moodboards': [{'id': 'board-zq', 'hidden_at': '2026-11-02T09:05:00Z', 'hidden_by': ALICE}]}
        self.blob.write_catalog([self.mine, self.theirs, dict(both, hidden=record)])
        first, second = both['id'] + '-1', both['id'] + '-2'
        alice = self.sign_in(ALICE)
        self.open_job(alice, 'job-r2-plain', export_id=second)
        alice.locator('.hide-btn').click()
        self.dialog(alice).locator('button', has_text='Hide generation').click()
        alice.locator('.hidden-banner').wait_for(timeout=30000)
        entry = next(e for e in self.blob.read_json('catalog.json')['exports'] if e['id'] == both['id'])
        self.assertEqual([item['id'] for item in entry['hidden']['jobs']], ['job-zq-solo', 'job-r2-plain'])

        self.open_overview(alice, export_id=second)
        section = alice.locator('.section--hidden')
        section.locator('.hidden-list').first.wait_for(timeout=30000)
        self.assertEqual(section.locator('.section__title .count').inner_text(), '3')
        board = section.locator('ul[aria-label="Hidden moodboards"] > li')
        self.assertIn('5 generations hidden with it', board.inner_text())  # over both roots
        self.assertIn('In Two roots › mj_export_hiding_first', board.inner_text())
        self.assertTrue(board.locator('a.hidden-row__title').get_attribute('href').endswith(
            '#/x/%s/moodboard/board-zq' % first))
        plain = section.locator('ul[aria-label="Hidden generations"] > li', has_text='second root orchard')
        self.assertIn('In Two roots › mj_export_hiding_second', plain.inner_text())
        plain.locator('button', has_text='Show again…').click()
        self.dialog(alice).locator('button', has_text='Show again').click()
        alice.wait_for_function("() => (document.querySelector('.section--hidden .section__title .count') || {})"
                                ".textContent === '2'", timeout=30000)
        entry = next(e for e in self.blob.read_json('catalog.json')['exports'] if e['id'] == both['id'])
        self.assertEqual([item['id'] for item in entry['hidden']['jobs']], ['job-zq-solo'])
        # A generation of root 2 hidden with board-zq, which only root 1 has: its banner opens the moodboard there.
        self.open_job(alice, 'job-zq-r2', export_id=second)
        link = alice.locator('.hidden-banner a', has_text='Open the moodboard')
        self.assertTrue(link.get_attribute('href').endswith('#/x/%s/moodboard/board-zq' % first))
        link.click()
        alice.wait_for_function("() => (document.querySelector('.page-title') || {}).textContent === 'Zqboard'",
                                timeout=30000)
        self.assertTrue(alice.evaluate('location.hash').endswith('/x/%s/moodboard/board-zq' % first))

        bob = self.sign_in(BOB)
        self.open_job(bob, 'job-r2-child', export_id=second)  # made from job-zq-solo, which is in the other root
        self.assertEqual(bob.locator('.lineage-facts').inner_text().strip(), GAP)
        self.open_job(bob, 'job-zq-r2', export_id=second)  # references board-zq by its id, which root 2 left out
        self.assert_not_found(bob)
        self.open_overview(bob, export_id=second)
        self.assertNotIn('zq', self.text_of(bob))

    def test_a_failed_build_shows_managers_their_hidden_list(self):
        """When an instance cannot apply a hidden list, everyone else gets a plain failure, while its managers see
        why and the Hidden section, from which they can show items again."""
        self.write_hidden(jobs=['job-zq-solo'], counts=COUNTS['solo'])
        with mock.patch.object(model.ExportIndex, 'without', side_effect=RuntimeError('synthetic build failure')):
            bob = self.sign_in(BOB)
            self.open_overview(bob)
            self.assertIn('This export could not be opened: could not prepare this export',
                          bob.locator('.state--error').inner_text())
            self.assertEqual(bob.locator('.section--hidden').count(), 0)
            alice = self.sign_in(ALICE)
            self.open_overview(alice)
            self.assertIn('This export could not be opened: could not apply the list of hidden items (RuntimeError)',
                          alice.locator('.state--error').inner_text())
            section = alice.locator('.section--hidden')
            section.locator('.hidden-row').first.wait_for(timeout=30000)
            self.assertIn('qxsolo lantern', section.inner_text())
            section.locator('button', has_text='Show again…').click()
            self.dialog(alice).locator('button', has_text='Show again').click()
            # The list is empty now, so the export opens; the section says so.
            alice.wait_for_selector('.stats', timeout=30000)
            section.locator('text=Nothing is hidden.').wait_for(timeout=30000)
            self.wait_focus(alice, '.section--hidden .section__title')
        self.assertIsNone(self.record())
        self.assertIn('could not apply the hidden list of %s (RuntimeError)' % self.x, self.hosted.log.getvalue())


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class LocalModeTests(_BrowserCase):
    """Local mode has no managers and nothing hidden: no button, no banner, no section, no neutral gap."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-hide-local-')
        cls.app = ViewerApp(cache_dir=os.path.join(cls._tmp.name, 'cache'))
        cls.x = cls.app.registry.open(fx.write_zip(fx.hiding_export(), os.path.join(cls._tmp.name, 'hiding.zip')))[0].id
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

    def test_local_mode_shows_no_hide_controls(self):
        page = self.new_page()
        page.goto('http://127.0.0.1:%d/' % self.server.port)
        page.wait_for_selector('.stats', timeout=30000)  # the only export: straight to its overview
        page.wait_for_timeout(300)
        self.assertEqual(page.locator('.section--hidden').count(), 0)
        for job_id in ('job-zq-solo', 'job-child-up'):
            self.open_job(page, job_id)
            self.assertEqual(page.locator('.hide-btn, .hidden-banner, .mini--unavailable').count(), 0)
        self.assertEqual(page.locator('.job-side .kv a.job-link').inner_text(), 'job-zq-solo')  # its parent, as usual
        self.open_board(page, 'board-1')
        self.assertEqual(page.locator('.hide-btn, .hidden-banner, .ph--unavailable').count(), 0)
        self.assertEqual(page.locator('.page-head__actions a', has_text='Show in gallery').count(), 1)


if __name__ == '__main__':
    unittest.main()
