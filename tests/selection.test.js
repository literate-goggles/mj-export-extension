import test from 'node:test';
import assert from 'node:assert/strict';
import {normalizeJob, normalizeBoards, normalizeProfiles, parentGaps} from '../lib/normalize.js';
import {
  applySelection, chooserGroups, choiceCounts, compactScan, groupState, initialChoice, makeSelection, resumeProblem,
  reusableScan, sameScope, scopeLabel, selectionLine, selectionProblem, setEverything, setGroup, setJobs, summaryText, toggleGroup,
} from '../lib/selection.js';

// Synthetic data only: made-up ids, prompts, codes and URLs.
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const reference = (refId, code, type = 'moodboard') => ({id: refId, code, type, user_id: 'self'});
const job = (n, references = [], extra = {}) => normalizeJob({
  id: id(n), enqueue_time: `2026-01-${String(n).padStart(2, '0')}T10:00:00+00:00`, full_command: `synthetic prompt ${n} --ar 1:1`,
  batch_size: 4, personalization_codes: references, image_likes: [], ...extra,
});
const scope = {mode: 'all', max: 20, since: ''};
const options = {...scope, images: true};
const identity = {user_id: 'self', username_v2: 'synthetic'};
function feed() {
  return {
    identity,
    moodboards: ['a', 'b', 'c', 'd'].map((name, i) => ({
      moodboard_id: String(101 + i), title: `Board ${name}`,
      images: [0, 1].map(k => ({url: `https://cdn.midjourney.com/synthetic/${name}${k}.png`, width: 10, height: 10})),
    })),
    profiles: {profiles: [{id: 'p1', title: 'Synthetic profile', majorVersion: '7', rankingCount: 3, service: 'main', type: 'profile', created: 0}], counts: {mainV7: 2}},
    jobs: [
      job(1, [reference('101', 'code-a1')]),
      job(2, [reference('101', 'code-a2'), reference('102', 'code-b2')]),
      job(3, [reference('102', 'code-b-hidden'), reference('p1', 'profile-hidden', 'profile')], {image_likes: [{synthetic: 3}]}),
      job(4, [], {parent_id: id(3), parent_grid: 0}),
      job(5, [reference('103', 'code-c-hidden')]),
      job(6, [], {parent_id: id(99)}),
      job(7, [reference('p1', 'profile-shared', 'profile'), reference(null, 'user-code', 'user')], {image_likes: [{synthetic: 7}]}),
      job(8, [], {video_segments: []}),
    ],
  };
}
// Job 3 and board 102 are left out; job 5 and board 104 were not seen when choosing.
const selection = {
  user_id: 'self', scope, chosen_at: '2026-02-01T00:00:00.000Z',
  jobs: {[id(1)]: true, [id(2)]: true, [id(3)]: false, [id(4)]: true, [id(6)]: true, [id(7)]: true, [id(8)]: true},
  moodboards: {101: true, 102: false, 103: true},
};
const hidden = [id(3), id(5)];

test('without a selection the export is exactly the unselected one', () => {
  const data = feed();
  const result = applySelection(data, null);
  assert.deepEqual(result, {
    jobs: data.jobs, boards: normalizeBoards(data.moodboards, data.jobs),
    profiles: normalizeProfiles(data.profiles, identity, data.jobs), selection: null, left_out_parents: [],
  });
  assert(result.boards.every(board => !('generated_left_out' in board)));
  assert.deepEqual(parentGaps(result.jobs, result.left_out_parents), parentGaps(data.jobs));
  const small = applySelection(data, null, {testSubset: true});
  assert.deepEqual(small.boards.map(board => board.id), ['101']);
  assert.equal(small.profiles.length, 1);
});

