"""Hidden items (hosted mode): the closure of a hidden list over every export root of a ZIP, and the visible index
that leaves it out while it loads. Synthetic data only (tests/fixtures.py: hiding_export and friends)."""
import json
import os
import tempfile
import unittest

from mjviewer.model import (EMPTY_CLOSURE, HIDDEN_META_COUNTS, UNAVAILABLE, ExportIndex, hidden_closure,
                            hidden_listing, hidden_note, hide_preview)
from mjviewer.server import dump_json
from mjviewer.sources import open_source
from tests import fixtures as fx

CID = '0123456789abcdef'
MARKER, COPY = fx.MARKER.encode(), fx.COPY.encode()


def open_roots(path):
    """Every export root of a ZIP or folder, as the hosted mode names them (the catalog id, -<n> per root)."""
    source = open_source(path)
    names = source.roots()
    info = {'type': 'catalog', 'name': os.path.basename(path), 'path': None}
    return source, [ExportIndex(source.view(root), CID if len(names) == 1 else '%s-%d' % (CID, n), info)
                    for n, root in enumerate(names, 1)]


def write(tmp, name, data):
    path = os.path.join(tmp, name)
    with open(path, 'wb') as handle:
        handle.write(data)
    return path


def hide(roots, jobs=fx.HIDE['jobs'], boards=fx.HIDE['moodboards']):
    closure = hidden_closure(roots, jobs, boards)
    return closure, [root.without(root.hidden_view(closure)) for root in roots]


def record(jobs=(), boards=(), counts=None):
    """A hidden record as catalog.py normalizes it: {id: {hidden_at, hidden_by}} per kind."""
    def who(items):
        return {item_id: {'hidden_at': at, 'hidden_by': by} for item_id, at, by in items}
    return {'jobs': who(jobs), 'moodboards': who(boards), 'counts': counts, 'key': 'k'}


def words(roots, closure):
    """Every word of every hidden job's prompt."""
    return sorted({word for root in roots for job_id in closure.jobs if job_id in root.jobs
                   for word in root.jobs[job_id].prompt.split()})


def queries(full, closure, roots):
    """Every search, filter and page a visitor could ask for, from what the full index knows."""
    facets = full.overview()['facets']
    out = [{'limit': '500'}, {'limit': '500', 'sort': 'oldest'}, {'limit': '1', 'offset': '3'},
           {'limit': '500', 'q': fx.MARKER}, {'limit': '500', 'links': 'explicit', 'moodboard': 'board-1'}]
    out += [{'limit': '500', 'q': word} for word in words(roots, closure)]
    out += [{'limit': '500', 'kind': kind['value']} for kind in facets['kinds']]
    out += [{'limit': '500', 'version': version['value']} for version in facets['versions']]
    out += [{'limit': '500', 'moodboard': board['id']} for board in facets['moodboards']]
    out += [{'limit': '500', 'profile': profile['id']} for profile in facets['profiles']]
    out += [{'limit': '500', 'profile': 'code:' + code['code']} for job in full.jobs.values() for code in job.codes]
    out += [{'limit': '500', 'date_from': facets['date_min'], 'date_to': facets['date_min']},
            {'limit': '500', 'date_from': facets['date_min'], 'date_to': facets['date_max']}]
    return out


def answers(index, full, closure, roots) -> bytes:
    """Everything the API can answer from an index, as the bytes it would send, for every id the full index
    knows (the ids asked for are not part of the answers)."""
    files = []
    for file_id in full.file_ids:
        rec = index.file(file_id)
        files.append(None if rec is None else [file_id, rec.rel, rec.status])
    return dump_json({
        'summary': index.summary, 'overview': index.overview(), 'moodboards': index.moodboards(),
        'profiles': index.profiles_list(), 'likes': index.likes_list(),
        'queries': [index.query_jobs(query) for query in queries(full, closure, roots)],
        'jobs': [index.job_detail(job_id) for job_id in full.jobs],
        'boards': [index.moodboard_detail(board_id) for board_id in full.boards],
        'files': files})


def problems(index):
    return {check['id']: check for check in index.checks if check['level'] in ('warning', 'error')}


