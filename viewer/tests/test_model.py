"""Parsing, normalisation, relationships, lineage, checks and job queries."""
import os
import tempfile
import time
import unittest
from unittest import mock

from mjviewer import model as model_module
from mjviewer.model import (ExportIndex, QueryError, compute_kind, compute_version, iso_utc,
                            parse_command, parse_date)
from mjviewer.sources import Limits, open_source
from tests import fixtures as fx


def build(export, tmp, limits=Limits(), root=fx.ROOT):
    folder = fx.write_folder(export, tmp, root=root)
    source = open_source(folder, limits)
    return ExportIndex(source.view(''), 'f' * 12, {'type': 'folder', 'name': 'x', 'path': folder}, limits)


def ids(result):
    return [card['job_id'] for card in result['items']]


def checks(index):
    return {c['id']: c['level'] for c in index.checks}


class StandardExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-model-')
        cls.export = fx.standard_export()
        cls.index = build(cls.export, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def jobs(self, **params):
        return self.index.query_jobs({k: str(v) for k, v in params.items()})

    def test_summary_counts(self):
        summary = self.index.summary
        self.assertEqual(summary['counts'], {'jobs': 9, 'moodboards': 2, 'profiles': 2, 'likes': 1,
                                             'images_expected': 33, 'images_present': 33})
        self.assertEqual(summary['version_support'], 'supported')
        self.assertEqual(summary['status'], 'complete')
        self.assertEqual(summary['export_time'], '2026-03-20T12:00:00.000Z')
        self.assertEqual((summary['warnings'], summary['errors']), (0, 0))

    def test_checks_of_a_clean_export(self):
        self.assertEqual(checks(self.index), {
            'extension_version': 'ok', 'status': 'ok', 'failures': 'ok', 'counts': 'ok', 'generated_job_ids': 'ok',
            'scope': 'info', 'unresolved_references': 'info', 'parent_gaps': 'info'})

    def test_overview_counts_and_files(self):
        overview = self.index.overview()
        self.assertEqual(overview['counts']['observed']['images_missing'], 0)
        self.assertEqual(overview['counts']['meta']['files'], 33)
        self.assertEqual(overview['files'], {'missing': [], 'unreferenced': [], 'unreadable': []})
        self.assertEqual(overview['limitations'], ['Likes cover the selected own jobs only.'])

    def test_facets(self):
        facets = self.index.facets
        self.assertEqual({k['value']: k['count'] for k in facets['kinds']}, {'grid': 6, 'upscale': 2, 'variation': 1})
        self.assertEqual([(v['value'], v['label'], v['count']) for v in facets['versions']],
                         [('v8.2', 'V8.2', 3), ('v8.1', 'V8.1', 2), ('v7', 'V7', 2), ('v6.1', 'V6.1', 1),
                          ('niji6', 'Niji 6', 1)])
        self.assertEqual([(m['id'], m['count']) for m in facets['moodboards']], [('board-1', 1), ('board-2', 1)])
        self.assertEqual([(p['id'], p['count'], p['unresolved']) for p in facets['profiles']],
                         [('profile-1', 2, False), (fx.USER_ID + '_7_main', 0, False), ('code:ucode9', 1, True)])
        self.assertEqual((facets['date_min'], facets['date_max']), ('2026-03-10', '2026-03-18'))

    def test_dates_are_normalised_to_utc(self):
        created = {j: self.index.jobs[j].created_iso for j in ('job-var', 'job-grid-d', 'job-niji', 'job-grid-a')}
        self.assertEqual(created, {'job-var': '2026-03-15T08:00:00.000Z', 'job-grid-d': '2026-03-13T08:00:00.000Z',
                                   'job-niji': '2026-03-13T20:00:00.000Z', 'job-grid-a': '2026-03-10T12:00:00.000Z'})
        self.assertEqual(self.index.profiles_list()['items'][0]['created_at'], '2026-01-01T00:00:00.000Z')
        self.assertIsNone(self.index.profiles_list()['items'][1]['created_at'])
        self.assertEqual(self.index.moodboards()['items'][0]['created_at'], '2026-02-01T10:00:00.000Z')

    def test_moodboard_links_and_references_stay_separate(self):
        detail = self.index.moodboard_detail('board-1')
        self.assertEqual([(g['job']['job_id'], g['link']) for g in detail['generations']], [('job-grid-a', 'exporter')])
        self.assertIn('generated_job_ids', detail['generations'][0]['evidence'])
        self.assertIn('also references the board id', detail['generations'][0]['evidence'])
        self.assertEqual([r['file'] for r in detail['references']],
                         ['images/moodboard_board-1_0.png', 'images/moodboard_board-1_1.png'])
        self.assertTrue(all(r['status'] == 'present' and r['url'] for r in detail['references']))
        generation_files = {i['file'] for g in detail['generations'] for i in g['job']['images']}
        self.assertFalse(generation_files & {r['file'] for r in detail['references']})
        board2 = self.index.moodboard_detail('board-2')
        self.assertEqual([(g['job']['job_id'], g['link']) for g in board2['generations']], [('job-grid-d', 'inferred_code')])
        self.assertTrue(board2['generations'][0]['evidence'].startswith('inferred'))
        items = {b['id']: b for b in self.index.moodboards()['items']}
        self.assertEqual(items['board-1']['generations'], {'exporter': 1, 'derived': 0, 'inferred_code': 0})
        self.assertEqual(items['board-2']['generations'], {'exporter': 0, 'derived': 0, 'inferred_code': 1})
        self.assertEqual((items['board-1']['reference_count'], items['board-1']['references_present']), (2, 2))
        self.assertEqual(len(items['board-1']['cover']), 2)
        card = self.index.card('job-grid-a')
        self.assertEqual(card['moodboards'], [{'id': 'board-1', 'name': 'Synthetic board one', 'link': 'exporter'}])

    def test_profile_links(self):
        result = self.index.profiles_list()
        by_id = {p['id']: p for p in result['items']}
        self.assertEqual([(j['job_id'], j['link']) for j in by_id['profile-1']['jobs']],
                         [('job-grid-b', 'explicit'), ('job-flag-only', 'inferred_code')])
        self.assertEqual(by_id[fx.USER_ID + '_7_main']['jobs'], [])
        self.assertEqual(by_id['profile-1']['ratings_count'], 42)
        self.assertEqual(result['unmatched_codes'], [{'code': 'ucode9', 'type': 'user', 'job_ids': ['job-grid-c']}])

    def test_unresolved_user_reference_is_kept_not_guessed(self):
        detail = self.index.job_detail('job-grid-c')
        self.assertEqual(len(detail['references']), 1)
        reference = detail['references'][0]
        self.assertEqual((reference['type'], reference['link'], reference['target']), ('user', 'unresolved', None))
        self.assertIn('without an id', reference['reason'])
        self.assertEqual(detail['moodboards'], [])
        self.assertEqual(detail['profiles'], [])
        self.assertEqual(detail['job']['codes'], [{'code': 'ucode9', 'type': 'user', 'resolved': False}])
        unresolved = self.index.overview()['unresolved']['references']
        self.assertEqual([(u['job_id'], u['code']) for u in unresolved], [('job-grid-c', 'ucode9')])

    def test_lineage_chain_children_and_gap(self):
        up = self.index.job_detail('job-up')['lineage']
        self.assertEqual([a['job_id'] for a in up['ancestors']], ['job-grid-a', 'job-var'])
        self.assertEqual(up['parent'], {'id': 'job-var', 'status': 'in_export', 'index': 1})
        self.assertEqual((up['children'], up['descendant_count'], up['cycle']), ([], 0, False))
        root = self.index.job_detail('job-grid-a')['lineage']
        self.assertEqual([(c['job_id'], c['parent_index']) for c in root['children']], [('job-var', 2)])
        self.assertEqual(root['descendant_count'], 2)
        self.assertEqual(root['parent'], {'id': None, 'status': 'none', 'index': None})
        self.assertEqual(self.index.card('job-var')['child_count'], 1)
        gap = self.index.job_detail('job-gap')['lineage']
        self.assertEqual((gap['parent']['status'], gap['ancestors']), ('missing', []))
        self.assertEqual(self.index.overview()['unresolved']['parent_gaps'],
                         [{'job_id': 'job-gap', 'parent_id': 'job-not-exported', 'reported_by_exporter': True,
                           'reason': 'the parent job is not in this export'}])

    def test_job_detail_fields(self):
        detail = self.index.job_detail('job-grid-b')
        job = detail['job']
        self.assertEqual(job['full_command'], 'blue whale under ice --v 7.0 --profile pcode1')
        self.assertEqual(job['version'], {'value': 'v7', 'label': 'V7', 'source': 'flag'})
        self.assertEqual((job['kind'], job['kind_source']), ('grid', 'export'))
        self.assertEqual(detail['raw'], self.export.jobs[1])
        self.assertEqual(detail['unknown_fields'], [])
        self.assertTrue(job['liked'])
        self.assertEqual(detail['likes'], [{'image_likes': [1], 'line': 1}])
        self.assertEqual([i['status'] for i in detail['images']], ['present'] * 4)
        self.assertTrue(detail['images'][0]['url'].startswith('/files/ffffffffffff/'))
        self.assertTrue(detail['images'][0]['thumb_url'].endswith('?size=thumb'))
        self.assertEqual(detail['images'][0]['source_url'], 'https://cdn.example.invalid/job-grid-b/0_0.jpeg')
        self.assertIsNone(self.index.job_detail('nope'))

    def test_likes(self):
        likes = self.index.likes_list()
        self.assertEqual([(i['job_id'], i['image_likes'], i['source']) for i in likes['items']],
                         [('job-grid-b', [1], 'likes.jsonl')])
        self.assertEqual([j for j, job in self.index.jobs.items() if job.liked], ['job-grid-b'])

    # ------------------------------------------------------------ filters

    def test_search_is_case_insensitive_and_needs_every_term(self):
        self.assertEqual(sorted(ids(self.jobs(q='red FOX'))), ['job-grid-a', 'job-up', 'job-var'])
        self.assertEqual(len(ids(self.jobs(q='fox'))), 4)
        self.assertEqual(ids(self.jobs(q='FOX  green')), ['job-grid-c'])
        self.assertEqual(ids(self.jobs(q='fox whale')), [])
        self.assertEqual(self.jobs(q='   ')['total'], 9)

    def test_filter_kind(self):
        self.assertEqual(sorted(ids(self.jobs(kind='upscale'))), ['job-gap', 'job-up'])
        self.assertEqual(ids(self.jobs(kind='zoom')), [])

    def test_filter_version(self):
        self.assertEqual(sorted(ids(self.jobs(version='v7'))), ['job-grid-b', 'job-grid-c'])  # '7.0' and '7'
        self.assertEqual(ids(self.jobs(version='v6.1')), ['job-grid-d'])  # from job_type
        self.assertEqual(self.index.card('job-grid-d')['version']['source'], 'job_type')
        self.assertEqual(ids(self.jobs(version='niji6')), ['job-niji'])

    def test_filter_moodboard_and_explicit_links(self):
        self.assertEqual(ids(self.jobs(moodboard='board-1')), ['job-grid-a'])
        self.assertEqual(ids(self.jobs(moodboard='board-2')), ['job-grid-d'])
        self.assertEqual(ids(self.jobs(moodboard='board-2', links='explicit')), [])
        self.assertEqual(ids(self.jobs(moodboard='board-1', links='explicit')), ['job-grid-a'])

    def test_filter_profile_and_unmatched_code(self):
        self.assertEqual(sorted(ids(self.jobs(profile='profile-1'))), ['job-flag-only', 'job-grid-b'])
        self.assertEqual(ids(self.jobs(profile='profile-1', links='explicit')), ['job-grid-b'])
        self.assertEqual(ids(self.jobs(profile='code:ucode9')), ['job-grid-c'])
        self.assertEqual(ids(self.jobs(profile='code:nothing')), [])

    def test_filter_date_range_is_inclusive_utc(self):
        self.assertEqual(ids(self.jobs(date_from='2026-03-12', date_to='2026-03-12')), ['job-grid-c'])
        self.assertEqual(sorted(ids(self.jobs(date_from='2026-03-13', date_to='2026-03-13'))), ['job-grid-d', 'job-niji'])
        self.assertEqual(self.jobs(date_from='2026-03-17')['total'], 2)
        self.assertEqual(self.jobs(date_to='2026-03-10')['total'], 1)

    def test_sort_and_paging(self):
        newest = ids(self.jobs())
        self.assertEqual(newest[0], 'job-flag-only')
        self.assertEqual(ids(self.jobs(sort='oldest')), list(reversed(newest)))
        page = self.jobs(limit=4, offset=4)
        self.assertEqual((page['total'], page['offset'], page['limit']), (9, 4, 4))
        self.assertEqual(ids(page), newest[4:8])
        self.assertEqual(ids(self.jobs(offset=8)), newest[8:])
        self.assertEqual(ids(self.jobs(offset=50)), [])

    def test_combined_filters(self):
        self.assertEqual(ids(self.jobs(q='fox', version='v8.2', kind='variation')), ['job-var'])

    def test_bad_parameters_are_rejected(self):
        for params in ({'limit': '0'}, {'limit': '501'}, {'offset': '-1'}, {'limit': 'ten'}, {'sort': 'random'},
                       {'links': 'some'}, {'date_from': '2026-13-01'}, {'date_to': '03/12/2026'}):
            with self.subTest(params=params):
                with self.assertRaises(QueryError):
                    self.index.query_jobs(params)


class LinkRuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mjv-links-')
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def links(self, index, board_id):
        return [(g['job']['job_id'], g['link']) for g in index.moodboard_detail(board_id)['generations']]

    def test_older_export_without_generated_job_ids_derives_links(self):
        export = fx.standard_export()
        export.meta['extension_version'] = '0.2.0'
        for board in export.boards:
            del board['generated_job_ids']
        index = build(export, self.tmp)
        self.assertEqual(self.links(index, 'board-1'), [('job-grid-a', 'derived')])
        self.assertEqual(self.links(index, 'board-2'), [('job-grid-d', 'inferred_code')])
        # 0.2.x is not a writer this viewer was checked against: best effort, one warning.
        self.assertEqual(index.summary['version_support'], 'unknown')
        self.assertEqual(checks(index)['generated_job_ids_absent'], 'info')
        self.assertEqual([c['id'] for c in index.checks if c['level'] == 'warning'], ['extension_version'])

    def test_reference_type_does_not_restrict_the_board_link(self):
        # lib/normalize.js links a board to every job whose reference id equals the board id, whatever the type.
        export = fx.standard_export()
        export.jobs[4]['personalization_codes'] = [fx.ref('profile', 'board-2', 'othercode')]
        for board in export.boards:
            del board['generated_job_ids']
        index = build(export, self.tmp)
        self.assertIn(('job-niji', 'derived'), self.links(index, 'board-2'))
        reference = index.job_detail('job-niji')['references'][0]
        self.assertEqual((reference['link'], reference['target']['kind']), ('explicit', 'moodboard'))

    def test_reference_id_shared_by_a_board_and_a_profile_links_both(self):
        export = fx.standard_export()
        export.profiles[0]['id'] = 'board-1'
        export.jobs[1]['personalization_codes'] = [fx.ref('profile', 'board-1', 'pcode1')]
        index = build(export, self.tmp)
        detail = index.job_detail('job-grid-b')
        self.assertEqual([(m['id'], m['link']) for m in detail['moodboards']], [('board-1', 'derived')])
        self.assertEqual([(p['id'], p['link']) for p in detail['profiles']], [('board-1', 'explicit')])
        reference = detail['references'][0]
        self.assertEqual(reference['target']['kind'], 'profile')
        self.assertIn('also equals a moodboard id', reference['reason'])
        self.assertEqual(checks(index)['generated_job_ids_omitted'], 'warning')

    def test_generated_job_ids_omitting_an_explicit_reference(self):
        export = fx.standard_export()
        export.boards[0]['generated_job_ids'] = []
        index = build(export, self.tmp)
        self.assertEqual(self.links(index, 'board-1'), [('job-grid-a', 'derived')])
        self.assertIn('omits', index.moodboard_detail('board-1')['generations'][0]['evidence'])
        self.assertEqual(checks(index)['generated_job_ids_omitted'], 'warning')

    def test_generated_job_ids_pointing_to_an_absent_job(self):
        export = fx.standard_export()
        export.boards[0]['generated_job_ids'] = ['job-grid-a', 'job-elsewhere']
        index = build(export, self.tmp)
        detail = index.moodboard_detail('board-1')
        self.assertEqual(self.links(index, 'board-1'), [('job-grid-a', 'exporter')])
        self.assertEqual(detail['unresolved_generated_job_ids'], ['job-elsewhere'])
        self.assertEqual(index.overview()['unresolved']['generated_job_ids'],
                         [{'board_id': 'board-1', 'job_id': 'job-elsewhere'}])
        self.assertEqual(checks(index)['generated_job_ids_unresolved'], 'warning')

    def test_exporter_list_wins_without_a_reference(self):
        export = fx.standard_export()
        export.boards[1]['generated_job_ids'] = ['job-niji']
        index = build(export, self.tmp)
        self.assertEqual(self.links(index, 'board-2'), [('job-niji', 'exporter'), ('job-grid-d', 'inferred_code')])

    def test_reference_with_unknown_id_but_known_code_is_inferred(self):
        export = fx.standard_export()
        export.jobs[4]['personalization_codes'] = [fx.ref('moodboard', 'board-deleted', 'mbcodeA')]
        index = build(export, self.tmp)
        reference = index.job_detail('job-niji')['references'][0]
        self.assertEqual(reference['link'], 'inferred_code')
        self.assertIn('matches no exported', reference['reason'])
        self.assertIn(('job-niji', 'inferred_code'), self.links(index, 'board-1'))

    def test_reference_matching_nothing_is_unresolved(self):
        export = fx.standard_export()
        export.jobs[4]['personalization_codes'] = [fx.ref('moodboard', 'board-gone', 'zzz')]
        index = build(export, self.tmp)
        reference = index.job_detail('job-niji')['references'][0]
        self.assertEqual((reference['link'], reference['target']), ('unresolved', None))
        self.assertIn({'code': 'zzz', 'type': 'moodboard', 'job_ids': ['job-niji']}, index.profiles_list()['unmatched_codes'])
        self.assertEqual(ids(index.query_jobs({'profile': 'code:zzz'})), ['job-niji'])

    def test_code_filter_matches_where_the_code_went_unmatched(self):
        """The unmatched-code facet, the profiles page and the code filter name the same jobs."""
        export = fx.standard_export()
        export.jobs[0]['personalization_codes'].append(fx.ref('moodboard', 'board-1', 'odd'))  # resolved by its id
        export.jobs[4]['personalization_codes'] = [fx.ref('user', None, 'odd')]                 # matches nothing
        index = build(export, self.tmp)
        self.assertEqual(ids(index.query_jobs({'profile': 'code:odd'})), ['job-niji'])
        facet = [f for f in index.facets['profiles'] if f['id'] == 'code:odd'][0]
        self.assertEqual((facet['count'], facet['unresolved']), (1, True))
        unmatched = [u for u in index.profiles_list()['unmatched_codes'] if u['code'] == 'odd'][0]
        self.assertEqual(unmatched['job_ids'], ['job-niji'])
        self.assertEqual(ids(index.query_jobs({'profile': 'code:mbcodeA'})), [])  # resolved everywhere

    def test_profile_flag_without_reference_is_unmatched_code(self):
        export = fx.standard_export()
        export.jobs[4]['flags']['profile'] = ['flagonly']
        index = build(export, self.tmp)
        detail = index.job_detail('job-niji')
        self.assertEqual([(r['source'], r['link']) for r in detail['references']], [('flag', 'unresolved')])
        self.assertEqual(detail['job']['codes'], [{'code': 'flagonly', 'type': 'flag', 'resolved': False}])


def outputs(index):
    """Everything the API serves for an export, minus the raw export_meta.json and the source path."""
    overview = dict(index.overview())
    overview.pop('meta')
    overview['summary'] = dict(overview['summary'], source=None)
    return {'overview': overview, 'jobs': {j: index.job_detail(j) for j in index.jobs},
            'boards': {b: index.moodboard_detail(b) for b in index.boards}, 'moodboards': index.moodboards(),
            'profiles': index.profiles_list(), 'likes': index.likes_list(), 'all': index.query_jobs({'limit': '500'})}


class SelectionTests(unittest.TestCase):
    """Exports in which the designer chose what to export (export_meta.selection), from fixtures.selection_export()."""

    SENTENCE = ('2 of 4 moodboards and 10 of 25 generations. 13 generations and 2 moodboards were left out; '
                '2 generations appeared after choosing and were not exported.')
    NOTE = ("3 more generations made with this moodboard are not in this export because of the designer's choice: "
            'left out, or made after choosing.')

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix='mjv-selection-')
        cls.index = build(fx.selection_export(), os.path.join(cls._tmp.name, 'shared'))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=self._tmp.name)

    def check(self, check_id, index=None):
        return [c for c in (index or self.index).checks if c['id'] == check_id][0]

    def test_the_overview_describes_the_selection_in_plain_words(self):
        check = self.check('selection')
        self.assertEqual((check['level'], check['title'], check['detail']),
                         ('info', 'The designer chose what to export', self.SENTENCE))
        self.assertEqual(self.index.overview()['selection'], {
            'chosen_at': '2026-03-19T09:00:00.000Z', 'moodboards': {'available': 4, 'exported': 2, 'left_out': 2},
            'jobs': {'available': 25, 'exported': 10, 'left_out': 13, 'not_reviewed': 2},
            'left_out_moodboard_ids': ['board-left'], 'left_out_moodboards_used': 1})
        # What the designer left out is reported, never as a warning or an error, under the version that writes it.
        self.assertEqual((self.index.summary['extension_version'], self.index.summary['version_support']),
                         ('0.4.0', 'supported'))
        self.assertEqual(checks(self.index), {
            'extension_version': 'ok', 'status': 'ok', 'failures': 'ok', 'counts': 'ok', 'generated_job_ids': 'ok',
            'scope': 'info', 'selection': 'info', 'unresolved_references': 'info', 'references_left_out': 'info',
            'parent_gaps': 'info', 'parent_gaps_left_out': 'info'})
        self.assertEqual((self.index.summary['warnings'], self.index.summary['errors']), (0, 0))
        self.assertEqual(self.index.overview()['limitations'][-1], fx.SELECTION_LIMITATION)

    def test_a_reference_to_a_left_out_moodboard_is_labelled_left_out(self):
        detail = self.index.job_detail('job-niji')
        self.assertEqual([(r['source'], r['link'], r['target'], r['reason']) for r in detail['references']],
                         [('reference', 'left_out', None, 'the designer left this moodboard out of the export')])
        self.assertEqual(detail['job']['codes'], [{'code': 'mbcodeL', 'type': 'moodboard', 'resolved': True}])
        self.assertEqual((detail['moodboards'], detail['profiles']), ([], []))
        # Not unresolved: neither listed as such nor offered as an unmatched code.
        overview = self.index.overview()
        self.assertEqual([(u['job_id'], u['code']) for u in overview['unresolved']['references']], [('job-grid-c', 'ucode9')])
        self.assertEqual([u['code'] for u in self.index.profiles_list()['unmatched_codes']], ['ucode9'])
        self.assertEqual([f['id'] for f in self.index.facets['profiles'] if f['unresolved']], ['code:ucode9'])
        self.assertEqual(ids(self.index.query_jobs({'profile': 'code:mbcodeL'})), [])
        self.assertEqual(self.check('references_left_out')['items'], [
            {'job_id': 'job-niji', 'type': 'moodboard', 'id': 'board-left', 'code': 'mbcodeL', 'source': 'reference',
             'reason': 'the designer left this moodboard out of the export'}])
        self.assertEqual(self.check('unresolved_references')['title'], '1 personalization reference(s) could not be linked')

    def test_a_left_out_parent_is_the_designers_choice_not_a_gap(self):
        card = self.index.card('job-left-child')
        self.assertEqual((card['parent_id'], card['parent_status']), ('job-left-parent', 'left_out'))
        lineage = self.index.job_detail('job-left-child')['lineage']
        self.assertEqual((lineage['parent'], lineage['ancestors']), ({'id': 'job-left-parent', 'status': 'left_out', 'index': 1}, []))
        self.assertEqual(self.check('parent_gaps_left_out')['items'], [
            {'job_id': 'job-left-child', 'parent_id': 'job-left-parent',
             'reason': 'the designer left the parent out of this export'}])
        # The ordinary gap stays one, and only it is unresolved.
        self.assertEqual(self.check('parent_gaps')['title'], '1 job(s) have a parent outside this export')
        self.assertEqual([g['job_id'] for g in self.index.overview()['unresolved']['parent_gaps']], ['job-gap'])
        self.assertEqual(self.index.card('job-gap')['parent_status'], 'missing')
        self.assertNotIn('parent_gaps_stale', checks(self.index))

    def test_moodboards_say_how_many_generations_were_left_out(self):
        items = {b['id']: b for b in self.index.moodboards()['items']}
        self.assertEqual((items['board-1']['generated_left_out'], items['board-2']['generated_left_out']), (3, 0))
        board = self.index.moodboard_detail('board-1')
        self.assertEqual(board['moodboard']['generated_left_out'], 3)
        self.assertEqual(board['notes'][0], self.NOTE)
        self.assertEqual([(g['job']['job_id'], g['link']) for g in board['generations']], [('job-grid-a', 'exporter')])
        other = self.index.moodboard_detail('board-2')
        self.assertEqual(other['moodboard']['generated_left_out'], 0)
        self.assertFalse([n for n in other['notes'] if 'left out' in n])

    def test_generations_made_after_choosing_are_not_called_left_out(self):
        # The designer kept everything, then made three more generations with board-1 before the export ran.
        # The exporter counts them in generated_left_out too, so the board must not say the designer left them out.
        export = fx.selection_export()
        export.meta['selection'].update(moodboards={'available': 2, 'exported': 2, 'left_out': 0},
                                        jobs={'available': 13, 'exported': 10, 'left_out': 0, 'not_reviewed': 3},
                                        left_out_moodboard_ids=[])
        export.jobs[4]['personalization_codes'] = []  # job-niji no longer uses a left-out moodboard
        export.jobs[4]['flags']['profile'] = []
        index = build(export, self.tmp)
        self.assertEqual(self.check('selection', index)['detail'],
                         '2 of 2 moodboards and 10 of 13 generations. Nothing was left out; '
                         '3 generations appeared after choosing and were not exported.')
        self.assertEqual(index.moodboards()['items'][0]['generated_left_out'], 3)
        notes = index.moodboard_detail('board-1')['notes']
        self.assertEqual(notes[0], self.NOTE)
        self.assertFalse([n for n in notes if 'left out by the designer' in n])
        self.assertEqual((index.summary['warnings'], index.summary['errors']), (0, 0))

    def test_wording_for_single_items_and_for_nothing_left_out(self):
        export = fx.selection_export()
        export.meta['selection'].update(moodboards={'available': 1, 'exported': 1, 'left_out': 0},
                                        jobs={'available': 12, 'exported': 10, 'left_out': 1, 'not_reviewed': 1},
                                        left_out_moodboard_ids=[])  # no moodboard was left out
        export.boards[0]['generated_left_out'] = 1  # each board misses one of the two generations not exported
        export.boards[1]['generated_left_out'] = 1
        export.boards[1]['generated_job_ids'] = []
        export.jobs[3]['personalization_codes'] = []  # board-2 then has no generation in the export
        export.jobs[3]['flags']['profile'] = []
        index = build(export, self.tmp)
        self.assertEqual(self.check('selection', index)['detail'],
                         '1 of 1 moodboard and 10 of 12 generations. 1 generation was left out; '
                         '1 generation appeared after choosing and was not exported.')
        self.assertEqual(index.moodboard_detail('board-2')['notes'][0],
                         "1 generation made with this moodboard is not in this export because of the designer's choice: "
                         'left out, or made after choosing.')
        self.assertNotIn('selection_invalid', checks(index))
        # Nothing left out: no generation is missing from a board, and the parent of job-left-child is an ordinary gap.
        export.meta['selection'].update(moodboards={'available': 1234, 'exported': 1234, 'left_out': 0},
                                        jobs={'available': 1203, 'exported': 1203, 'left_out': 0, 'not_reviewed': 0})
        export.boards[0]['generated_left_out'] = export.boards[1]['generated_left_out'] = 0
        del export.meta['parent_gaps'][-1]['left_out']
        index = build(export, os.path.join(self.tmp, 'again'))
        self.assertEqual(self.check('selection', index)['detail'],
                         '1,234 of 1,234 moodboards and 1,203 of 1,203 generations. Nothing was left out.')
        self.assertNotIn('selection_invalid', checks(index))

    def test_only_left_out_moodboards_that_references_use_are_counted(self):
        export = fx.selection_export()
        export.meta['selection']['left_out_moodboard_ids'] = ['board-left', 'board-unused']
        export.jobs[0]['personalization_codes'].append(fx.ref('moodboard', 'board-left', 'mbcodeL'))
        index = build(export, self.tmp)
        # Two jobs use board-left, and no exported job uses board-unused: one moodboard is in use.
        self.assertEqual(len(self.check('references_left_out', index)['items']), 2)
        self.assertEqual(index.overview()['selection']['left_out_moodboards_used'], 1)

    def test_a_selection_with_nothing_in_scope_says_the_export_is_empty(self):
        # A run that stops before it reads the feed writes a selection whose counts are all 0, and empty files.
        export = fx.selection_export()
        export.jobs, export.boards, export.profiles, export.likes = [], [], [], []
        export.meta.update(status='stopped', parent_gaps=[],
                           failures=[{'kind': 'run', 'id': 'export', 'reason': 'Run stopped'}])
        del export.meta['feed_exhausted']
        export.meta['selection'] = {
            'chosen_at': '2026-03-19T09:00:00.000Z', 'left_out_moodboard_ids': [],
            'moodboards': {'available': 0, 'exported': 0, 'left_out': 0},
            'jobs': {'available': 0, 'exported': 0, 'left_out': 0, 'not_reviewed': 0}}
        index = build(export.sync_counts(), self.tmp)
        check = self.check('selection', index)
        self.assertEqual((check['level'], check['detail']), ('info', ''))
        self.assertEqual(check['title'], 'The designer chose what to export, but this export has no moodboards or generations')
        self.assertNotIn('selection_invalid', checks(index))
        # Rows beside an all-zero record: the numbers as recorded, never 'Nothing was left out'.
        export = fx.selection_export()
        export.meta['selection'].update(moodboards={'available': 0, 'exported': 0, 'left_out': 0},
                                        jobs={'available': 0, 'exported': 0, 'left_out': 0, 'not_reviewed': 0})
        index = build(export, os.path.join(self.tmp, 'rows'))
        check = self.check('selection', index)
        self.assertEqual((check['title'], check['detail']),
                         ('The designer chose what to export', '0 of 0 moodboards and 0 of 0 generations.'))

    def test_the_moodboard_id_wins_over_a_matching_code(self):
        # The reference names the left-out board by id; board-1 sharing its code does not make it board-1's job.
        export = fx.selection_export()
        export.jobs[4]['personalization_codes'] = [fx.ref('moodboard', 'board-left', 'mbcodeA')]
        export.jobs[4]['flags']['profile'] = ['mbcodeA']
        index = build(export, self.tmp)
        self.assertEqual([r['link'] for r in index.job_detail('job-niji')['references']], ['left_out'])
        self.assertNotIn('job-niji', [g['job']['job_id'] for g in index.moodboard_detail('board-1')['generations']])

    def test_a_left_out_id_that_names_an_exported_record_keeps_its_link(self):
        export = fx.selection_export()
        export.meta['selection']['left_out_moodboard_ids'] = ['board-left', 'board-1', 'profile-1']
        index = build(export, self.tmp)
        # Only board-left is used by a reference labelled left out; the overview counts that, not the list.
        self.assertEqual(index.overview()['selection']['left_out_moodboard_ids'], ['board-left', 'board-1', 'profile-1'])
        self.assertEqual(index.overview()['selection']['left_out_moodboards_used'], 1)
        self.assertEqual(index.card('job-grid-a')['moodboards'],
                         [{'id': 'board-1', 'name': 'Synthetic board one', 'link': 'exporter'}])
        reference = index.job_detail('job-grid-b')['references'][0]
        self.assertEqual(reference['link'], 'explicit')
        self.assertIn('also equals the id of a moodboard the designer left out', reference['reason'])
        problem = self.check('selection_invalid', index)
        self.assertEqual(problem['level'], 'warning')
        self.assertEqual(problem['items'], [{
            'file': 'export_meta.json', 'field': 'selection.left_out_moodboard_ids', 'ids': ['board-1'],
            'problem': '1 id(s) name moodboards that are in this export; they are shown as exported'}])

    def test_unreadable_selection_values_are_a_warning_and_ignored(self):
        export = fx.selection_export()
        export.meta['selection'] = 'yes'
        index = build(export, self.tmp)
        self.assertEqual(self.check('selection_invalid', index)['items'],
                         [{'file': 'export_meta.json', 'field': 'selection', 'problem': 'is a string, not an object; ignored'}])
        self.assertNotIn('selection', checks(index))
        self.assertNotIn('selection', index.overview())
        self.assertEqual(index.job_detail('job-niji')['references'][0]['link'], 'unresolved')
        self.assertEqual(index.summary['warnings'], 1)

        export.meta['selection'] = {'chosen_at': 'soon', 'moodboards': [1], 'extra': True,
                                    'jobs': {'available': -1, 'exported': 2.5, 'left_out': True, 'not_reviewed': '2', 'more': 0},
                                    'left_out_moodboard_ids': ['board-left', 7, None, {}]}
        export.boards[0]['generated_left_out'] = 'three'
        export.boards[1]['generated_left_out'] = -1
        index = build(export, os.path.join(self.tmp, 'parts'))
        selection = index.overview()['selection']
        self.assertEqual(selection, {'chosen_at': None, 'moodboards': {'available': None, 'exported': None, 'left_out': None},
                                     'jobs': {'available': None, 'exported': None, 'left_out': None, 'not_reviewed': None},
                                     'left_out_moodboard_ids': ['board-left', '7'], 'left_out_moodboards_used': 1})
        self.assertEqual(self.check('selection', index)['detail'], 'export_meta.json records the choice, but not its numbers.')
        problems = self.check('selection_invalid', index)['items']
        self.assertEqual([(p['file'], p['field']) for p in problems], [
            ('export_meta.json', 'selection.chosen_at'), ('export_meta.json', 'selection.moodboards'),
            ('export_meta.json', 'selection.jobs.available'), ('export_meta.json', 'selection.jobs.exported'),
            ('export_meta.json', 'selection.jobs.left_out'), ('export_meta.json', 'selection.jobs.not_reviewed'),
            ('export_meta.json', 'selection.left_out_moodboard_ids'),
            ('moodboards.jsonl', 'generated_left_out'), ('moodboards.jsonl', 'generated_left_out')])
        self.assertEqual([p.get('id') for p in problems[-2:]], ['board-1', 'board-2'])
        self.assertIn('2 value(s) are not ids', problems[6]['problem'])
        self.assertEqual(index.moodboard_detail('board-1')['moodboard'].get('generated_left_out', 'absent'), 'absent')
        self.assertEqual(index.job_detail('job-niji')['references'][0]['link'], 'left_out')  # the readable ids still count
        unknown = {(i['file'], i['field']) for i in self.check('unknown_fields', index)['items']}
        self.assertEqual(unknown, {('export_meta.json', 'selection.extra'), ('export_meta.json', 'selection.jobs.more')})
        self.assertEqual((index.summary['warnings'], index.summary['errors']), (1, 0))

    def problems(self, index):
        return [(p['field'], p['problem']) for p in self.check('selection_invalid', index)['items']]

    def test_numbers_that_add_up_to_more_than_available_are_a_warning(self):
        export = fx.selection_export()
        export.meta['selection'].update(moodboards={'available': 1, 'exported': 2, 'left_out': 5},
                                        jobs={'available': 25, 'exported': 30, 'left_out': 13, 'not_reviewed': 2})
        index = build(export, self.tmp)
        self.assertEqual(self.problems(index), [
            ('selection.moodboards', 'exported and left out add up to 7, more than the 1 available; shown as recorded'),
            ('selection.jobs', 'exported, left out and not reviewed add up to 45, more than the 25 available; '
                               'shown as recorded')])
        self.assertEqual(self.check('selection_invalid', index)['level'], 'warning')
        # The numbers are still shown as recorded.
        self.assertEqual(self.check('selection', index)['detail'],
                         '2 of 1 moodboard and 30 of 25 generations. 13 generations and 5 moodboards were left out; '
                         '2 generations appeared after choosing and were not exported.')
        self.assertEqual(index.overview()['selection']['jobs']['exported'], 30)
        # One readable part is enough to contradict 'available'.
        export.meta['selection']['jobs'] = {'available': 25, 'exported': 30}
        index = build(export, os.path.join(self.tmp, 'one'))
        self.assertIn(('selection.jobs', 'exported is 30, more than the 25 available; shown as recorded'),
                      self.problems(index))

    def test_a_record_that_contradicts_the_files_never_says_nothing_was_left_out(self):
        # The numbers say nothing was left out, but a reference is to a left-out moodboard, board-1 misses three
        # generations and a parent is marked left out.
        export = fx.selection_export()
        export.meta['selection'].update(moodboards={'available': 2, 'exported': 2, 'left_out': 0},
                                        jobs={'available': 10, 'exported': 10, 'left_out': 0, 'not_reviewed': 0})
        index = build(export, self.tmp)
        self.assertEqual(self.check('selection', index)['detail'], '2 of 2 moodboards and 10 of 10 generations.')
        bound = 'more than the 0 generations not exported because of the selection (selection.jobs left_out + not_reviewed)'
        self.assertEqual(self.problems(index), [
            ('generated_left_out', 'is 3, %s; shown as recorded' % bound),
            ('selection.left_out_moodboard_ids', 'names 1 moodboard outside this export, more than the 0 left out '
                                                 '(selection.moodboards.left_out); shown as recorded'),
            ('parent_gaps', 'marks 1 parent left_out, %s; shown as left out' % bound)])
        self.assertEqual(self.check('selection_invalid', index)['items'][0]['id'], 'board-1')
        # What the files say is still shown.
        self.assertEqual(index.job_detail('job-niji')['references'][0]['link'], 'left_out')
        self.assertEqual(index.card('job-left-child')['parent_status'], 'left_out')
        self.assertEqual(index.moodboard_detail('board-1')['moodboard']['generated_left_out'], 3)
        self.assertEqual((index.summary['warnings'], index.summary['errors']), (1, 0))

    def test_a_board_count_above_the_generations_in_scope_is_a_warning(self):
        # Without readable left_out and not_reviewed numbers, the generations in scope are the limit.
        export = fx.selection_export()
        export.meta['selection']['jobs'] = {'available': 2, 'exported': 2}
        index = build(export, self.tmp)
        self.assertIn(('generated_left_out', 'is 3, more than the 2 generations in scope (selection.jobs.available); '
                                             'shown as recorded'), self.problems(index))

    def test_counts_above_2_53_are_unreadable_so_the_page_and_the_server_agree(self):
        export = fx.selection_export()
        export.meta['selection']['jobs'] = {'available': 2 ** 53, 'exported': 10 ** 30, 'left_out': int('9' * 400),
                                            'not_reviewed': 2 ** 53 + 1}
        export.boards[0]['generated_left_out'] = 10 ** 40
        index = build(export, self.tmp)
        self.assertEqual(index.overview()['selection']['jobs'],
                         {'available': 2 ** 53, 'exported': None, 'left_out': None, 'not_reviewed': None})
        too_large = 'too large to be a real count (above 2^53); ignored'
        self.assertEqual(self.problems(index), [('selection.jobs.exported', too_large), ('selection.jobs.left_out', too_large),
                                                ('selection.jobs.not_reviewed', too_large), ('generated_left_out', too_large)])
        self.assertEqual(self.check('selection', index)['detail'], '2 of 4 moodboards. 2 moodboards were left out.')
        self.assertNotIn('generated_left_out', index.moodboard_detail('board-1')['moodboard'])
        self.assertNotIn('generated_left_out', index.moodboards()['items'][0])
        self.assertFalse([n for n in index.moodboard_detail('board-1')['notes'] if 'not in this export' in n])

    def test_left_out_data_without_a_selection_is_a_warning(self):
        # The exporter writes generated_left_out and left_out gaps only with a selection.
        for name, selection in (('null', None), ('absent', fx.MISSING)):
            export = fx.standard_export()
            if selection is not fx.MISSING:
                export.meta['selection'] = selection
            export.boards[0]['generated_left_out'] = 4
            export.meta['parent_gaps'][0]['left_out'] = True
            index = build(export, os.path.join(self.tmp, name))
            self.assertIsNone(index.selection)
            self.assertEqual(self.problems(index), [
                ('generated_left_out', 'recorded, but export_meta.json records no selection; shown as recorded'),
                ('parent_gaps', 'marks 1 parent left_out, but export_meta.json records no selection; shown as left out')])
            # Shown as recorded.
            self.assertEqual(index.card('job-gap')['parent_status'], 'left_out')
            self.assertEqual(index.moodboards()['items'][0]['generated_left_out'], 4)
            self.assertEqual(index.summary['warnings'], 1)
        # Without export_meta.json (an interrupted run) nothing says whether the designer chose: no such warning.
        export.meta = None
        index = build(export, os.path.join(self.tmp, 'nometa'))
        self.assertNotIn('selection_invalid', checks(index))
        self.assertEqual(index.moodboards()['items'][0]['generated_left_out'], 4)

    def test_only_a_true_left_out_flag_marks_a_parent(self):
        export = fx.selection_export()
        export.meta['parent_gaps'][-1]['left_out'] = 'yes'
        export.meta['parent_gaps'][0]['left_out'] = 1
        index = build(export, self.tmp)
        self.assertEqual(index.card('job-left-child')['parent_status'], 'missing')
        self.assertNotIn('parent_gaps_left_out', checks(index))
        self.assertEqual([g['job_id'] for g in index.overview()['unresolved']['parent_gaps']], ['job-gap', 'job-left-child'])

    def test_a_null_selection_reads_exactly_like_an_export_without_one(self):
        # The exporter writes selection: null when the designer did not choose; older exporters write no key.
        plain = build(fx.standard_export(), os.path.join(self.tmp, 'plain'))
        export = fx.standard_export()
        export.meta['selection'] = None
        chosen_nothing = build(export, os.path.join(self.tmp, 'null'))
        self.assertEqual(outputs(chosen_nothing), outputs(plain))
        self.assertIsNone(chosen_nothing.selection)
        for index in (plain, chosen_nothing):
            self.assertNotIn('selection', index.overview())
            self.assertFalse([b for b in index.moodboards()['items'] if 'generated_left_out' in b])
            self.assertNotIn('generated_left_out', index.moodboard_detail('board-1')['moodboard'])


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='mjv-robust-')
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_malformed_lines_are_reported_with_line_numbers(self):
        export = fx.standard_export()
        good = fx.jsonl(export.jobs[:2]).splitlines()
        export.raw['jobs.jsonl'] = b'\n'.join([
            good[0], b'{"job_id": "broken", ', b'[1, 2, 3]', good[1], b'{"job_id": "nan-job", "width": NaN}',
            b'{"job_id": "bad-\xff-utf8"}', b'   ', b'{"prompt": "row without an id"}',
            b'{"job_id": "huge", "width": 1e999}', b'"just a string"']) + b'\n'
        index = build(export, self.tmp)
        self.assertEqual(list(index.jobs), ['job-grid-a', 'job-grid-b'])
        errors = {(e['file'], e['line']) for e in index.parse_errors + index.row_problems}
        self.assertEqual(errors, {('jobs.jsonl', n) for n in (2, 3, 5, 6, 8, 9, 10)})
        check = [c for c in index.checks if c['id'] == 'parse_errors'][0]
        self.assertEqual(check['level'], 'warning')
        self.assertEqual([i['line'] for i in check['items']], [2, 3, 5, 6, 8, 9, 10])
        self.assertTrue(all('nan' not in str(i['message']).lower() or 'non-finite' in i['message'] for i in check['items']))

    def test_empty_and_absent_files(self):
        export = fx.standard_export()
        export.likes = []
        export.jobs[1]['image_likes'] = []
        export.sync_counts()
        export.absent.add('likes.jsonl')
        export.raw['profiles.jsonl'] = b''
        export.raw['moodboards.jsonl'] = b'\n\n'
        index = build(export, self.tmp)
        states = {name: s['state'] for name, s in index.file_states.items()}
        self.assertEqual((states['likes.jsonl'], states['profiles.jsonl'], states['moodboards.jsonl']), ('absent', 'empty', 'empty'))
        self.assertEqual(index.summary['counts']['jobs'], 9)
        self.assertEqual(index.summary['counts']['profiles'], 0)
        levels = checks(index)
        self.assertEqual(levels['file:likes.jsonl'], 'info')
        self.assertEqual(levels['file:moodboards.jsonl'], 'warning')  # export_meta reports 2
        self.assertEqual(levels['counts'], 'warning')
        self.assertEqual(index.likes_list()['items'], [])

    def test_unknown_fields_are_listed_and_kept(self):
        export = fx.standard_export()
        export.jobs[0]['new_field'] = {'nested': True}
        export.jobs[0]['flags']['newflag'] = '1'
        export.jobs[0]['personalization_codes'][0]['weight'] = 2
        export.boards[0]['colour'] = 'teal'
        export.boards[0]['images'][0]['caption'] = 'x'
        export.profiles[0]['extra'] = 1
        export.meta['future'] = True
        index = build(export, self.tmp)
        detail = index.job_detail('job-grid-a')
        self.assertEqual(detail['unknown_fields'], ['flags.newflag', 'new_field', 'personalization_codes[].weight'])
        self.assertEqual(detail['raw']['new_field'], {'nested': True})
        self.assertEqual(index.moodboard_detail('board-1')['unknown_fields'], ['colour', 'images[].caption'])
        self.assertEqual(index.profiles_list()['items'][0]['unknown_fields'], ['extra'])
        items = [c for c in index.checks if c['id'] == 'unknown_fields'][0]['items']
        self.assertIn({'file': 'export_meta.json', 'field': 'future', 'rows': 1}, items)
        self.assertEqual(index.summary['warnings'], 0)

    def test_unknown_and_missing_extension_version(self):
        export = fx.standard_export()
        export.meta['extension_version'] = '9.9.0'
        index = build(export, self.tmp)
        self.assertEqual(index.summary['version_support'], 'unknown')
        self.assertEqual(checks(index)['extension_version'], 'warning')
        self.assertEqual([c['detail'] for c in index.checks if c['id'] == 'extension_version'],
                         ['This viewer was checked against exports from versions 0.3.0, 0.3.1 and 0.4.0; '
                          '9.9.0 was read with best effort.'])
        self.assertEqual(index.summary['counts']['jobs'], 9)
        del export.meta['extension_version']
        index = build(export, tempfile.mkdtemp(dir=self.tmp))
        self.assertEqual(index.summary['version_support'], 'missing')
        self.assertEqual(checks(index)['extension_version'], 'warning')

    def test_absent_or_invalid_export_meta(self):
        export = fx.standard_export()
        export.meta = None
        index = build(export, self.tmp)
        self.assertIsNone(index.summary['status'])
        self.assertEqual(index.summary['version_support'], 'missing')
        # Written last by the exporter: its absence means an interrupted run. The data is still shown.
        self.assertEqual(checks(index)['export_meta'], 'error')
        self.assertIn('interrupted', [c for c in index.checks if c['id'] == 'export_meta'][0]['detail'])
        self.assertEqual(index.summary['counts']['jobs'], 9)
        self.assertNotIn('feed', checks(index))
        # Nothing to cross-check the parent gap with: unknown, not "unreported".
        self.assertEqual(checks(index)['parent_gaps'], 'info')
        self.assertIsNone(index.overview()['unresolved']['parent_gaps'][0]['reported_by_exporter'])
        export.raw['export_meta.json'] = b'[1, 2]'
        index = build(export, tempfile.mkdtemp(dir=self.tmp))
        self.assertEqual(checks(index)['export_meta'], 'error')
        self.assertEqual(index.summary['errors'], 1)

    def test_supported_versions(self):
        for version, support in (('0.3.0', 'supported'), ('0.3.1', 'supported'), ('0.3.2', 'unknown'),
                                 ('0.4.0', 'supported'), ('0.4.1', 'unknown'), ('0.2.0', 'unknown'), ('1.0.0', 'unknown')):
            export = fx.standard_export()
            export.meta['extension_version'] = version
            index = build(export, tempfile.mkdtemp(dir=self.tmp))
            self.assertEqual(index.summary['version_support'], support, version)

    def test_in_progress_status_is_incomplete(self):
        # 'reading' and 'exporting' are in-memory run states; a finished run never writes them.
        export = fx.standard_export()
        export.meta['status'] = 'reading'
        index = build(export, self.tmp)
        status = [c for c in index.checks if c['id'] == 'status'][0]
        self.assertEqual(status['level'], 'warning')
        self.assertIn('incomplete', status['title'])

    def test_feed_exhausted(self):
        cases = ((True, 'all', None), (False, 'all', 'warning'), (False, 'max', 'info'),
                 (None, 'all', 'warning'), ('yes', 'all', 'warning'))
        for value, mode, level in cases:
            export = fx.standard_export()
            export.meta['scope']['mode'] = mode
            if value is None:
                del export.meta['feed_exhausted']  # JSON.stringify drops an undefined run.exhausted
            else:
                export.meta['feed_exhausted'] = value
            index = build(export, tempfile.mkdtemp(dir=self.tmp))
            self.assertEqual(checks(index).get('feed'), level, (value, mode))
            self.assertEqual(index.overview()['feed_exhausted'], value if isinstance(value, bool) else None)

    def test_scope_wording_and_small_test(self):
        def scope_check(index):
            return [c for c in index.checks if c['id'] == 'scope'][0]['detail']
        export = fx.standard_export()
        index = build(export, self.tmp)
        self.assertEqual(scope_check(index), 'All jobs the own-job feed returned; images included.')  # max ignored
        self.assertNotIn('test_subset', checks(index))
        export.meta['scope'].update(mode='since', since='2026-03-12', testSubset=True)
        index = build(export, tempfile.mkdtemp(dir=self.tmp))
        self.assertIn('on or after 2026-03-12 (00:00 UTC)', scope_check(index))
        self.assertIn('small test on', scope_check(index))
        self.assertEqual(checks(index)['test_subset'], 'warning')
        export.meta['scope'].update(mode='max', max=5, testSubset=False, images=False)
        index = build(export, tempfile.mkdtemp(dir=self.tmp))
        self.assertEqual(scope_check(index), 'The first 5 jobs in feed order; metadata only, no images.')

    def test_metadata_only_resume_keeps_earlier_job_files(self):
        # A metadata-only resume keeps image_files from an earlier run but resets board files to null.
        export = fx.standard_export()
        export.meta['scope']['images'] = False
        for job in export.jobs[1:]:
            job['image_files'] = []
        for board in export.boards:
            for image in board['images']:
                image['file'] = None
        export.sync_counts()
        index = build(export, self.tmp)
        kept = [r['status'] for r in index.card('job-grid-a')['images']]
        self.assertEqual(kept, ['present'] * 4)
        self.assertEqual({r['status'] for r in index.card('job-grid-b')['images']}, {'not_exported'})
        check = [c for c in index.checks if c['id'] == 'metadata_only'][0]
        self.assertIn('4 image file(s) recorded by an earlier run', check['detail'])

    def test_raw_creation_dates_are_kept_beside_the_normalised_ones(self):
        export = fx.standard_export()
        index = build(export, self.tmp)
        board = index.moodboards()['items'][0]
        self.assertEqual((board['created_at'], board['created_at_raw']),
                         ('2026-02-01T10:00:00.000Z', '2026-02-01 10:00:00.000000'))
        self.assertEqual(index.moodboard_detail('board-1')['moodboard']['created_at_raw'], '2026-02-01 10:00:00.000000')
        profiles = index.profiles_list()['items']
        self.assertEqual((profiles[0]['created_at_raw'], profiles[1]['created_at_raw']), (1767225600000, None))

    def test_partial_export(self):
        export = fx.standard_export()
        export.meta['status'] = 'stopped'
        export.meta['failures'] = [{'kind': 'run', 'id': 'export', 'reason': 'Run paused'}]
        for job in export.jobs[6:]:
            job['image_files'] = []
        export.sync_counts()
        index = build(export, self.tmp)
        levels = checks(index)
        self.assertEqual((levels['status'], levels['failures'], levels['jobs_without_files']), ('warning',) * 3)
        status = [c for c in index.checks if c['id'] == 'status'][0]
        self.assertIn('partial', status['title'])
        ref = index.job_detail('job-flag-only')['images'][0]
        self.assertEqual((ref['status'], ref['url']), ('not_exported', None))
        self.assertIn('no file was recorded', ref['reason'])
        self.assertEqual(index.overview()['failures'], export.meta['failures'])

    def test_metadata_only_export(self):
        export = fx.standard_export()
        export.meta['scope']['images'] = False
        for job in export.jobs:
            job['image_files'] = []
        for board in export.boards:
            for image in board['images']:
                image['file'] = None
        export.files.clear()
        export.sync_counts()
        index = build(export, self.tmp)
        refs = [r for j in index.jobs for r in index.card(j)['images']]
        self.assertEqual({r['status'] for r in refs}, {'not_exported'})
        self.assertTrue(all(r['url'] is None and r['source_url'] for r in refs))
        board_refs = index.moodboard_detail('board-1')['references']
        self.assertEqual({r['status'] for r in board_refs}, {'not_exported'})
        self.assertEqual(checks(index)['metadata_only'], 'info')
        self.assertEqual(index.summary['counts']['images_expected'], 0)
        self.assertEqual(index.summary['warnings'], 0)

    def test_missing_and_unreferenced_files(self):
        export = fx.standard_export()
        export.skip_images.add('images/job-grid-c_2.png')
        export.files['images/stray.png'] = fx.png_bytes(2, 2)
        export.sync_counts()
        index = build(export, self.tmp)
        ref = index.job_detail('job-grid-c')['images'][2]
        self.assertEqual((ref['status'], ref['url']), ('missing', None))
        files = index.overview()['files']
        self.assertEqual(files['missing'], [{'path': 'images/job-grid-c_2.png',
                                              'referenced_by': [{'kind': 'job', 'id': 'job-grid-c', 'index': 2}]}])
        self.assertEqual(files['unreferenced'], ['images/stray.png'])
        levels = checks(index)
        self.assertEqual((levels['files_missing'], levels['files_unreferenced']), ('warning', 'info'))
        self.assertEqual(index.summary['counts']['images_present'], 32)

    def test_non_image_bytes_and_svg_are_unsupported(self):
        export = fx.standard_export()
        export.files['images/job-grid-a_1.png'] = fx.TEXT_BYTES
        export.jobs[4]['image_files'][0] = 'images/job-niji_0.svg'
        export.files['images/job-niji_0.svg'] = fx.SVG_BYTES
        index = build(export, self.tmp)
        text_ref = index.job_detail('job-grid-a')['images'][1]
        svg_ref = index.job_detail('job-niji')['images'][0]
        self.assertEqual((text_ref['status'], text_ref['url']), ('unsupported', None))
        self.assertIn('text file', text_ref['reason'])
        self.assertEqual(svg_ref['status'], 'unsupported')
        self.assertIn('SVG', svg_ref['reason'])
        self.assertEqual(checks(index)['files_unreadable'], 'warning')

    def test_lazy_type_check_for_large_exports(self):
        export = fx.standard_export()
        export.files['images/job-grid-a_1.png'] = fx.TEXT_BYTES
        index = build(export, self.tmp, limits=Limits(sniff_limit=0))
        ref = index.job_detail('job-grid-a')['images'][1]
        self.assertEqual(ref['status'], 'present')  # not checked yet
        rec = index.file(ref['url'].rsplit('/', 1)[1])
        self.assertEqual(rec.status, 'unsupported')
        later = index.job_detail('job-grid-a')['images'][1]  # cards are built on demand, so they catch up
        self.assertEqual((later['status'], later['url']), ('unsupported', None))
        self.assertEqual(checks(index)['files_unchecked'], 'info')

    def test_video_jobs(self):
        export = fx.standard_export()
        export.jobs.append(fx.make_job('job-video', '2026-03-19T00:00:00Z', 'moving water', video=True, with_files=False))
        export.sync_counts()
        index = build(export, self.tmp)
        ref = index.job_detail('job-video')['images'][0]
        self.assertEqual(ref['status'], 'not_exported')
        self.assertIn('video', ref['reason'])
        self.assertEqual(checks(index)['video'], 'info')
        self.assertNotIn('jobs_without_files', checks(index))

    def test_duplicate_ids_keep_the_first_row(self):
        export = fx.standard_export()
        twin = dict(export.jobs[0], prompt='second copy')
        export.jobs.append(twin)
        index = build(export, self.tmp)
        self.assertEqual(index.jobs['job-grid-a'].prompt, 'A Red fox in deep snow')
        self.assertEqual(checks(index)['duplicates'], 'warning')
        self.assertEqual(index.summary['counts']['jobs'], 9)

    def test_parent_cycle_is_guarded(self):
        export = fx.standard_export()
        export.jobs += [
            fx.make_job('cyc-1', '2026-03-01T00:00:00Z', 'loop one', kind='variation', parent_id='cyc-2', parent_index=0),
            fx.make_job('cyc-2', '2026-03-02T00:00:00Z', 'loop two', kind='variation', parent_id='cyc-1', parent_index=1),
            fx.make_job('cyc-tail', '2026-03-03T00:00:00Z', 'tail', kind='upscale', parent_id='cyc-1', parent_index=0),
            fx.make_job('self-loop', '2026-03-04T00:00:00Z', 'me', kind='variation', parent_id='self-loop'),
        ]
        export.sync_counts()
        started = time.monotonic()
        index = build(export, self.tmp)
        lineage = index.job_detail('cyc-1')['lineage']
        self.assertTrue(lineage['cycle'])
        self.assertEqual([a['job_id'] for a in lineage['ancestors']], ['cyc-2'])
        self.assertEqual(lineage['descendant_count'], 2)
        self.assertTrue(index.job_detail('cyc-tail')['lineage']['cycle'])
        self.assertTrue(index.job_detail('self-loop')['lineage']['cycle'])
        self.assertEqual(index.job_detail('self-loop')['lineage']['children'], [])
        self.assertEqual(checks(index)['lineage_cycle'], 'warning')
        self.assertEqual(sorted([c for c in index.checks if c['id'] == 'lineage_cycle'][0]['items']),
                         ['cyc-1', 'cyc-2', 'self-loop'])
        self.assertLess(time.monotonic() - started, 10)

    def test_unreported_parent_gap_is_a_warning(self):
        export = fx.standard_export()
        export.meta['parent_gaps'] = []
        index = build(export, self.tmp)
        self.assertEqual(checks(index)['parent_gaps'], 'warning')
        self.assertFalse(index.overview()['unresolved']['parent_gaps'][0]['reported_by_exporter'])

    def test_kind_and_flags_are_derived_when_missing(self):
        export = fx.standard_export()
        for job in export.jobs:
            del job['kind']
        export.jobs[4].pop('flags')
        export.jobs[4].pop('prompt')
        export.jobs[4]['full_command'] = '"quoted --raw" lanterns --niji 6 --p abc'
        index = build(export, self.tmp)
        kinds = {j: index.card(j)['kind'] for j in ('job-grid-a', 'job-var', 'job-up', 'job-gap')}
        self.assertEqual(kinds, {'job-grid-a': 'grid', 'job-var': 'variation', 'job-up': 'upscale', 'job-gap': 'upscale'})
        self.assertEqual(index.card('job-grid-a')['kind_source'], 'viewer')
        niji = index.job_detail('job-niji')['job']
        self.assertEqual(niji['prompt'], '"quoted --raw" lanterns')
        self.assertEqual((niji['flags_source'], niji['version']['label'], niji['flags']['raw']), ('viewer', 'Niji 6', False))
        self.assertEqual(niji['codes'], [{'code': 'abc', 'type': 'flag', 'resolved': False}])

    def test_gc_is_restored_after_loading_even_on_failure(self):
        import gc
        self.assertTrue(gc.isenabled())
        build(fx.standard_export(), self.tmp)
        self.assertTrue(gc.isenabled())

        class Broken:
            root = ''

            def stat(self, rel):
                raise RuntimeError('boom')

        with self.assertRaises(RuntimeError):
            ExportIndex(Broken(), 'x' * 12, {'type': 'zip', 'name': 'x', 'path': 'x'})
        self.assertTrue(gc.isenabled())

    def test_huge_numbers_never_fail_the_export_root(self):
        """int() refuses more than 4300 digits (Python 3.11+) and a float overflows: one value must not cost the root."""
        digits = '7' * 5000
        export = fx.standard_export()
        export.jobs[0]['flags']['v'] = digits
        export.jobs[1]['flags']['niji'] = digits
        del export.jobs[2]['flags']
        export.jobs[2]['full_command'] = 'green FOX portrait --v ' + digits
        export.jobs[3]['job_type'] = 'v%s_diffusion' % digits
        export.jobs[4]['created_at'] = 10 ** 400
        export.boards[0]['created_at'] = 10 ** 400
        export.profiles[0]['created_at'] = -10 ** 400
        index = build(export, self.tmp)
        self.assertEqual((len(index.jobs), len(index.boards), len(index.profiles)), (9, 2, 2))
        self.assertEqual(index.row_problems, [])
        self.assertTrue(index.job_detail('job-grid-a')['job']['version']['label'].startswith('V777'))
        self.assertEqual(index.job_detail('job-grid-d')['job']['version']['value'], 'unknown')
        self.assertEqual(len(index.bad_dates), 3)
        self.assertEqual(index.job_detail('job-niji')['job']['created_at'], None)

    def test_a_row_nothing_anticipated_is_skipped_alone(self):
        real = model_module.compute_version

        def flaky(flags, job_type):
            if flags.get('v') == '8.1':
                raise RuntimeError('a value nothing anticipated')
            return real(flags, job_type)

        with mock.patch.object(model_module, 'compute_version', side_effect=flaky):
            index = build(fx.standard_export(), self.tmp)
        self.assertEqual(len(index.jobs), 7)  # job-gap and job-flag-only use --v 8.1
        self.assertEqual([(p['file'], p['message']) for p in index.row_problems],
                         [('jobs.jsonl', 'row could not be read (RuntimeError); skipped')] * 2)
        self.assertEqual(checks(index)['parse_errors'], 'warning')

    def test_unsafe_paths_count_once_each(self):
        export = fx.standard_export()
        export.jobs[0]['image_files'] = ['../escape.png', '../escape.png', '/abs.png', 5]
        export.jobs[1]['image_files'] = ['../escape.png'] + export.jobs[1]['image_files'][1:]
        export.boards[0]['images'][0]['file'] = '../escape.png'
        index = build(export, self.tmp)
        unreadable = [u['path'] for u in index.overview()['files']['unreadable']]
        self.assertEqual(sorted(unreadable), ['../escape.png', '/abs.png', '5'])
        self.assertEqual(len(index.unsafe_refs[0]['referenced_by']), 4)
        observed = index.overview()['counts']['observed']
        self.assertEqual(observed['images_expected'], observed['images_present'] + 3)
        statuses = [i['status'] for i in index.job_detail('job-grid-a')['images']]
        self.assertEqual(statuses, ['unsupported'] * 4)

    def test_a_high_image_index_does_not_add_empty_slots(self):
        export = fx.standard_export()
        job = export.jobs[0]
        job['image_files'] = ['images/job-grid-a_0.png', 'images/job-grid-a_999.png']
        job['image_urls'] = job['image_urls'][:2]
        export.files['images/job-grid-a_999.png'] = fx.png_bytes(4, 4)
        index = build(export, self.tmp)
        images = index.job_detail('job-grid-a')['images']
        self.assertEqual([(i['index'], i['file']) for i in images],
                         [(0, 'images/job-grid-a_0.png'), (1, 'images/job-grid-a_999.png')])
        # Indices within a job's four images keep their place, gaps included.
        job['image_files'] = ['images/job-grid-a_0.png', 'images/job-grid-a_3.png']
        export.files['images/job-grid-a_3.png'] = fx.png_bytes(4, 4)
        images = build(export, os.path.join(self.tmp, 'again')).job_detail('job-grid-a')['images']
        self.assertEqual([(i['index'], i['status']) for i in images],
                         [(0, 'present'), (1, 'not_exported'), (2, 'not_exported'), (3, 'present')])

    def test_wrong_types_do_not_crash(self):
        export = fx.standard_export()
        export.jobs[0].update({'width': 'wide', 'image_files': 'images/job-grid-a_0.png', 'personalization_codes': {'a': 1},
                               'created_at': True, 'parent_id': 7, 'flags': {'v': 8, 'profile': 'mbcodeA'}})
        export.boards[0].update({'images': [None, 'x', {'file': 5}], 'codes': 'nope', 'generated_job_ids': 'job-grid-a'})
        export.profiles[0]['codes'] = None
        export.meta['failures'] = 'none'
        index = build(export, self.tmp)
        card = index.card('job-grid-a')
        self.assertIsNone(card['width'])
        self.assertIsNone(card['created_at'])
        self.assertEqual(card['version']['label'], 'V8')
        self.assertEqual(card['parent_status'], 'missing')
        self.assertEqual(checks(index)['generated_job_ids_invalid'], 'warning')
        refs = index.moodboard_detail('board-1')['references']
        self.assertEqual([r['status'] for r in refs], ['not_exported', 'not_exported', 'unsupported'])
        index.overview()
        index.query_jobs({})