test('left-out and not-reviewed items leave no trace in rows, links, codes or downloads', () => {
  const data = feed();
  const result = applySelection(data, selection);
  assert.deepEqual(result.jobs.map(j => j.job_id), [1, 2, 4, 6, 7, 8].map(id));
  // An exported child keeps its parent_id (lineage); the left-out parent is reported in parent_gaps.
  const linked = JSON.stringify({boards: result.boards, profiles: result.profiles});
  for (const jobId of hidden) assert(!linked.includes(jobId), `${jobId} is linked from a board or profile`);
  const text = JSON.stringify({jobs: result.jobs, boards: result.boards, profiles: result.profiles});
  for (const code of ['code-b-hidden', 'code-c-hidden', 'profile-hidden']) assert(!text.includes(code), `${code} appears in the export`);
  // Images are downloaded from exported job rows and board rows only.
  const downloads = [...result.jobs.flatMap(j => j.image_urls), ...result.boards.flatMap(b => b.images.map(i => i.source_url))];
  for (const url of downloads) assert(!hidden.some(jobId => url.includes(jobId)) && !/\/(b|d)\d\.png$/.test(url), url);
  assert.deepEqual(result.jobs.filter(j => j.image_likes.length).map(j => j.job_id), [id(7)]);
  // Left-out board 102 and unseen board 104 have no row.
  assert.deepEqual(result.boards.map(b => b.id), ['101', '103']);
  assert.deepEqual(result.boards.map(b => b.generated_job_ids), [[id(1), id(2)], []]);
  assert.deepEqual(result.boards.map(b => b.codes), [['code-a1', 'code-a2'], []]);
  assert.deepEqual(result.boards.map(b => b.generated_left_out), [0, 1]);
  assert.deepEqual(Object.keys(result.boards[0]), ['id', 'code', 'codes', 'name', 'version', 'created_at', 'generated_job_ids', 'generated_left_out', 'images']);
  assert.deepEqual(result.profiles.find(p => p.id === 'p1').codes, ['profile-shared']);
});

test('the selection block counts what was available, exported, left out and not reviewed', () => {
  const result = applySelection(feed(), selection);
  assert.deepEqual(result.selection, {
    chosen_at: '2026-02-01T00:00:00.000Z',
    moodboards: {available: 4, exported: 2, left_out: 1},
    jobs: {available: 8, exported: 6, left_out: 1, not_reviewed: 1},
    left_out_moodboard_ids: ['102'],
  });
  // Small test trims the chosen boards further, so the counts need not add up to what was available.
  const small = applySelection(feed(), selection, {testSubset: true});
  assert.deepEqual(small.boards.map(b => b.id), ['101']);
  assert.equal(small.selection.moodboards.exported + small.selection.moodboards.left_out <= small.selection.moodboards.available, true);
  assert.deepEqual(small.selection.left_out_moodboard_ids, ['102']);
});

test('a left-out board is listed only when an exported job references it', () => {
  const only = {...selection, jobs: {...selection.jobs, [id(2)]: false}, moodboards: {101: true, 102: false, 103: false, 104: false}};
  const result = applySelection(feed(), only);
  assert.deepEqual(result.selection.left_out_moodboard_ids, []);
  assert.deepEqual(result.selection.moodboards, {available: 4, exported: 1, left_out: 3});
  assert.deepEqual(result.boards[0].generated_left_out, 1);
});

test('a parent left out by the designer is a marked parent gap', () => {
  const result = applySelection(feed(), selection);
  assert.deepEqual(result.left_out_parents, [id(3)]);
  assert.deepEqual(parentGaps(result.jobs, result.left_out_parents), [
    {job_id: id(4), parent_id: id(3), reason: 'The designer left the parent out of this export', left_out: true},
    {job_id: id(6), parent_id: id(99), reason: 'Parent is outside the selected subset or unavailable in the own feed'},
  ]);
});

