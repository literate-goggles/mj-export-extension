"""End-to-end test of the real UI in Chromium, on a synthetic export (opt-in).

Run from viewer/:   MJV_E2E=1 python3 -m unittest tests.test_e2e_browser -v

It needs Playwright for Python and its Chromium build
(pip install playwright && python3 -m playwright install chromium) and is
skipped without MJV_E2E=1, without Playwright, or when Chromium cannot start.
The server listens on 127.0.0.1 only and the data comes from
tests/fixtures.gallery_export(): 150 invented jobs, two boards, two profiles.
HostedBrowserTests runs hosted mode the same way, with tests/fake_google.py for
the sign-in and tests/fake_blob.py holding the published synthetic exports; one
test puts two instances behind a strict round-robin front end
(tests/hosted_harness.RoundRobin).
"""
import hashlib
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.parse

from mjviewer.server import ViewerApp, make_server
from tests import fixtures as fx
from tests.fake_blob import FakeBlob
from tests.fake_google import FakeGoogle
from tests.hosted_harness import Hosted, RoundRobin

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None

ENABLED = os.environ.get('MJV_E2E') == '1'
WIDTH, HEIGHT = 1280, 800
N_JOBS = 150
PAGE_SIZE = 60


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-')
        cls.export = fx.gallery_export(N_JOBS)
        path = fx.write_zip(cls.export, os.path.join(cls._tmp.name, 'gallery.zip'), root='mj_export_e2e_20260430')
        cls.app = ViewerApp(cache_dir=os.path.join(cls._tmp.name, 'cache'))
        cls.export_id = cls.app.registry.open(path)[0].id
        cls.server = make_server(cls.app, '127.0.0.1', 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.port
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:  # Chromium not installed for Playwright
            cls._stop_server()
            cls.playwright.stop()
            raise unittest.SkipTest('Chromium for Playwright is unavailable: %s' % str(exc).splitlines()[0])

    @classmethod
    def _stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()
        cls._tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls._stop_server()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT})
        self.page = self.context.new_page()
        self.console, self.requests = [], []
        self.page.on('console', lambda m: self.console.append(m.text) if m.type == 'error' else None)
        self.page.on('pageerror', lambda e: self.console.append(str(e)))
        self.page.on('request', lambda r: self.requests.append(r.url))
        self.page.goto(self.base + '/')
        self.page.wait_for_selector('.page-title', timeout=15000)

    def tearDown(self):
        self.context.close()
        # No script errors or CSP refusals (Chromium reports both on the console), and nothing
        # was requested from anywhere but the viewer itself. ERR_NETWORK_CHANGED is the machine's
        # network changing under Chromium (it aborts even local requests); the UI retries those.
        self.assertEqual([m for m in self.console if 'net::ERR_NETWORK_CHANGED' not in m], [])
        hosts = {urllib.parse.urlsplit(u).netloc for u in self.requests if not u.startswith(('data:', 'blob:'))}
        self.assertEqual(hosts, {urllib.parse.urlsplit(self.base).netloc})

    # ------------------------------------------------------------ helpers

    def go(self, hash_):
        self.page.evaluate('h => { location.hash = h; }', hash_)

    def count_text(self, expected):
        self.page.wait_for_function("t => (document.querySelector('.toolbar__count') || {}).textContent === t",
                                    arg=expected, timeout=15000)

    def cards(self):
        return self.page.locator('ul.grid > li.grid__item:not(.grid__item--skeleton)')

    def wait_job(self, job_id):
        """Wait for the page of job_id itself, not the previous page that is still on screen."""
        self.page.wait_for_function(
            "id => location.hash.includes('/job/' + encodeURIComponent(id)) && document.querySelector('.lineage-facts')"
            " && [...document.querySelectorAll('.id-value__text')].some((e) => e.textContent === id)",
            arg=job_id, timeout=15000)

    def file_requests(self):
        return [u for u in self.requests if '/files/' in u]

    def job_row(self, job_id):
        return next(j for j in self.export.jobs if j['job_id'] == job_id)

    # -------------------------------------------------------------- tests

    def test_gallery_pages_and_loads_thumbnails_lazily(self):
        self.go('#/x/%s/gallery' % self.export_id)
        self.count_text('%d jobs' % N_JOBS)
        self.assertEqual(self.cards().count(), PAGE_SIZE)
        self.page.wait_for_timeout(300)
        images = self.page.evaluate("""() => [...document.querySelectorAll('ul.grid img')].map((i) => ({
            requested: Boolean(i.getAttribute('src')), waiting: Boolean(i.dataset.src),
            top: i.getBoundingClientRect().top}))""")
        requested = [i for i in images if i['requested']]
        self.assertTrue(requested, 'thumbnails in view are requested')
        self.assertTrue([i for i in images if i['waiting']], 'thumbnails below the fold wait')
        self.assertFalse([i for i in requested if i['top'] >= HEIGHT], 'nothing below the fold is requested')
        before = set(self.file_requests())
        self.assertTrue(before and len(before) < PAGE_SIZE * 4)
        self.assertTrue(all(u.endswith('?size=thumb') for u in before))

        # Scrolling loads the next pages and the thumbnails that come into view.
        for _ in range(60):
            if self.cards().count() >= N_JOBS:
                break
            self.page.evaluate('window.scrollBy(0, window.innerHeight)')
            self.page.wait_for_timeout(120)
        self.assertEqual(self.cards().count(), N_JOBS)
        self.page.wait_for_function("() => /End of results/.test(document.querySelector('.grid-footer').textContent)",
                                    timeout=15000)
        after = set(self.file_requests())
        self.assertGreater(len(after), len(before))
        self.assertTrue(all(u.endswith('?size=thumb') for u in after))
        # Each page is fetched (a GET aborted by a network change is retried, so count pages, not
        # requests); the exact card count above shows no page was added twice.
        jobs_requests = [u for u in self.requests if '/jobs?' in u]
        self.assertEqual(sorted({int(re.search(r'offset=(\d+)', u).group(1)) for u in jobs_requests}), [0, 60, 120])

    def test_search_by_prompt(self):
        self.go('#/x/%s/gallery' % self.export_id)
        self.count_text('%d jobs' % N_JOBS)
        self.page.fill('input[data-search]', 'ZEPHYR')
        self.count_text('3 jobs of %d' % N_JOBS)
        self.assertEqual(self.cards().count(), 3)
        prompts = self.page.locator('ul.grid .card__prompt').all_inner_texts()
        self.assertTrue(all('zephyr' in p for p in prompts), prompts)
        self.assertIn('q=ZEPHYR', self.page.evaluate('location.hash'))
        self.page.fill('input[data-search]', 'zephyr 031')  # every term must match
        self.count_text('1 job of %d' % N_JOBS)
        self.page.fill('input[data-search]', 'no such prompt anywhere')
        self.page.wait_for_selector('text=No jobs match these filters', timeout=15000)
        self.page.locator('input[data-search]').press('Escape')
        self.count_text('%d jobs' % N_JOBS)

    def test_filter_by_version_and_kind(self):
        self.go('#/x/%s/gallery' % self.export_id)
        self.count_text('%d jobs' % N_JOBS)
        expected_v7 = sum(1 for j in self.export.jobs if j['flags']['v'] == '7')
        self.page.select_option('select[name=version]', value='v7')
        self.count_text('%d jobs of %d' % (expected_v7, N_JOBS))
        badges = self.page.locator('ul.grid .card__row .badge--version').all_inner_texts()
        self.assertEqual(badges, ['V7'] * expected_v7)
        self.assertIn('version=v7', self.page.evaluate('location.hash'))
        self.page.select_option('select[name=version]', value='')
        self.page.select_option('select[name=kind]', value='variation')
        self.count_text('1 job of %d' % N_JOBS)
        self.page.locator('.toolbar__clear').click()
        self.count_text('%d jobs' % N_JOBS)

    def test_job_detail_and_lineage_navigation(self):
        up, variation, grid = fx.gallery_job_id(4), fx.gallery_job_id(5), fx.gallery_job_id(6)
        self.go('#/x/%s/gallery?kind=upscale' % self.export_id)
        self.count_text('2 jobs of %d' % N_JOBS)
        self.page.locator('ul.grid a.card__main[href*="/job/%s"]' % up).click()
        self.wait_job(up)
        row = self.job_row(up)
        self.assertEqual(self.page.locator('.code__pre').first.inner_text(), row['full_command'])
        self.assertEqual(self.page.locator('.shots .shot img').count(), 1)
        details = self.page.locator('.job-side .kv').first.inner_text()
        self.assertIn('%d × %d px' % (row['width'], row['height']), details)
        self.assertIn('Image 3 of the parent (index 2)', details)
        self.assertEqual(self.page.locator('.table--flags tbody tr').count(), 1)  # --v
        trail = self.page.locator('.trail .mini__meta').all_inner_texts()
        self.assertEqual([t.split(' · ')[0] for t in trail], ['Grid', 'Variation', 'Upscale'])

        # Up to the parent through the details link; the child is listed there.
        self.page.locator('.job-side .kv a.job-link').first.click()
        self.wait_job(variation)
        self.assertEqual(self.page.locator('.mini-list a.mini[href*="/job/%s"]' % up).count(), 1)
        # Up to the root through the lineage trail; scrolling to its child loads the child's thumbnail.
        self.page.locator('.trail a.mini[href*="/job/%s"]' % grid).click()
        self.wait_job(grid)
        child = self.page.locator('.mini-list a.mini[href*="/job/%s"]' % variation)
        child.scroll_into_view_if_needed()
        self.page.wait_for_function(
            "id => { const i = document.querySelector('.mini-list a.mini[href*=\"/job/' + id + '\"] img');"
            " return i && i.complete && i.naturalWidth > 0; }", arg=variation, timeout=15000)
        self.assertIn('A root job', self.page.locator('.lineage-facts').inner_text())
        # Browser Back retraces the path; the child link goes forward again.
        self.page.go_back()
        self.wait_job(variation)
        self.page.go_back()
        self.wait_job(up)
        self.page.go_forward()
        self.wait_job(variation)
        self.page.locator('.mini-list a.mini[href*="/job/%s"]' % up).click()
        self.wait_job(up)

    def test_a_dot_id_is_never_shown_as_another_route(self):
        """Browsers fold '.' path segments, so /jobs/. reaches the job list: the page must say not found (F8)."""
        export = fx.standard_export()
        export.jobs[0]['job_id'] = '.'
        export.boards[0]['id'] = '.'
        path = fx.write_zip(export, os.path.join(self._tmp.name, 'dots.zip'))
        dots = self.app.registry.open(path)[0].id
        for hash_, title in (('#/x/%s/job/.' % dots, 'Job not found'), ('#/x/%s/moodboard/.' % dots, 'Moodboard not found')):
            with self.subTest(hash=hash_):
                self.go(hash_)
                self.page.wait_for_function("t => (document.querySelector('.page-title') || {}).textContent === t",
                                            arg=title, timeout=15000)
                self.assertEqual(self.page.locator('.state--error').count(), 1)

    def test_moodboard_page_keeps_references_and_generations_apart(self):
        self.go('#/x/%s/moodboard/gboard-1' % self.export_id)
        self.page.wait_for_selector('.section--gen .grid__item', timeout=15000)
        self.assertEqual(self.page.locator('.page-title').inner_text(), 'Synthetic coast board')
        references = self.page.locator('.section--ref ul.refs > li.ref')
        self.assertEqual(references.count(), 5)
        self.assertEqual(references.first.locator('.ref__label').text_content(), 'Reference 1')
        generations = self.page.locator('.section--gen li.grid__item')
        self.assertEqual(generations.count(), 3)
        hrefs = [generations.nth(i).locator('a.card__main').get_attribute('href') for i in range(3)]
        self.assertEqual([h.rsplit('/', 1)[-1] for h in hrefs], [fx.gallery_job_id(i) for i in range(3)])
        self.assertEqual(self.page.locator('.section--gen .gen-group__head .link-chip').all_inner_texts(), ['Exporter link'])
        self.assertEqual(set(self.page.locator('.section--gen .card__link .link-chip').all_inner_texts()), {'Exporter link'})
        # Every reference image loads once scrolled to, and none is shown among the generations.
        self.page.locator('.section--gen').scroll_into_view_if_needed()
        self.page.wait_for_function("() => [...document.querySelectorAll('.section--ref img')]"
                                    ".every((i) => i.complete && i.naturalWidth > 0)", timeout=15000)
        ref_src = set(self.page.evaluate("[...document.querySelectorAll('.section--ref img')].map((i) => i.src)"))
        gen_src = set(self.page.evaluate("[...document.querySelectorAll('.section--gen img')]"
                                         ".map((i) => i.src || i.dataset.src)"))
        self.assertEqual(len(ref_src), 5)
        self.assertFalse(ref_src & gen_src)
        # A board without linked jobs says so instead of showing an empty grid.
        self.go('#/x/%s/moodboard/gboard-2' % self.export_id)
        self.page.wait_for_selector('text=No linked generations in this export', timeout=15000)
        self.assertEqual(self.page.locator('.section--ref ul.refs > li.ref').count(), 3)


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class HostedBrowserTests(unittest.TestCase):
    """Sign in through (fake) Google, open a published export, browse it, sign out."""

    N = 40

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-hosted-')
        cls.blob = FakeBlob(os.path.join(cls._tmp.name, 'blob'))
        cls.google = FakeGoogle(email='alice@example.com', hd='example.com')
        cls.export = fx.gallery_export(cls.N)
        cls.entry = cls.blob.put_export(fx.export_zip_bytes(cls.export, root='mj_export_e2e_20260430'),
                                        name='mj_export_e2e_20260430.zip', label='Synthetic team export')
        cls.other = cls.blob.put_export(fx.export_zip_bytes(fx.standard_export()), name='mj_export_second.zip')
        cls.hosted = Hosted(cls._tmp.name, cls.blob, cls.google)
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:  # Chromium not installed for Playwright
            cls._stop()
            cls.playwright.stop()
            raise unittest.SkipTest('Chromium for Playwright is unavailable: %s' % str(exc).splitlines()[0])

    @classmethod
    def _stop(cls):
        cls.hosted.stop()
        cls.blob.stop()
        cls.google.stop()
        cls._tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls._stop()

    def setUp(self):
        self.google.reset()
        self.context = self.browser.new_context(viewport={'width': WIDTH, 'height': HEIGHT})
        self.page = self.context.new_page()
        self.console, self.requests = [], []
        self.page.on('console', lambda m: self.console.append(m.text) if m.type == 'error' else None)
        self.page.on('pageerror', lambda e: self.console.append(str(e)))
        self.page.on('request', lambda r: self.requests.append(r.url))
        self.origin = self.hosted.origin

    def tearDown(self):
        self.context.close()
        self.assertEqual([m for m in self.console if 'net::ERR_NETWORK_CHANGED' not in m
                          and 'status of 401' not in m and 'status of 409' not in m], [])
        hosts = {urllib.parse.urlsplit(u).netloc for u in self.requests if not u.startswith(('data:', 'blob:'))}
        self.assertTrue(hosts <= {urllib.parse.urlsplit(self.origin).netloc,
                                  urllib.parse.urlsplit(self.google.origin).netloc}, hosts)

    def authorize_count(self):
        return sum(1 for u in self.requests if u.startswith(self.google.origin + '/o/oauth2/v2/auth'))

    def wait_hash(self, part):
        self.page.wait_for_function('p => location.hash.includes(p)', arg=part, timeout=30000)

    def wait_count(self, count, what=None):
        what = what or self.authorize_count
        for _ in range(300):
            if what() >= count:
                break
            self.page.wait_for_timeout(100)
        self.assertEqual(what(), count)

    def test_single_export_sign_in_browse_and_sign_out(self):
        self.blob.write_catalog([self.entry])
        # Signing in comes back to the app, which opens the only export and shows its overview.
        self.page.goto(self.origin + '/')
        self.wait_count(1)
        self.wait_hash('/x/%s/overview' % self.entry['id'])
        self.page.wait_for_selector('.stats', timeout=30000)
        self.assertEqual(self.page.locator('.page-title').inner_text(), 'Synthetic team export')
        self.page.wait_for_function("() => !document.getElementById('account').hidden && document.querySelector("
                                    "'#account .account__out').title === 'Signed in as alice@example.com'", timeout=15000)
        self.assertEqual(self.page.locator('.dropzone, #open-path, .library').count(), 0)
        self.assertIn('Published ZIP', self.page.locator('.overview__side').inner_text())
        # Gallery, then a job with its image.
        self.page.locator('#export-tabs a.tab', has_text='Gallery').click()
        self.page.wait_for_function("t => (document.querySelector('.toolbar__count') || {}).textContent === t",
                                    arg='%d jobs' % self.N, timeout=15000)
        first = self.page.locator('ul.grid a.card__main').first
        job_id = first.get_attribute('href').rsplit('/', 1)[-1]
        first.click()
        self.wait_hash('/job/' + job_id)
        self.page.wait_for_function("() => [...document.querySelectorAll('.shots img')].some((i) => i.complete && i.naturalWidth > 0)",
                                    timeout=15000)
        # The session ends (cookies gone): the next API call signs in again and returns to the same view.
        self.context.clear_cookies()
        self.page.evaluate("() => { location.hash = '#/x/%s/moodboards'; }" % self.entry['id'])
        self.wait_count(2)
        self.page.wait_for_selector('article.bcard', timeout=30000)
        self.assertIn('/x/%s/moodboards' % self.entry['id'], self.page.evaluate('location.hash'))
        self.assertEqual(self.page.locator('article.bcard').count(), 2)
        # Sign out: the signed-out page, then the API refuses and Sign in starts at Google again.
        self.page.wait_for_selector('#account .account__out', timeout=15000)
        self.page.locator('#account .account__out').click()
        self.page.wait_for_selector('text=Signed out', timeout=15000)
        self.assertEqual(self.page.request.get(self.origin + '/api/exports').status, 401)
        self.assertEqual(self.page.locator('a.btn', has_text='Sign in').get_attribute('href'), '/auth/login')
        self.google.approve = False
        self.page.locator('a.btn', has_text='Sign in').click()
        self.page.wait_for_selector('text=Sign-in cancelled', timeout=15000)
        self.assertEqual(self.authorize_count(), 3)
        self.google.approve = True
        self.page.locator('a.btn', has_text='Sign in again').click()
        self.wait_hash('/overview')
        self.page.wait_for_selector('.stats', timeout=30000)

    def test_start_page_lists_and_opens_a_published_export(self):
        self.blob.write_catalog([self.entry, self.other])
        self.blob.faults[self.other['blob']] = 'slow'
        self.blob.chunk, self.blob.slow_delay = 16384, 0.02
        try:
            self.page.goto(self.origin + '/')
            self.page.wait_for_selector('.xcard--catalog', timeout=30000)
            cards = self.page.locator('.xcard--catalog')
            self.assertEqual(cards.count(), 2)
            self.assertEqual(self.page.locator('.page-title').inner_text(), 'Exports')
            second = cards.filter(has_text='mj_export_second')
            second.locator('button', has_text='Open').click()
            self.page.wait_for_selector('.xcard--catalog [role=progressbar]', timeout=15000)
            self.wait_hash('/x/%s/overview' % self.other['id'])
            self.page.wait_for_selector('.stats', timeout=30000)
            self.page.go_back()
            link = self.page.locator('.xcard--catalog .xcard__link', has_text='mj_export_second')
            link.wait_for(timeout=15000)
            self.assertEqual(link.get_attribute('href'), '#/x/%s/overview' % self.other['id'])
        finally:
            self.blob.faults.clear()

    def test_a_shared_link_comes_back_to_its_view_through_the_account_chooser(self):
        """Google's account chooser is a page the user clicks through: only the address the browser itself
        hands to /auth/login keeps the #view of a shared link (E2E-2)."""
        self.blob.write_catalog([self.entry])
        self.google.chooser = True
        self.page.goto(self.origin + '/#/x/%s/moodboards' % self.entry['id'])
        self.page.wait_for_selector('#account', timeout=15000)
        self.page.locator('#account').click()
        self.wait_hash('/x/%s/moodboards' % self.entry['id'])
        self.page.wait_for_selector('article.bcard', timeout=30000)
        self.assertEqual(self.page.locator('article.bcard').count(), 2)
        self.assertEqual(self.authorize_count(), 1)

    def test_a_failure_is_shown_once_behind_two_instances_without_affinity(self):
        """Two instances share a cache behind a front end that alternates strictly. A published ZIP whose bytes
        do not match its SHA-256 fails once per request for it: the card shows the error and nothing downloads
        it again until Try again (E2E-1, E2E-6)."""
        lying = self.blob.put_export(fx.export_zip_bytes(fx.standard_export()), name='mj_export_lying.zip',
                                     sha256=hashlib.sha256(b'not these bytes').hexdigest())
        self.blob.write_catalog([self.entry, lying])
        front = RoundRobin()
        base = tempfile.mkdtemp(prefix='rr-', dir=self._tmp.name)
        instances = [Hosted(base, self.blob, self.google, env={'MJV_PUBLIC_URL': front.origin}) for _ in range(2)]
        front.ports = [h.port for h in instances]
        self.origin = front.origin  # tearDown checks that requests go to the front end and Google only
        try:
            self.page.goto(front.origin + '/')
            self.page.wait_for_selector('.xcard--catalog', timeout=30000)
            card = self.page.locator('.xcard--catalog').filter(has_text='mj_export_lying')
            card.locator('button', has_text='Open').click()
            error = 'Could not open: the downloaded ZIP does not match the SHA-256 in the catalog'
            card.locator('.xcard__state-text.is-err', has_text=error).wait_for(timeout=30000)
            time.sleep(3)  # longer than the UI's pause between automatic open requests
            self.assertEqual(self.blob.count(lying['blob']), 1)
            self.assertEqual(front.count('POST', '/api/catalog/%s/open' % lying['id']), 1)
            # Try again is a request to start again: one more download, the same error with a single prefix.
            card.locator('button', has_text='Try again').click()
            self.page.wait_for_function('n => document.querySelectorAll(".xcard--catalog .is-err").length === n',
                                        arg=1, timeout=30000)
            card.locator('.xcard__state-text.is-err', has_text=error).wait_for(timeout=30000)
            self.assertEqual(card.locator('.xcard__state-text.is-err').inner_text(), error)
            time.sleep(3)
            self.assertEqual(self.blob.count(lying['blob']), 2)
        finally:
            for hosted in instances:
                hosted.stop()
            front.stop()


if __name__ == '__main__':
    unittest.main()
