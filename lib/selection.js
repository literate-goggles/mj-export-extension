import {normalizeBoards, normalizeProfiles} from './normalize.js';
// Pure selection logic: what the chooser shows and what an export with a saved choice writes.
// A saved choice is an opt-in snapshot: only items it marks true are exported.
function referenceIdsOf(job) {
 return new Set((job.personalization_codes || []).map(reference => reference?.id).filter(Boolean));
}
function choiceOf(map, id) {
 return map && Object.hasOwn(map, id) && typeof map[id] === 'boolean' ? map[id] : undefined;
}
export function countLabel(n, word) {
 return `${n} ${word}${n === 1 ? '' : 's'}`;
}
export function scopeOf(options) {
 return {mode: options.mode, max: options.max, since: options.since, testSubset: !!options.testSubset};
}
// Images on or off does not change what is in scope; N and the date matter only in their own mode.
export function sameScope(a, b) {
 if (!a || !b || a.mode !== b.mode || !!a.testSubset !== !!b.testSubset) return false;
 return (a.mode !== 'max' || a.max === b.max) && (a.mode !== 'since' || a.since === b.since);
}
export function scopeLabel(scope) {
 const base = scope.mode === 'max' ? `Max ${scope.max} jobs` : scope.mode === 'since' ? `Jobs since ${scope.since}` : 'All jobs';
 return scope.testSubset ? `${base} · small test` : base;
}
// What the chooser needs, kept by Scan: no images, and every own board even with Small test on.
export function compactScan(identity, options, jobs, moodboards, scannedAt = new Date().toISOString()) {
 const boards = normalizeBoards(moodboards, jobs);
 const own = new Set(boards.map(board => board.id));
 return {
  user_id: identity.user_id, scope: scopeOf(options), scanned_at: scannedAt,
  jobs: jobs.map(job => ({
   id: job.job_id, created_at: job.created_at, prompt: job.prompt, kind: job.kind, images: job.image_urls.length,
   parent_id: job.parent_id, boards: [...referenceIdsOf(job)].filter(id => own.has(id)), video: job.video,
  })),
  boards: boards.map(board => ({id: board.id, name: board.name ?? '', images: board.images.map(image => image.source_url), jobs: board.generated_job_ids})),
 };
}
// A job belongs to every own board it references; jobs that reference none are "Other generations".
export function chooserGroups(scan) {
 const groups = scan.boards.map(board => ({key: `board:${board.id}`, board: board.id, name: board.name || '', references: board.images || [], jobs: board.jobs || []}));
 groups.push({key: 'other', board: null, name: 'Other generations', references: [], jobs: scan.jobs.filter(job => !job.boards?.length).map(job => job.id)});
 return groups;
}
// Everything starts ticked; a saved choice for the same account and scope restores its decisions.
export function initialChoice(scan, saved) {
 const usable = !!saved && saved.user_id === scan.user_id && sameScope(saved.scope, scan.scope);
 const pick = (map, id) => (usable ? choiceOf(map, id) : undefined) ?? true;
 return {
  jobs: Object.fromEntries(scan.jobs.map(job => [job.id, pick(saved?.jobs, job.id)])),
  moodboards: Object.fromEntries(scan.boards.map(board => [board.id, pick(saved?.moodboards, board.id)])),
 };
}
// A whole board is its references and all its generations.
export function groupState(choice, group) {
 const values = group.jobs.map(id => choice.jobs[id] === true);
 if (group.board !== null) values.push(choice.moodboards[group.board] === true);
 if (!values.length || !values.some(Boolean)) return 'none';
 return values.every(Boolean) ? 'all' : 'some';
}
export function setGroup(choice, group, value) {
 if (group.board !== null) choice.moodboards[group.board] = value;
 return setJobs(choice, group.jobs, value);
}
export function toggleGroup(choice, group) {
 return setGroup(choice, group, groupState(choice, group) !== 'all');
}
export function setJobs(choice, ids, value) {
 for (const id of ids) choice.jobs[id] = value;
 return choice;
}
export function setEverything(choice, value) {
 for (const id of Object.keys(choice.jobs)) choice.jobs[id] = value;
 for (const id of Object.keys(choice.moodboards)) choice.moodboards[id] = value;
 return choice;
}
// Works on a chooser state and on a saved selection alike.
export function choiceCounts(choice) {
 const count = map => ({chosen: Object.values(map || {}).filter(value => value === true).length, total: Object.keys(map || {}).length});
 return {moodboards: count(choice.moodboards), jobs: count(choice.jobs)};
}
export function summaryText(counts) {
 return `Exporting ${counts.moodboards.chosen} of ${countLabel(counts.moodboards.total, 'moodboard')} · ${counts.jobs.chosen} of ${countLabel(counts.jobs.total, 'generation')}`;
}
// The saved selection records a decision for every job and board seen when choosing.
export function makeSelection(scan, choice, chosenAt = new Date().toISOString()) {
 return {
  user_id: scan.user_id, scope: scopeOf(scan.scope), chosen_at: chosenAt,
  jobs: Object.fromEntries(scan.jobs.map(job => [job.id, choice.jobs[job.id] === true])),
  moodboards: Object.fromEntries(scan.boards.map(board => [board.id, choice.moodboards[board.id] === true])),
 };
}
// Leave userId undefined to check only the scope (before the account is known).
export function selectionProblem(selection, options, userId) {
 if (!selection) return null;
 if (userId !== undefined && selection.user_id !== userId) return 'Your saved choice of what to export belongs to another Midjourney account. Choose again, or clear the selection.';
 if (!sameScope(selection.scope, options)) {
  return `Your saved choice of what to export was made for ${selection.scope ? scopeLabel(selection.scope) : 'another scope'}, not ${scopeLabel(options)}. Choose again for this scope, or clear the selection.`;
 }
 return null;
}
// A resume keeps the choice its run started with, but never exports what the designer's latest choice for the
// account leaves out, whatever that choice's scope. rows: what the resume would export; by default the run's own rows.
export function resumeProblem(selection, run, rows = run) {
 if (!selection || selection.user_id !== run.identity?.user_id || (run.selection && selection.chosen_at === run.selection.chosen_at)) return null;
 const leftOut = (rows.jobs || []).some(job => choiceOf(selection.jobs, job.job_id) === false) || (rows.boards || []).some(board => choiceOf(selection.moodboards, board.id) === false);
 return leftOut ? 'You changed what to export after this export started. To export with your new choice, use New export. To finish this export as it started, use Clear selection first.' : null;
}
// One line for the panel and the popup. A saved export keeps the choice it started with, unless a newer one leaves out its rows.
export function selectionLine(selection, run) {
 let text = 'What to export: everything in the scope. “Choose what to export” lets you leave out moodboards or generations.';
 if (selection) {
  const counts = choiceCounts(selection);
  text = `What to export: ${counts.moodboards.chosen} of ${countLabel(counts.moodboards.total, 'moodboard')} · ${counts.jobs.chosen} of ${countLabel(counts.jobs.total, 'generation')} (chosen for ${selection.scope ? scopeLabel(selection.scope) : 'an unknown scope'}).`;
 }
 if (run && (run.selection?.chosen_at ?? null) !== (selection?.chosen_at ?? null)) {
  text += resumeProblem(selection, run)
   ? ' Your saved export includes something this choice leaves out, so Export / resume stops: use New export to export with this choice, or Clear selection to finish the saved export as it started.'
   : ` Export / resume continues your saved export ${run.selection ? 'with the choice it started with' : 'with everything in its scope'}; New export ${selection ? 'uses this choice' : 'exports everything'}.`;
 }
 return text;
}
// The export_meta.json "selection" block. jobs and moodboards are what was in scope (raw boards).
export function summarizeSelection(selection, jobs, exportedJobs, moodboards, exportedBoards) {
 const referenced = new Set(exportedJobs.flatMap(job => [...referenceIdsOf(job)]));
 const jobChoice = job => choiceOf(selection.jobs, job.job_id);
 const boardChoice = board => choiceOf(selection.moodboards, board.moodboard_id);
 return {
  chosen_at: selection.chosen_at ?? null,
  moodboards: {available: moodboards.length, exported: exportedBoards.length, left_out: moodboards.filter(board => boardChoice(board) === false).length},
  jobs: {
   available: jobs.length, exported: exportedJobs.length,
   left_out: jobs.filter(job => jobChoice(job) === false).length,
   not_reviewed: jobs.filter(job => jobChoice(job) === undefined).length,
  },
  left_out_moodboard_ids: moodboards.filter(board => boardChoice(board) === false && referenced.has(board.moodboard_id)).map(board => board.moodboard_id),
 };
}
// Turns freshly read feed data ({identity, jobs, moodboards, profiles}) into exactly what is exported.
// Without a selection this is the unchanged export. Small test keeps the first exported board and profile.
export function applySelection(data, selection, {testSubset = false} = {}) {
 const trim = list => (testSubset ? list.slice(0, 1) : list);
 if (!selection) {
  return {
   jobs: data.jobs, boards: trim(normalizeBoards(data.moodboards, data.jobs)),
   profiles: trim(normalizeProfiles(data.profiles, data.identity, data.jobs)), selection: null, left_out_parents: [],
  };
 }
 const jobs = data.jobs.filter(job => choiceOf(selection.jobs, job.job_id) === true);
 const kept = new Set(jobs.map(job => job.job_id));
 const dropped = data.jobs.filter(job => !kept.has(job.job_id));
 const droppedIds = new Set(dropped.map(job => job.job_id));
 const leftOutUses = new Map();
 for (const job of dropped) for (const id of referenceIdsOf(job)) leftOutUses.set(id, (leftOutUses.get(id) || 0) + 1);
 const chosenBoards = data.moodboards.filter(board => choiceOf(selection.moodboards, board.moodboard_id) === true);
 const boards = trim(normalizeBoards(chosenBoards, jobs).map(({images, ...board}) => ({...board, generated_left_out: leftOutUses.get(board.id) || 0, images})));
 return {
  jobs, boards, profiles: trim(normalizeProfiles(data.profiles, data.identity, jobs)),
  selection: summarizeSelection(selection, data.jobs, jobs, data.moodboards, boards),
  left_out_parents: [...new Set(jobs.filter(job => job.parent_id && droppedIds.has(job.parent_id)).map(job => job.parent_id))],
 };
}