class ParserAndValueTests(unittest.TestCase):
    """Parity with lib/parser.js and lib/normalize.js for rows that lack parsed fields."""

    def test_parse_command_flags_and_aliases(self):
        parsed = parse_command('paper boat on a pond --ar 3:2 --v 7 --s 100 --chaos 5 --w 20 --sref https://e.x/a.png::2 '
                               'https://e.x/b.png --p abc def --profile xyz --niji 6 --raw --tile --iw 1.5')
        self.assertEqual(parsed['prompt'], 'paper boat on a pond')
        self.assertEqual(parsed['flags'], {'ar': '3:2', 'v': '7', 'stylize': '100', 'chaos': '5', 'weird': '20',
                                           'sref': ['https://e.x/a.png::2', 'https://e.x/b.png'],
                                           'profile': ['abc', 'def', 'xyz'], 'niji': '6', 'raw': True, 'tile': True,
                                           'iw': '1.5'})

    def test_parse_command_quotes_unknown_and_defaults(self):
        text = '"a sign --raw"::2 kite\'s tail::0.5 --ar 2:3'
        parsed = parse_command(text)
        self.assertEqual(parsed['prompt'], '"a sign --raw"::2 kite\'s tail::0.5')
        self.assertEqual((parsed['flags']['raw'], parsed['flags']['ar']), (False, '2:3'))
        self.assertEqual(parse_command('portrait --seed 12 --no text watermark --v 6.1')['unknown_flags'],
                         {'seed': ['12'], 'no': ['text watermark']})
        self.assertTrue(parse_command('kite --p')['profile_default'])
        self.assertEqual(parse_command('kite --niji')['flags']['niji'], True)
        self.assertEqual(parse_command('kite --sref "https://e.x/a?q=two words" --sref random')['flags']['sref'],
                         ['https://e.x/a?q=two words', 'random'])
        self.assertEqual(parse_command(None)['prompt'], '')

    def test_parse_date(self):
        cases = {
            '2026-03-04T05:06:07.891234+00:00': '2026-03-04T05:06:07.891Z',
            '2026-03-04T07:06:07+02:00': '2026-03-04T05:06:07.000Z',
            '2026-02-03 04:05:06.789012': '2026-02-03T04:05:06.789Z',
            '2026-02-03T04:05:06Z': '2026-02-03T04:05:06.000Z',
            '2026-02-03': '2026-02-03T00:00:00.000Z',
            1767225600000: '2026-01-01T00:00:00.000Z',
            1767225600: '2026-01-01T00:00:00.000Z',
            1767225600.5: '2026-01-01T00:00:00.500Z',
            '1767225600123': '2026-01-01T00:00:00.123Z',
        }
        for value, expected in cases.items():
            self.assertEqual(iso_utc(parse_date(value)), expected, value)
        for value in (None, True, '', 'yesterday', '2026-02-30', '2026-01-01T25:00:00', float('nan'), [], 10 ** 30,
                      10 ** 400, -10 ** 400, '9' * 400, 1e308):
            self.assertIsNone(parse_date(value), repr(value)[:40])

    def test_versions(self):
        self.assertEqual(compute_version({'v': '7.0'}, None)['value'], compute_version({'v': '7'}, None)['value'])
        self.assertEqual(compute_version({'v': '8.2'}, 'v7_diffusion')['label'], 'V8.2')
        self.assertEqual(compute_version({'v': None}, 'v8-1_diffusion'), {'value': 'v8.1', 'label': 'V8.1', 'source': 'job_type'})
        self.assertEqual(compute_version({'niji': '6', 'v': '7'}, None)['label'], 'Niji 6')
        self.assertEqual(compute_version({'v': True}, None), {'value': 'unknown', 'label': 'Unknown', 'source': 'none'})
        self.assertEqual(compute_version({'v': True}, 'v7_diffusion')['label'], 'V7')
        self.assertEqual(compute_version({'niji': True}, 'v7_diffusion')['label'], 'Niji')
        self.assertEqual(compute_version({}, 'mystery')['value'], 'unknown')
        digits = '1' * 5000
        self.assertEqual(compute_version({'v': digits}, None)['label'], 'V' + '1' * 24 + '\u2026')
        self.assertEqual(compute_version({}, 'v%s_diffusion' % digits)['value'], 'unknown')
        # A job_type that mentions niji is never read as a version: its naming has not been observed.
        for job_type in ('niji6_diffusion', 'v6_niji_diffusion', 'NIJI-7'):
            self.assertEqual(compute_version({}, job_type)['value'], 'unknown', job_type)

    def test_kinds(self):
        self.assertEqual(compute_kind('v7_upscale_2x', 'diffusion', 'parent', 1), 'upscale')
        self.assertEqual(compute_kind('v7_diffusion', 'variation', 'parent', 4), 'variation')
        self.assertEqual(compute_kind('v7_diffusion', 'diffusion', None, 4), 'grid')
        self.assertEqual(compute_kind('v7_diffusion', 'diffusion', 'parent', 4), 'other')
        self.assertEqual(compute_kind(None, None, None, 1), 'other')


if __name__ == '__main__':
    unittest.main()