test('jobs that appear after choosing are not reviewed and not exported', () => {
  const data = feed();
  data.jobs.unshift(job(20, [reference('101', 'code-new')]));
  const result = applySelection(data, selection);
  assert(!result.jobs.some(j => j.job_id === id(20)));
  assert.equal(result.selection.jobs.not_reviewed, 2);
  assert.equal(result.boards[0].generated_left_out, 1);
  assert(!result.boards[0].codes.includes('code-new'));
  // A decision that is not true or false counts as not reviewed and is never exported.
  const odd = applySelection(feed(), {...selection, jobs: {...selection.jobs, [id(1)]: 'yes'}});
  assert(!odd.jobs.some(j => j.job_id === id(1)));
  assert.equal(odd.selection.jobs.not_reviewed, 2);
});

test('a choice of everything seen exports the same rows, plus the selection fields', () => {
  const data = feed();
  const scan = compactScan(identity, options, data.jobs, data.moodboards, '2026-02-01T00:00:00.000Z');
  const result = applySelection(data, makeSelection(scan, initialChoice(scan, null)));
  const plain = applySelection(data, null);
  assert.deepEqual(result.jobs, plain.jobs);
  assert.deepEqual(result.boards.map(({generated_left_out, ...board}) => board), plain.boards);
  assert(result.boards.every(board => board.generated_left_out === 0));
  assert.deepEqual(result.profiles, plain.profiles);
  assert.deepEqual(result.selection.jobs, {available: 8, exported: 8, left_out: 0, not_reviewed: 0});
});

test('scan data groups jobs by every own board they reference', () => {
  const data = feed();
  const scan = compactScan(identity, options, data.jobs, data.moodboards, '2026-02-01T00:00:00.000Z');
  assert.deepEqual(scan.scope, scope);
  assert.equal(scan.user_id, 'self');
  assert.deepEqual(scan.jobs[1], {id: id(2), created_at: '2026-01-02T10:00:00+00:00', prompt: 'synthetic prompt 2', kind: 'grid', images: 4, parent_id: null, boards: ['101', '102'], video: false});
  assert.equal(scan.jobs[7].video, true);
  assert.deepEqual(scan.boards[0], {id: '101', name: 'Board a', images: ['https://cdn.midjourney.com/synthetic/a0.png', 'https://cdn.midjourney.com/synthetic/a1.png'], jobs: [id(1), id(2)]});
  const groups = chooserGroups(scan);
  assert.deepEqual(groups.map(g => [g.board, g.jobs]), [
    ['101', [id(1), id(2)]], ['102', [id(2), id(3)]], ['103', [id(5)]], ['104', []],
    [null, [id(4), id(6), id(7), id(8)]],
  ]);
  // Small test still shows every board, and is not part of the scope: it trims the export, not the choice.
  const small = compactScan(identity, {...options, testSubset: true}, data.jobs, data.moodboards);
  assert.equal(small.boards.length, 4);
  assert.deepEqual(small.scope, scope);
});

test('Choose reuses a scan of the same account and scope only while it is under ten minutes old', () => {
  const data = feed();
  const scan = compactScan(identity, options, data.jobs, data.moodboards, '2026-02-01T10:00:00.000Z');
  const at = time => Date.parse(`2026-02-01T${time}.000Z`);
  assert.equal(reusableScan(scan, 'self', options, at('10:00:00')), true);
  assert.equal(reusableScan(scan, 'self', options, at('10:09:59')), true);
  assert.equal(reusableScan(scan, 'self', options, at('10:10:00')), false);
  // A scan time in the future, or none, is not trusted.
  assert.equal(reusableScan(scan, 'self', options, at('09:59:59')), false);
  assert.equal(reusableScan({...scan, scanned_at: undefined}, 'self', options, at('10:00:01')), false);
  assert.equal(reusableScan(scan, 'someone-else', options, at('10:00:01')), false);
  assert.equal(reusableScan(scan, 'self', {...options, mode: 'max'}, at('10:00:01')), false);
  assert.equal(reusableScan(undefined, 'self', options, at('10:00:01')), false);
});

