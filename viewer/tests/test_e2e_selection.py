"""End-to-end test of how the real UI shows the designer's selection, in Chromium (opt-in).

Run from viewer/:   MJV_E2E=1 python3 -m unittest tests.test_e2e_selection -v

Like tests/test_e2e_browser.py, it needs Playwright for Python and its Chromium build
(pip install playwright && python3 -m playwright install chromium) and is skipped
without MJV_E2E=1, without Playwright, or when Chromium cannot start. The server
listens on 127.0.0.1 only, the browser aborts every request that goes anywhere
else, and the data is tests/fixtures.selection_export(): invented jobs in which
the designer left moodboards, generations and one parent job out.
"""
import os
import tempfile
import threading
import unittest
import urllib.parse

from mjviewer.server import ViewerApp, make_server
from tests import fixtures as fx

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None

ENABLED = os.environ.get('MJV_E2E') == '1'
WIDTH, HEIGHT = 1280, 800
SENTENCE = ('2 of 4 moodboards and 10 of 25 generations. 13 generations and 2 moodboards were left out; '
            '2 generations appeared after choosing and were not exported.')
NOT_EXPORTED = "made with this moodboard are not in this export because of the designer's choice: left out, or made after choosing."


def odd_export():
    """selection_export() with two things the UI must not take at face value.

    board-2 has no generation in the export, and two that were not exported. left_out_moodboard_ids also
    lists board-1, which is exported, and board-unused, which no exported job references.
    """
    export = fx.selection_export()
    export.boards[1].update(generated_job_ids=[], generated_left_out=2)
    export.jobs[3]['personalization_codes'] = []
    export.jobs[3]['flags']['profile'] = []
    export.meta['selection']['left_out_moodboard_ids'] = ['board-left', 'board-1', 'board-unused']
    return export.sync_counts()