class HidingTestCase(unittest.TestCase):
    """hiding_export() as a folder and as a ZIP (with an encrypted member named for a hidden job), and the
    two-root ZIP, each with HIDE applied."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-hidden-')
        tmp = cls._tmp.name
        cls.sources = []

        def load(path):
            source, roots = open_roots(path)
            cls.sources.append(source)
            closure, visible = hide(roots)
            return roots, closure, visible

        cls.folder = load(fx.write_folder(fx.hiding_export(), os.path.join(tmp, 'folder')))
        cls.single = load(write(tmp, 'hiding.zip', fx.hiding_zip_bytes()))
        cls.multi = load(write(tmp, 'two.zip', fx.two_root_hiding_zip()))
        cls.full, cls.closure, (cls.visible,) = cls.single[0][0], cls.single[1], cls.single[2]

    @classmethod
    def tearDownClass(cls):
        for source in cls.sources:
            source.close()
        cls._tmp.cleanup()

    def cases(self):
        """(name, full roots, closure, visible roots) for every fixture."""
        return [('folder',) + self.folder, ('zip',) + self.single, ('two roots',) + self.multi]


class ClosureTests(HidingTestCase):
    def test_made_with_a_moodboard_is_exactly_what_its_page_lists(self):
        tmp = self._tmp.name
        exports = {'standard': fx.standard_export(), 'hiding': fx.hiding_export(), 'gallery': fx.gallery_export(40)}
        for name, export in exports.items():
            source, (full,) = open_roots(fx.write_folder(export, os.path.join(tmp, 'page-' + name)))
            try:
                for board_id in full.boards:
                    with self.subTest(export=name, board=board_id):
                        closure = hidden_closure([full], [], [board_id])
                        listed = {item['job']['job_id'] for item in full.moodboard_detail(board_id)['generations']}
                        self.assertEqual({job_id for job_id in closure.jobs if job_id in full.jobs}, listed)
                        kinds = {item['link'] for item in full.moodboard_detail(board_id)['generations']}
                        if name == 'hiding' and board_id == 'board-zq':  # all three link kinds
                            self.assertEqual(kinds, {'exporter', 'derived', 'inferred_code'})
            finally:
                source.close()

    def test_a_zip_with_two_roots_hides_the_same_ids_in_both(self):
        roots, closure, (first, second) = self.multi
        self.assertIn('job-zq-r2', closure.jobs)  # it references board-zq, which its root left out (rule c)
        self.assertIn('job-zq-r2', roots[1].jobs)
        self.assertNotIn('job-zq-r2', second.jobs)
        self.assertEqual(sorted(second.jobs), ['job-r2-child', 'job-r2-plain'])
        child = second.job_detail('job-r2-child')
        self.assertEqual((child['job']['parent_id'], child['job']['parent_status'], child['lineage']['parent']),
                         (None, 'unavailable', {'id': None, 'status': 'unavailable', 'index': 3}))
        overview = second.overview()
        self.assertEqual((overview['parent_gaps'], overview['unresolved']['parent_gaps']), ([], []))
        self.assertEqual(overview['selection']['left_out_moodboard_ids'], [])
        self.assertEqual(overview['selection']['left_out_moodboards_used'], 0)
        self.assertNotIn('references_left_out', {check['id'] for check in second.checks})
        self.assertIn('references_left_out', {check['id'] for check in roots[1].checks})
        self.assertNotIn(MARKER, dump_json(overview).lower())
        for full, visible in zip(roots, (first, second)):
            self.assertLessEqual(set(problems(visible)), set(problems(full)))
        # Counted over both roots: the preview of hiding the moodboard and the Hidden listing.
        self.assertEqual(hide_preview(roots, None, 'moodboard', 'board-zq'), {'generations': 5, 'made_from_them': 1})
        self.assertEqual(hide_preview(roots, None, 'job', 'job-zq-solo'), {'made_from_it': 3})
        listing = hidden_listing(roots, closure, record(boards=[('board-zq', None, None)]))
        self.assertEqual((listing['moodboards'][0]['generations'], listing['moodboards'][0]['export_id']),
                         (5, CID + '-1'))
        # The record names ids, so a job of one root is hidden in the other one too.
        self.assertEqual(sum(visible.hidden.present['jobs'] for visible in (first, second)), 6)

    def test_a_generation_hidden_on_its_own_stays_hidden_when_its_moodboard_returns(self):
        roots = self.single[0]
        both = hidden_closure(roots, ['job-zq-exp'], ['board-zq'])
        alone = hidden_closure(roots, ['job-zq-exp'], [])
        self.assertIn('job-zq-exp', alone.jobs)
        self.assertNotIn('job-zq-derived', alone.jobs)
        self.assertEqual(alone.jobs, frozenset({'job-zq-exp'}))
        self.assertNotEqual(alone.key, both.key)
        # Hiding a job that a hidden moodboard already hides changes the list, not what is left out.
        self.assertEqual(hidden_closure(roots, [], ['board-zq']).key, both.key)
        self.assertEqual(EMPTY_CLOSURE.jobs, frozenset())


class VisibleIndexTests(HidingTestCase):
    def test_the_visible_index_never_mentions_a_hidden_item(self):
        for name, roots, closure, visible in self.cases():
            for full, index in zip(roots, visible):
                with self.subTest(fixture=name, root=index.id):
                    body = answers(index, full, closure, roots).lower()
                    self.assertNotIn(MARKER, body)
                    if COPY in body:  # only job-child-up's own copy of its parent's prompt
                        self.assertIn(b'job-child-up', body)
                    self.assertNotIn(COPY, body.replace(fx.COPY_PROMPT.encode(), b''))
                    # The full index does mention them: the test would notice a leak.
                    self.assertIn(MARKER, answers(full, full, closure, roots).lower())
        # A search for any word of a hidden prompt finds only visible jobs (a word may also be in their prompts).
        for word in words(self.single[0], self.closure):
            found = [card['job_id'] for card in self.visible.query_jobs({'q': word, 'limit': '500'})['items']]
            self.assertTrue(set(found) <= set(self.visible.jobs), word)
            if fx.MARKER in word:
                self.assertEqual(found, [], word)
        self.assertEqual([card['job_id'] for card in self.visible.query_jobs({'q': 'qxsolo'})['items']],
                         ['job-child-up'])

    def test_children_keep_their_own_copy_of_the_prompt(self):
        """The documented outcome of keeping what was made from a hidden generation (D6)."""
        for job_id, prompt, kind, index in (('job-child-up', fx.COPY_PROMPT, 'upscale', 2),
                                            ('job-child-v', 'paper boat remix', 'remix', 1)):
            with self.subTest(job=job_id):
                card = self.visible.card(job_id)
                detail = self.visible.job_detail(job_id)
                self.assertEqual((card['prompt'], card['kind'], card['parent_id'], card['parent_status']),
                                 (prompt, kind, None, 'unavailable'))
                self.assertEqual((detail['job']['prompt'], detail['raw']['prompt'], detail['raw']['parent_id']),
                                 (prompt, prompt, None))
                self.assertEqual(detail['lineage']['parent'], {'id': None, 'status': 'unavailable', 'index': index})
                self.assertIs(detail['raw_withheld'], True)
        # A parent_grid that is not an integer could hold the id: it is withheld too; an integer one is kept.
        self.assertEqual(self.full.job_detail('job-child-v')['raw']['parent_grid'], 'zqgrid-1')
        self.assertIsNone(self.visible.job_detail('job-child-v')['raw']['parent_grid'])
        self.assertIsNone(self.visible.job_detail('job-child-v')['job']['parent_grid'])
        self.assertEqual(self.visible.job_detail('job-child-up')['raw']['parent_grid'], 2)

    def test_a_child_of_a_hidden_generation_shows_a_neutral_gap(self):
        for job_id in ('job-child-up', 'job-child-v', 'job-exp-var'):
            with self.subTest(job=job_id):
                self.assertEqual(self.full.card(job_id)['parent_status'], 'in_export')
                self.assertEqual(self.visible.card(job_id)['kind'], self.full.card(job_id)['kind'])
                self.assertEqual(self.visible.card(job_id)['parent_status'], 'unavailable')
                self.assertEqual(self.visible.job_detail(job_id)['lineage']['ancestors'], [])
        overview = self.visible.overview()
        gaps = {gap['job_id'] for gap in overview['unresolved']['parent_gaps']}
        self.assertEqual(gaps, {'job-gap'})
        self.assertEqual({gap['job_id'] for gap in overview['parent_gaps']}, {'job-gap'})
        self.assertNotIn('parent_gaps_stale', {check['id'] for check in self.visible.checks})
        # A visible parent loses its hidden children: counts are of visible jobs.
        self.assertEqual(self.full.card('job-grid-a')['child_count'], self.visible.card('job-grid-a')['child_count'])
        self.assertEqual(self.full.job_detail('job-zq-solo')['lineage']['descendant_count'], 2)

    def test_a_short_link_picture_that_led_to_a_hidden_generation_is_not_available(self):
        export = fx.hiding_export()
        export.meta['extension_version'] = '0.4.1'
        rel = 'images/moodboard_board-1_3.png'
        export.boards[0]['images'].append({'source_url': fx.SHORT_LINKS[0], 'file': rel, 'width': 4, 'height': 4,
                                           'final_url': 'https://cdn.example.invalid/job-zq-solo/0_1.png'})
        export.sync_counts()
        source, (full,) = open_roots(fx.write_folder(export, os.path.join(self._tmp.name, 'short-hidden')))
        self.sources.append(source)
        _, (visible,) = hide([full])
        self.assertEqual(full.moodboard_detail('board-1')['references'][3]['status'], 'present')
        ref = visible.moodboard_detail('board-1')['references'][3]
        self.assertEqual((ref['status'], ref['url'], ref['file'], ref['source_url']), ('unavailable', None, None, None))
        self.assertIsNone(visible.moodboard_detail('board-1')['raw']['images'][3])
        self.assertIn(rel, visible.hidden.files)
        self.assertIsNone(visible.file(full.files[rel].file_id))
        # Where a short link led decides only for the picture it names: the others on the board stay.
        self.assertEqual(visible.moodboard_detail('board-1')['references'][0]['status'], 'present')

    def test_a_short_link_picture_whose_address_names_a_hidden_generation_further_on_is_not_available(self):
        # Where a short link led can be on any host, and name its generation after the first path segment.
        export = fx.hiding_export()
        export.meta['extension_version'] = '0.4.1'
        # Synthetic job ids: the first is hidden, the second is not.
        hidden_id, shown_id = '1b2c3d4e-0000-4000-8000-000000000abc', '1b2c3d4e-0000-4000-8000-000000000def'
        export.jobs += [fx.make_job(hidden_id, '2026-03-08T10:00:00+00:00', 'zqled lighthouse'),
                        fx.make_job(shown_id, '2026-03-09T10:00:00+00:00', 'shown lighthouse')]
        finals = ['https://cdn.example.invalid/attachments/111/222/someone_a_prompt_%s.png' % hidden_id,
                  'https://cdn.example.invalid/a/%s/0_1.png' % hidden_id.upper(),
                  'https://cdn.example.invalid/a/b/%s/0_1.png' % hidden_id.replace('-', '%2D'),
                  'https://cdn.example.invalid/attachments/111/222/someone_a_prompt_%s.png' % shown_id]
        for n, final in enumerate(finals, 3):
            export.boards[0]['images'].append({'source_url': 'https://s.mj.run/AAAAAAAAAB%d' % n, 'width': 4,
                                               'height': 4, 'file': 'images/moodboard_board-1_%d.png' % n,
                                               'final_url': final})
        export.sync_counts()
        source, (full,) = open_roots(fx.write_folder(export, os.path.join(self._tmp.name, 'short-hidden-further')))
        self.sources.append(source)
        _, (visible,) = hide([full], jobs=fx.HIDE['jobs'] + [hidden_id])

        def statuses(index):
            return [ref['status'] for ref in index.moodboard_detail('board-1')['references'][3:]]
        self.assertEqual(statuses(full), ['present'] * 4)
        # The hidden job's id anywhere in the path, in either case or percent-encoded; another job's id is not it.
        self.assertEqual(statuses(visible), ['unavailable'] * 3 + ['present'])

    def test_a_moodboard_picture_of_a_hidden_generation_is_not_available(self):
        rel = 'images/moodboard_board-1_2.png'
        file_id = self.full.files[rel].file_id
        ref = self.visible.moodboard_detail('board-1')['references'][2]
        self.assertEqual(ref, {'index': 2, 'status': 'unavailable', 'url': None, 'thumb_url': None, 'width': None,
                               'height': None, 'file': None, 'source_url': None, 'reason': UNAVAILABLE})
        self.assertEqual(self.full.moodboard_detail('board-1')['references'][2]['status'], 'present')
        detail = self.visible.moodboard_detail('board-1')
        self.assertIsNone(detail['raw']['images'][2])
        self.assertEqual(len(detail['raw']['images']), 3)
        self.assertIs(detail['raw_withheld'], True)
        self.assertIsNone(self.visible.file(file_id))
        self.assertIsNotNone(self.full.file(file_id))
        self.assertNotIn(rel, self.visible.overview()['files']['unreferenced'])
        listed = self.visible.moodboards()['items'][0]
        self.assertEqual((listed['reference_count'], listed['references_present'], len(listed['cover'])), (3, 2, 2))
        # Only a hidden item uses it, so it counts among the hidden pictures (and their bytes).
        self.assertIn(rel, self.visible.hidden.files)
        self.assertEqual(self.visible.hidden.present['images'], 18)
        # Unknown fields of the withheld entry are not this board's any more.
        self.assertNotIn('images[].zq_pin_note', dump_json(self.visible.overview()['checks']).decode())

    def test_hidden_ids_answer_like_unknown_ones(self):
        for name, roots, closure, visible in self.cases():
            for full, index in zip(roots, visible):
                with self.subTest(fixture=name, root=index.id):
                    for job_id in closure.jobs:
                        self.assertIsNone(index.job_detail(job_id))
                        self.assertIsNone(index.card(job_id))
                    for board_id in closure.moodboards:
                        self.assertIsNone(index.moodboard_detail(board_id))
                    hidden_files = [full.files[rel] for rel in index.hidden.files]
                    self.assertTrue(hidden_files)  # every root of these fixtures has pictures only hidden jobs use
                    for rec in hidden_files:
                        self.assertIsNone(index.file(rec.file_id), rec.rel)
                    for rec in full.files.values():
                        if rec.rel not in index.hidden.files and rec.file_id:
                            self.assertEqual(index.file(rec.file_id).rel, rec.rel)
        # A file a hidden job shares with a visible moodboard stays served.
        source, roots = open_roots(fx.write_folder(fx.shared_file_export(), os.path.join(self._tmp.name, 'shared')))
        try:
            closure, (visible,) = hide(roots, ['job-zq-sharer'], [])
            rel = 'images/moodboard_board-2_0.png'
            self.assertEqual(visible.hidden.files, frozenset())
            self.assertEqual(visible.file(roots[0].files[rel].file_id).rel, rel)
            self.assertIsNone(visible.job_detail('job-zq-sharer'))
            self.assertEqual(visible.hidden.present, {'jobs': 1, 'moodboards': 0, 'images': 0})
        finally:
            source.close()

    def test_a_visible_moodboard_drops_hidden_generations_quietly(self):
        detail = self.visible.moodboard_detail('board-1')
        self.assertEqual(self.full.moodboard_detail('board-1')['raw']['generated_job_ids'],
                         ['job-grid-a', 'job-zq-solo', 'job-zq-shared'])
        self.assertEqual(detail['raw']['generated_job_ids'], ['job-grid-a'])
        self.assertEqual([item['job']['job_id'] for item in detail['generations']], ['job-grid-a'])
        self.assertEqual(detail['unresolved_generated_job_ids'], [])
        self.assertEqual(self.visible.moodboards()['items'][0]['generations'],
                         {'exporter': 1, 'derived': 0, 'inferred_code': 0})
        self.assertNotIn('not in this export', ' '.join(detail['notes']))
        self.assertEqual(self.visible.overview()['unresolved']['generated_job_ids'], [])
        # The full index lists job-zq-gone as not in the export; the visible one never names it.
        self.assertIn({'board_id': 'board-zq', 'job_id': 'job-zq-gone'},
                      self.full.overview()['unresolved']['generated_job_ids'])

    def test_files_named_for_hidden_items_are_left_out(self):
        for name, roots, closure, visible in self.cases():
            for full, index in zip(roots, visible):
                with self.subTest(fixture=name, root=index.id):
                    files = index.overview()['files']
                    self.assertNotIn(MARKER, dump_json(files).lower())
                    self.assertEqual(files['missing'], [])
        full_files = self.full.overview()['files']
        self.assertEqual(full_files['unreferenced'], ['images/job-zq-solo_3.jpeg.crdownload',
                                                      'images/job-zq-solo_7.png', 'images/moodboard_board-zq_1.png.png',
                                                      'images/moodboard_board-zq_9.png'])
        self.assertEqual([item['path'] for item in full_files['unreadable']], [fx.ENCRYPTED_HIDDEN])
        self.assertEqual(self.visible.overview()['files'], {'missing': [], 'unreferenced': [], 'unreadable': []})
        self.assertEqual(self.visible.overview()['counts']['observed']['images_unreferenced'], 0)

    def test_export_meta_is_redacted(self):
        meta = self.visible.overview()['meta']
        self.assertEqual([gap['job_id'] for gap in meta['parent_gaps']], ['job-gap'])
        self.assertEqual(meta['failures'], [{'kind': 'job', 'id': 'job-grid-d', 'reason': 'Synthetic failure'}])
        self.assertEqual(self.visible.overview()['failures'], meta['failures'])
        self.assertEqual(meta['selection']['left_out_moodboard_ids'], ['board-gone'])
        self.assertEqual((meta['selection']['jobs'], meta['selection']['moodboards']),
                         ({'available': 16, 'exported': 12, 'left_out': 3, 'not_reviewed': 1},
                          {'available': 3, 'exported': 2, 'left_out': 1}))
        self.assertEqual(self.visible.selection_reduced, {'jobs': 5, 'moodboards': 1})
        # The full index keeps what was written.
        written = self.full.overview()['meta']
        self.assertEqual(len(written['failures']), 4)
        self.assertEqual(written['selection']['jobs']['available'], 21)
        self.assertEqual((self.visible.meta['username'], self.visible.meta['scope']),
                         (written['username'], written['scope']))
        second = self.multi[2][1].overview()['meta']
        self.assertEqual((second['parent_gaps'], second['selection']['left_out_moodboard_ids']), ([], []))
        self.assertEqual(second['selection']['jobs'], {'available': 4, 'exported': 2, 'left_out': 2,
                                                       'not_reviewed': 0})

    def test_without_hidden_items_nothing_changes(self):
        """An index built with nothing hidden answers exactly as the full one, byte for byte; local mode
        (hidden=None) never has the new keys or statuses."""
        for name, roots, closure, visible in self.cases():
            for full in roots:
                with self.subTest(fixture=name, root=full.id):
                    same = full.without(full.hidden_view(EMPTY_CLOSURE))
                    self.assertEqual(answers(same, full, closure, roots), answers(full, full, closure, roots))
        body = answers(self.full, self.full, self.closure, self.single[0])
        for key in (b'"raw_withheld"', b'"unavailable"', b'"hide_preview"', b'"hidden"'):
            self.assertNotIn(key, body)
        source, (standard,) = open_roots(fx.write_folder(fx.standard_export(), os.path.join(self._tmp.name, 'std')))
        try:
            self.assertIsNone(standard.hidden)
            self.assertEqual(standard.selection_reduced, {'jobs': 0, 'moodboards': 0})
        finally:
            source.close()


class ChecksAndCountsTests(HidingTestCase):
    def assert_no_new_problem(self, full, visible):
        """Every warning or error of the visible index is one the full index raises, about things it lists."""
        before = problems(full)
        for check_id, check in problems(visible).items():
            self.assertIn(check_id, before)
            if check_id in ('counts', 'selection_invalid'):  # their items carry the visible numbers
                where = {(item.get('file'), item.get('field')) for item in before[check_id]['items']}
                for item in check['items']:
                    self.assertIn((item.get('file'), item.get('field')), where)
            else:
                for item in check['items']:
                    self.assertIn(item, before[check_id]['items'])

    def test_hiding_adds_no_warning_or_error(self):
        for name, roots, closure, visible in self.cases():
            for full, index in zip(roots, visible):
                with self.subTest(fixture=name, root=index.id):
                    self.assert_no_new_problem(full, index)
        self.assertIn('generated_job_ids_omitted', problems(self.full))
        self.assertNotIn('generated_job_ids_omitted', problems(self.visible))
        checks = {check['id']: check['level'] for check in self.visible.checks}
        self.assertEqual(checks['generated_job_ids'], 'ok')  # an ok check may appear
        self.assertEqual(checks['counts'], 'ok')
        source, roots = open_roots(fx.write_folder(fx.unreadable_selection_variant(),
                                                   os.path.join(self._tmp.name, 'unreadable')))
        try:
            closure, (visible,) = hide(roots)
            self.assert_no_new_problem(roots[0], visible)
            full_items = problems(roots[0])['selection_invalid']['items']
            self.assertEqual(problems(visible)['selection_invalid']['items'], full_items)
            self.assertEqual([item['field'] for item in full_items], ['selection.jobs.left_out'])
            self.assertEqual(visible.boards['board-2'].generated_left_out, 17)  # above 21 - 5, within 21
        finally:
            source.close()

    def test_counts_read_as_if_the_items_were_never_exported(self):
        hidden, full, visible = self.visible.hidden, self.full, self.visible
        self.assertEqual(hidden.present, {'jobs': 5, 'moodboards': 1, 'images': 18})
        before, after = full.summary['counts'], visible.summary['counts']
        self.assertEqual(after, {'jobs': before['jobs'] - 5, 'moodboards': before['moodboards'] - 1,
                                 'profiles': before['profiles'], 'likes': before['likes'] - 1,
                                 'images_expected': before['images_expected'] - len(hidden.files),
                                 'images_present': before['images_present'] - 18})
        written, shown = full.overview()['counts']['meta'], visible.overview()['counts']['meta']
        for key in HIDDEN_META_COUNTS:
            self.assertEqual(shown[key], written[key] - hidden.delta[key], key)
        self.assertEqual(hidden.delta['completed_jobs'], 4)  # job-zq-inferred is a video: never counted as done
        self.assertEqual(shown['profiles'], written['profiles'])
        self.assertEqual({check['id']: check['level'] for check in visible.checks}['counts'], 'ok')
        self.assertEqual(visible.overview()['counts']['observed']['jobs'], 12)
        selection = {check['id']: check for check in visible.checks}['selection']['detail']
        self.assertTrue(selection.startswith('2 of 3 moodboards and 12 of 16 generations.'), selection)
        self.assertIn('3 generations and 1 moodboard were left out', selection)
        # A pair with an unreadable number is left as written.
        export = fx.hiding_export()
        export.meta['selection']['moodboards']['exported'] = 'some'
        source, roots = open_roots(fx.write_folder(export, os.path.join(self._tmp.name, 'pair')))
        try:
            closure, (index,) = hide(roots)
            self.assertEqual(index.overview()['meta']['selection']['moodboards'],
                             {'available': 4, 'exported': 'some', 'left_out': 1})
            self.assertEqual(index.selection_reduced, {'jobs': 5, 'moodboards': 0})
        finally:
            source.close()
        # Facets: hidden kinds, versions, dates, moodboards and codes are gone, counts are of visible jobs.
        facets = visible.overview()['facets']
        self.assertIn('v5.2', [item['value'] for item in full.overview()['facets']['versions']])
        self.assertNotIn('v5.2', [item['value'] for item in facets['versions']])
        self.assertNotIn('zoom', [item['value'] for item in facets['kinds']])
        self.assertEqual((full.overview()['facets']['date_min'], facets['date_min']), ('2026-03-01', '2026-03-02'))
        self.assertEqual([(item['id'], item['count']) for item in facets['moodboards']],
                         [('board-1', 1), ('board-2', 1)])
        self.assertEqual([item['id'] for item in facets['profiles'] if item['unresolved']], ['code:ucode9'])


class ManagerViewTests(HidingTestCase):
    def test_hide_preview_counts_what_changes(self):
        roots = self.single[0]
        self.assertEqual(hide_preview(roots, None, 'moodboard', 'board-zq'), {'generations': 4, 'made_from_them': 1})
        self.assertEqual(hide_preview(roots, None, 'job', 'job-zq-solo'), {'made_from_it': 2})
        self.assertEqual(hide_preview(roots, None, 'job', 'job-child-up'), {'made_from_it': 0})
        self.assertEqual(hide_preview(roots, None, 'moodboard', 'board-2'), {'generations': 1, 'made_from_them': 0})
        # Against the current list: what is hidden already is not counted again.
        hidden = hidden_closure(roots, [], ['board-zq'])  # board-1 also lists job-zq-shared, hidden already
        self.assertEqual(hide_preview(roots, hidden, 'moodboard', 'board-1'), {'generations': 2, 'made_from_them': 4})
        self.assertEqual(hide_preview(roots, hidden, 'job', 'job-zq-solo'), {'made_from_it': 2})
        # Descendants are followed through hidden ones: job-up was made from job-var, made from job-grid-a.
        var = hidden_closure(roots, ['job-var'], [])
        self.assertEqual(hide_preview(roots, var, 'job', 'job-grid-a'), {'made_from_it': 1})
        self.assertEqual(hide_preview(roots, None, 'job', 'job-grid-a'), {'made_from_it': 2})

    def test_hidden_listing_and_notes(self):
        roots = self.single[0]
        listed = record(jobs=[('job-zq-solo', '2026-11-03T10:15:00Z', 'alice@example.com'),
                              ('job-zq-exp', '2026-11-03T10:30:00Z', 'carol@example.com'),
                              ('job-nowhere-zq', None, None), ('job-elsewhere-zq', None, None)],
                        boards=[('board-zq', '2026-11-03T10:20:00Z', 'alice@example.com'),
                                ('board-nowhere-zq', '2026-11-02T08:00:00Z', None)],
                        counts={'jobs': 6, 'moodboards': 1, 'images': 22})
        closure = hidden_closure(roots, listed['jobs'], listed['moodboards'])
        listing = hidden_listing(roots, closure, listed)
        self.assertEqual(listing['counts'], {'jobs': 6, 'moodboards': 1, 'images': 22})
        self.assertEqual([row['id'] for row in listing['jobs']],
                         ['job-zq-exp', 'job-zq-solo', 'job-elsewhere-zq', 'job-nowhere-zq'])
        self.assertEqual([row['id'] for row in listing['moodboards']], ['board-zq', 'board-nowhere-zq'])
        board, nowhere = listing['moodboards']
        self.assertEqual({key: board[key] for key in ('export_id', 'name', 'code', 'codes', 'reference_count',
                                                      'generations', 'hidden_at', 'hidden_by')},
                         {'export_id': CID, 'name': 'Zqboard', 'code': 'zqmb', 'codes': ['zqmb'], 'reference_count': 2,
                          'generations': 4, 'hidden_at': '2026-11-03T10:20:00Z', 'hidden_by': 'alice@example.com'})
        self.assertEqual([ref['status'] for ref in board['cover']], ['present', 'present'])
        self.assertEqual(nowhere, {'id': 'board-nowhere-zq', 'export_id': None, 'name': None, 'code': None, 'codes': [],
                                   'reference_count': 0, 'cover': [], 'generations': 0,
                                   'hidden_at': '2026-11-02T08:00:00Z', 'hidden_by': None})
        exp, solo, elsewhere, nowhere = listing['jobs']
        self.assertEqual((exp['export_id'], exp['job']['job_id'], exp['with_moodboards']),
                         (CID, 'job-zq-exp', [{'id': 'board-zq', 'name': 'Zqboard'}]))
        self.assertEqual((solo['with_moodboards'], solo['hidden_by']), ([], 'alice@example.com'))
        self.assertEqual(nowhere, {'id': 'job-nowhere-zq', 'export_id': None, 'job': None, 'hidden_at': None,
                                   'hidden_by': None, 'with_moodboards': []})
        # A job's card and pictures come from the full index, for its managers.
        self.assertEqual(solo['job']['images'][0]['status'], 'present')
        # Notes on a hidden item's own page.
        self.assertEqual(hidden_note(roots, closure, listed, 'job', 'job-zq-exp'),
                         {'on_its_own': {'hidden_at': '2026-11-03T10:30:00Z', 'hidden_by': 'carol@example.com'},
                          'with_moodboards': [{'id': 'board-zq', 'name': 'Zqboard', 'export_id': CID,
                                               'hidden_at': '2026-11-03T10:20:00Z', 'hidden_by': 'alice@example.com'}]})
        self.assertEqual(hidden_note(roots, closure, listed, 'job', 'job-zq-derived'),
                         {'on_its_own': None, 'with_moodboards': [
                             {'id': 'board-zq', 'name': 'Zqboard', 'export_id': CID,
                              'hidden_at': '2026-11-03T10:20:00Z', 'hidden_by': 'alice@example.com'}]})
        self.assertEqual(hidden_note(roots, closure, listed, 'moodboard', 'board-zq'),
                         {'hidden_at': '2026-11-03T10:20:00Z', 'hidden_by': 'alice@example.com', 'generations': 4,
                          'stay_hidden': 1})
        self.assertEqual(json.loads(dump_json(listing)), listing)  # plain JSON: no sets
        # A job hidden with a moodboard that its own root left out names the first root that has it, else none.
        two = self.multi[0]
        board = record(boards=[('board-zq', None, None)])
        note = hidden_note(two, hidden_closure(two, [], ['board-zq']), board, 'job', 'job-zq-r2')
        self.assertEqual([(b['id'], b['name'], b['export_id']) for b in note['with_moodboards']],
                         [('board-zq', 'Zqboard', CID + '-1')])
        alone = two[1:]
        note = hidden_note(alone, hidden_closure(alone, [], ['board-zq']), board, 'job', 'job-zq-r2')
        self.assertEqual([(b['id'], b['name'], b['export_id']) for b in note['with_moodboards']],
                         [('board-zq', None, None)])
        # Without a record the listing is empty.
        self.assertEqual(hidden_listing(roots, None, None), {'counts': None, 'moodboards': [], 'jobs': []})


if __name__ == '__main__':
    unittest.main()