test('whole-board, single-generation and select-all changes', () => {
  const data = feed();
  const scan = compactScan(identity, options, data.jobs, data.moodboards);
  const [a, b, , d, other] = chooserGroups(scan);
  const choice = initialChoice(scan, null);
  assert.equal(groupState(choice, a), 'all');
  setJobs(choice, [id(2)], false);
  // Job 2 is in two boards: both show a partial choice.
  assert.equal(groupState(choice, a), 'some');
  assert.equal(groupState(choice, b), 'some');
  toggleGroup(choice, a);
  assert.equal(groupState(choice, a), 'all');
  assert.equal(groupState(choice, b), 'all');
  toggleGroup(choice, a);
  assert.equal(choice.moodboards['101'], false);
  assert.equal(choice.jobs[id(1)], false);
  assert.equal(groupState(choice, a), 'none');
  assert.equal(groupState(choice, b), 'some');
  // A board without generations is just its references.
  setGroup(choice, d, false);
  assert.equal(groupState(choice, d), 'none');
  setGroup(choice, other, false);
  assert.equal(groupState(choice, other), 'none');
  assert.equal(groupState(choice, {board: null, jobs: []}), 'none');
  setJobs(choice, b.jobs, true);
  assert.equal(groupState(choice, b), 'all');
  assert.deepEqual(Object.keys(choice.jobs).filter(key => choice.jobs[key]), [id(2), id(3), id(5)]);
  assert.deepEqual(choiceCounts(choice), {moodboards: {chosen: 2, total: 4}, jobs: {chosen: 3, total: 8}});
  assert.equal(summaryText(choiceCounts(choice)), 'Exporting 2 of 4 moodboards · 3 of 8 generations');
  setEverything(choice, false);
  assert.deepEqual(choiceCounts(choice), {moodboards: {chosen: 0, total: 4}, jobs: {chosen: 0, total: 8}});
  setEverything(choice, true);
  assert.equal(summaryText(choiceCounts(choice)), 'Exporting 4 of 4 moodboards · 8 of 8 generations');
  assert.equal(summaryText({moodboards: {chosen: 1, total: 1}, jobs: {chosen: 0, total: 1}}), 'Exporting 1 of 1 moodboard · 0 of 1 generation');
});

test('the chooser starts from the latest saved choice for the account, whatever its scope', () => {
  const data = feed();
  const scan = compactScan(identity, options, data.jobs, data.moodboards);
  for (const saved of [selection, {...selection, scope: {...scope, mode: 'max', max: 3}}, {...selection, scope: {...scope, testSubset: true}}]) {
    const restored = initialChoice(scan, saved);
    assert.equal(restored.jobs[id(3)], false);
    assert.equal(restored.moodboards['102'], false);
    // Items the saved choice never saw start ticked.
    assert.equal(restored.jobs[id(5)], true);
    assert.equal(restored.moodboards['104'], true);
  }
  assert(Object.values(initialChoice(scan, {...selection, user_id: 'someone-else'}).jobs).every(Boolean));
  const restored = initialChoice(scan, selection);
  const saved = makeSelection(scan, restored, '2026-02-02T00:00:00.000Z');
  assert.deepEqual(Object.keys(saved), ['user_id', 'scope', 'chosen_at', 'jobs', 'moodboards']);
  assert.deepEqual(saved.scope, scope);
  assert.deepEqual(Object.keys(saved.jobs), data.jobs.map(j => j.job_id));
  assert.deepEqual(Object.keys(saved.moodboards), ['101', '102', '103', '104']);
  assert(Object.values({...saved.jobs, ...saved.moodboards}).every(value => typeof value === 'boolean'));
});