@unittest.skipUnless(ENABLED, 'set MJV_E2E=1 to run the browser test')
@unittest.skipUnless(sync_playwright is not None, 'Playwright for Python is not installed')
class SelectionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-e2e-selection-')
        path = fx.write_zip(fx.selection_export(), os.path.join(cls._tmp.name, 'chosen.zip'))
        other = fx.write_zip(odd_export(), os.path.join(cls._tmp.name, 'chosen-odd.zip'))
        cls.app = ViewerApp(cache_dir=os.path.join(cls._tmp.name, 'cache'))
        cls.export_id = cls.app.registry.open(path)[0].id
        cls.other_id = cls.app.registry.open(other)[0].id
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
        self.blocked = []
        local = urllib.parse.urlsplit(self.base).netloc

        def only_local(route):
            if urllib.parse.urlsplit(route.request.url).netloc == local:
                route.continue_()
            else:
                self.blocked.append(route.request.url)
                route.abort()

        self.context.route('**/*', only_local)
        self.page = self.context.new_page()
        self.console = []
        self.page.on('console', lambda m: self.console.append(m.text) if m.type == 'error' else None)
        self.page.on('pageerror', lambda e: self.console.append(str(e)))
        self.page.goto(self.base + '/')
        self.page.wait_for_selector('.page-title', timeout=15000)

    def tearDown(self):
        self.context.close()
        # No script errors or CSP refusals, and nothing was asked of any host but the viewer.
        self.assertEqual([m for m in self.console if 'net::ERR_NETWORK_CHANGED' not in m], [])
        self.assertEqual(self.blocked, [])

    def go(self, hash_):
        self.page.evaluate('h => { location.hash = h; }', hash_)

    def wait_title(self, title):
        self.page.wait_for_function("t => (document.querySelector('.page-title') || {}).textContent === t",
                                    arg=title, timeout=15000)

    def test_overview_says_what_the_designer_chose(self):
        self.go('#/x/%s/overview' % self.export_id)
        check = self.page.locator('.check', has_text='The designer chose what to export')
        check.wait_for(timeout=15000)
        self.assertIn('check--info', check.get_attribute('class'))
        self.assertEqual(check.locator('.check__detail').inner_text(), SENTENCE)
        titles = self.page.locator('.check--info .check__title').all_inner_texts()
        self.assertIn('1 personalization reference(s) point to moodboards the designer left out', titles)
        self.assertIn('1 job(s) have a parent the designer left out', titles)
        self.assertEqual(self.page.locator('.check--warning, .check--error').count(), 0)
        self.assertEqual(self.page.locator('.callout--warn').count(), 0)  # no version notice: 0.4.0 is supported
        details = self.page.locator('.overview__side .kv').inner_text()
        self.assertIn('Chosen by the designer', details)
        self.assertIn('0.4.0', details)
        table = self.page.locator('section', has_text='What the designer chose to export').locator('tbody tr')
        self.assertEqual([' '.join(r.split()) for r in table.all_inner_texts()],
                         ['Moodboards 4 2 2 —', 'Generations 25 10 13 2'])
        notes = self.page.locator('.selection-notes p')
        self.assertEqual(notes.nth(1).inner_text().strip(), '1 left-out moodboard was used by exported generations. '
                         'Job pages label those references Left out by the designer.')
        self.assertEqual(self.page.locator('.selection-notes .link-chip').inner_text(), 'Left out by the designer')
        gaps = self.page.locator('section', has_text='Parent gaps').locator('tbody tr')
        self.assertEqual(gaps.count(), 2)
        self.assertIn('Left out by the designer', gaps.nth(1).inner_text())
        # Left-out items are not unresolved relationships.
        unresolved = self.page.locator('section', has_text='Unresolved relationships').inner_text()
        self.assertNotIn('job-left-parent', unresolved)
        self.assertNotIn('board-left', unresolved)

    def test_job_pages_label_a_left_out_moodboard_and_parent(self):
        self.go('#/x/%s/job/job-niji' % self.export_id)
        self.wait_title('paper lanterns')
        references = self.page.locator('section', has_text='Personalization references').locator('tbody tr')
        self.assertEqual(references.count(), 1)
        self.assertEqual(references.locator('.link-chip').inner_text(), 'Left out by the designer')
        self.assertIn('A moodboard left out of this export', references.inner_text())
        made_with = self.page.locator('.relations .relation')
        self.assertEqual(made_with.count(), 1)
        self.assertIn('board-left', made_with.inner_text())
        self.assertEqual(made_with.locator('.link-chip').inner_text(), 'Left out by the designer')

        self.go('#/x/%s/job/job-left-child' % self.export_id)
        self.wait_title('lantern detail')
        facts = self.page.locator('.lineage-facts .callout')
        self.assertIn('callout--info', facts.get_attribute('class'))
        self.assertEqual(facts.inner_text().strip(),
                         'The designer left the parent job job-left-parent out of this export, so the chain stops here.')
        self.assertEqual(self.page.locator('.trail .mini--left-out .mini__title').inner_text(), 'Parent left out')
        self.assertIn('Left out by the designer', self.page.locator('.job-side .kv').first.inner_text())

        # An ordinary gap keeps its warning.
        self.go('#/x/%s/job/job-gap' % self.export_id)
        self.wait_title('orphan detail')
        self.assertIn('callout--warn', self.page.locator('.lineage-facts .callout').get_attribute('class'))

        # In the gallery, the card says the parent was left out, without a warning colour.
        self.go('#/x/%s/gallery?kind=upscale' % self.export_id)
        card = self.page.locator('ul.grid article.card', has=self.page.locator('a[href*="/job/job-left-child"]'))
        card.wait_for(timeout=15000)
        hint = card.locator('.card__lineage .hint')
        self.assertEqual(hint.inner_text().strip(), 'Parent left out')
        self.assertNotIn('hint--warn', hint.get_attribute('class'))

    def test_moodboards_count_the_generations_left_out(self):
        self.go('#/x/%s/moodboards' % self.export_id)
        self.page.wait_for_selector('article.bcard', timeout=15000)
        first = self.page.locator('article.bcard', has_text='Synthetic board one')
        # One number counts generations left out and generations made after choosing, so the words cover both.
        self.assertEqual(first.locator('.bcard__stat--gen .bcard__stat-note').inner_text(), '3 not exported')
        self.assertEqual(self.page.locator('article.bcard', has_text='Synthetic board two')
                         .locator('.bcard__stat--gen .bcard__stat-note').count(), 0)

        self.go('#/x/%s/moodboard/board-1' % self.export_id)
        self.wait_title('Synthetic board one')
        self.page.wait_for_selector('.section--gen .grid__item', timeout=15000)
        self.assertEqual(self.page.locator('.section--gen .section__actions').inner_text().strip(), '3 more not exported')
        self.assertEqual(self.page.locator('.callout .notes li').first.inner_text(), '3 more generations ' + NOT_EXPORTED)

        self.go('#/x/%s/moodboard/board-2' % self.export_id)
        self.wait_title('Synthetic board two')
        self.page.wait_for_selector('.section--gen', timeout=15000)
        self.assertEqual(self.page.locator('.section--gen .section__actions').count(), 0)
        self.assertNotIn('left out', self.page.locator('.callout .notes').inner_text())

    def test_the_overview_counts_the_left_out_moodboards_in_use_not_the_listed_ids(self):
        self.go('#/x/%s/overview' % self.other_id)
        notes = self.page.locator('.selection-notes p')
        notes.first.wait_for(timeout=15000)
        # Three ids are listed: board-1 is in the export and nothing uses board-unused.
        self.assertIn('1 left-out moodboard was used by exported generations.', notes.all_inner_texts()[-1])

    def test_a_board_without_exported_generations_keeps_the_scope_caveat(self):
        self.go('#/x/%s/moodboard/board-2' % self.other_id)
        self.wait_title('Synthetic board two')
        empty = self.page.locator('.section--gen .state')
        empty.wait_for(timeout=15000)
        self.assertEqual(empty.locator('.state__title').inner_text(), 'No linked generations in this export')
        self.assertEqual([p.strip() for p in empty.locator('.state__body p').all_inner_texts()], [
            'No exported job references this board. That does not prove the board was never used: jobs outside the '
            'export scope are not included, and older jobs may not record the reference.',
            '2 generations ' + NOT_EXPORTED.replace("'", '’')])
        self.assertEqual(self.page.locator('.section--gen .section__actions').count(), 0)


if __name__ == '__main__':
    unittest.main()