test('a saved selection is refused for another account or scope', () => {
  assert.equal(selectionProblem(null, options, 'self'), null);
  assert.equal(selectionProblem(selection, options, 'self'), null);
  assert.match(selectionProblem(selection, options, 'someone-else'), /another Midjourney account/);
  assert.match(selectionProblem(selection, {...options, mode: 'max', max: 20}), /made for All jobs, not Max 20 jobs\. Choose again for this scope, or clear the selection\./);
  // Small test and Include images are not part of the scope.
  assert.equal(selectionProblem(selection, {...options, testSubset: true, images: false}, 'self'), null);
  assert.equal(selectionProblem({...selection, scope: {...scope, testSubset: true}}, options, 'self'), null);
  assert.match(selectionProblem({...selection, scope: undefined}, options), /another scope/);
  // The scope check alone runs before the account is known.
  assert.equal(selectionProblem({...selection, user_id: 'someone-else'}, options), null);
  // N matters only for Max N, and the date only for Since.
  assert(sameScope(scope, {...scope, max: 50, since: '2026-01-01'}));
  assert(!sameScope({...scope, mode: 'max'}, {...scope, mode: 'max', max: 21}));
  assert(!sameScope({...scope, mode: 'since', since: '2026-01-01'}, {...scope, mode: 'since', since: '2026-01-02'}));
  assert.equal(scopeLabel({mode: 'since', since: '2026-01-01', testSubset: true}), 'Jobs since 2026-01-01');
});

test('the one-line selection summary says what Export and New export will use', () => {
  assert.match(selectionLine(null, null), /^What to export: everything in the scope\./);
  assert.equal(selectionLine(selection, null), 'What to export: 2 of 3 moodboards · 6 of 7 generations (chosen for All jobs).');
  assert.equal(selectionLine(selection, {selection}), 'What to export: 2 of 3 moodboards · 6 of 7 generations (chosen for All jobs).');
  assert.match(selectionLine(selection, {selection: null}), /Export \/ resume continues your saved export with everything in its scope; New export uses this choice\.$/);
  assert.match(selectionLine(null, {selection}), /with the choice it started with; New export exports everything\.$/);
  // A saved export without a choice whose rows include job 3, which this choice leaves out.
  assert.match(selectionLine(selection, {identity, selection: null, jobs: [{job_id: id(3)}], boards: []}), /\(chosen for All jobs\)\. Your saved export includes something this choice leaves out, so Export \/ resume stops: use New export to export with this choice, or Clear selection to finish the saved export as it started\.$/);
});

test('a resume stops only for a newer choice of its account that leaves out something it would export', () => {
  const run = {identity, selection, jobs: [{job_id: id(1)}, {job_id: id(2)}], boards: [{id: '101'}]};
  const newer = {...selection, chosen_at: '2026-03-01T00:00:00.000Z'};
  const stop = /^You changed what to export after this export started\. To export with your new choice, use New export\. To finish this export as it started, use Clear selection first\.$/;
  assert.equal(resumeProblem(null, run), null);
  // The run's own choice, even if its decisions were changed in storage, is not a newer one.
  assert.equal(resumeProblem({...selection, jobs: {[id(1)]: false}}, run), null);
  assert.equal(resumeProblem(newer, run), null);
  assert.match(resumeProblem({...newer, jobs: {...newer.jobs, [id(2)]: false}}, run), stop);
  assert.match(resumeProblem({...newer, moodboards: {101: false}}, run), stop);
  // Items the newer choice does not mention, items the run does not export, and another account's choice do not stop it.
  assert.equal(resumeProblem({...newer, jobs: {}, moodboards: {}}, run), null);
  assert.equal(resumeProblem({...newer, jobs: {[id(3)]: false}, moodboards: {102: false}}, run), null);
  assert.equal(resumeProblem({...newer, user_id: 'someone-else', jobs: {[id(1)]: false}}, run), null);
  // A run without a choice: every saved choice is newer, whatever its scope; the rows it would export can be passed in.
  const plain = {...run, selection: null};
  assert.match(resumeProblem({...selection, scope: {...scope, mode: 'max'}, jobs: {[id(2)]: false}}, plain), stop);
  assert.equal(resumeProblem(newer, plain, {jobs: [{job_id: id(1)}], boards: []}), null);
  assert.match(resumeProblem(newer, plain, {jobs: [{job_id: id(1)}, {job_id: id(3)}], boards: []}), stop);
});
