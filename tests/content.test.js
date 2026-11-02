import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setTimeout(resolve, 0)); };

// Simulated Chrome, page bridge and panel DOM: these tests never access Midjourney.
function harness({ run: savedRun = null, pauseOnDownload = false, feed = {}, stored = {}, chooser = null } = {}) {
  const options = { mode: 'max', max: 20, since: '', images: true, testSubset: true };
  const state = structuredClone({ ...stored, ...(savedRun ? { run: savedRun } : {}) });
  const ops = [], downloads = [], files = {}, statuses = [], lines = [];
  const data = {
    identity: { user_id: 'self', username_v2: 'synthetic' },
    jobs: { data: [], cursor: null, checkpoint: null },
    moodboards: [{ moodboard_id: 'board', title: 'Synthetic', images: [0, 1, 2].map(i => ({ url: `https://cdn.midjourney.com/test${i}.png` })) }],
    profiles: { profiles: [], counts: {} },
    ...feed,
  };
  let messageHandler;
  let downloadId = 0;
  let waiters = [];
  const listeners = [];
  const window = {
    addEventListener: (_, fn) => listeners.push(fn),
    postMessage(message) {
      if (message.source !== 'mj-export-request') return;
      ops.push(message.op);
      queueMicrotask(() => listeners.forEach(fn => fn({
        source: window, origin: 'https://www.midjourney.com',
        data: { source: 'mj-export-response', id: message.id, result: { status: 200, data: data[message.op] } },
      })));
    },
  };
  const element = selector => {
    const children = new Map();
    let text = '';
    return {
      style: {}, append() {}, addEventListener() {},
      querySelector(child) { if (!children.has(child)) children.set(child, element(child)); return children.get(child); },
      get textContent() { return text; },
      set textContent(value) {
        text = value;
        if (selector === '[data-status]') statuses.push(value);
        if (selector === '[data-selection]') lines.push(value);
      },
    };
  };
  const chrome = {
    runtime: {
      onMessage: { addListener(fn) { messageHandler = fn; } },
      getManifest: () => ({ version: 'test' }),
      async sendMessage(message) {
        if (message.type === 'release') { const ready = waiters; waiters = []; ready.forEach(resolve => resolve()); }
        if (message.type === 'cdn-file') {
          downloads.push(message.url);
          if (pauseOnDownload) messageHandler({ type: 'command', command: 'pause', options }, {}, () => {});
          return { status: 200, id: ++downloadId, bytes: 10, filename: message.filename };
        }
        if (message.type === 'text-file') {
          files[message.filename.split('/').pop()] = message.text;
          return { id: ++downloadId, filename: message.filename };
        }
        if (message.type === 'check-file') return { complete: true, bytes: 10 };
        return { ok: true };
      },
    },
    storage: { local: {
      async get() { return structuredClone(state); },
      async set(value) { Object.assign(state, structuredClone(value)); },
      async remove(keys) { for (const key of [keys].flat()) delete state[key]; },
    } },
  };
  const context = vm.createContext({
    window, chrome, crypto: { randomUUID }, URL, structuredClone, queueMicrotask,
    setTimeout, clearTimeout,
    location: { origin: 'https://www.midjourney.com', pathname: '/organize' },
    document: { readyState: 'complete', body: element('body'), createElement: () => element(''), addEventListener() {} },
  });
  if (chooser) context.mjExportChooser = chooser(chrome);
  vm.runInContext(fs.readFileSync(new URL('../src/generated/core.js', import.meta.url), 'utf8'), context);
  vm.runInContext('ReadScheduler.prototype.sleep = async () => {};', context);
  // Replace the default timer for scheduler waits without changing RPC timeouts.
  vm.runInContext('const OriginalScheduler = ReadScheduler; ReadScheduler = class extends OriginalScheduler { constructor() { super({sleep: async () => {}}); } };', context);
  vm.runInContext(fs.readFileSync(new URL('../src/content.js', import.meta.url), 'utf8'), context);
  return {
    state, options, ops, downloads, files, statuses, lines,
    send(command, customOptions = options) { messageHandler({ type: 'command', command, options: customOptions }, {}, () => {}); },
    // Runs one command that takes the run lease, until it has released it and settled.
    async run(command, customOptions = options) {
      const released = new Promise(resolve => waiters.push(resolve));
      this.send(command, customOptions);
      await released;
      await settle();
    },
    output() {
      const rows = name => (files[name] || '').split('\n').filter(Boolean).map(line => JSON.parse(line));
      return { jobs: rows('jobs.jsonl'), boards: rows('moodboards.jsonl'), profiles: rows('profiles.jsonl'), likes: rows('likes.jsonl'), meta: JSON.parse(files['export_meta.json']) };
    },
  };
}

// Synthetic feed for selection tests: made-up ids, prompts, codes and URLs.
const jobId = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const apiJob = (n, references = [], extra = {}) => ({
  id: jobId(n), enqueue_time: `2026-01-0${n}T10:00:00+00:00`, full_command: `synthetic prompt ${n}`, batch_size: 2, image_likes: [],
  personalization_codes: references.map(([id, code, type = 'moodboard']) => ({ id, code, type, user_id: 'self' })), ...extra,
});
const boardFeed = {
  jobs: { cursor: null, checkpoint: null, data: [
    apiJob(1, [['101', 'code-a']]),
    apiJob(2, [['101', 'code-a'], ['102', 'code-b']]),
    apiJob(3, [['102', 'code-b-hidden'], ['p1', 'profile-hidden', 'profile']], { image_likes: [{ synthetic: 3 }] }),
    apiJob(4, [], { parent_id: jobId(3), parent_grid: 1 }),
    apiJob(5, [['103', 'code-c-hidden']]),
    apiJob(6, [['p1', 'profile-shared', 'profile']], { image_likes: [{ synthetic: 6 }] }),
  ] },
  moodboards: [['101', ['a0']], ['102', ['b0', 'b1']], ['103', ['c0']]].map(([moodboard_id, names]) => ({
    moodboard_id, title: `Board ${moodboard_id}`, images: names.map(name => ({ url: `https://cdn.midjourney.com/synthetic/${name}.png` })),
  })),
  profiles: { profiles: [{ id: 'p1', title: 'Synthetic profile', majorVersion: '7', rankingCount: 3, service: 'main', type: 'profile', created: 0 }], counts: {} },
};
const allJobs = { mode: 'all', max: 20, since: '', images: true, testSubset: false };
const scope = { mode: 'all', max: 20, since: '', testSubset: false };
// Job 3 and board 102 are left out; job 5 was not seen when choosing.
const selection = {
  user_id: 'self', scope, chosen_at: '2026-02-01T00:00:00.000Z',
  jobs: { [jobId(1)]: true, [jobId(2)]: true, [jobId(3)]: false, [jobId(4)]: true, [jobId(6)]: true },
  moodboards: { 101: true, 102: false, 103: true },
};
const jobImages = n => [0, 1].map(i => `https://cdn.midjourney.com/${jobId(n)}/0_${i}.jpeg`);
const chosenDownloads = [...[1, 2, 4, 6].flatMap(jobImages), 'https://cdn.midjourney.com/synthetic/a0.png', 'https://cdn.midjourney.com/synthetic/c0.png'];
const hiddenDownloads = [...[3, 5].flatMap(jobImages), 'https://cdn.midjourney.com/synthetic/b0.png', 'https://cdn.midjourney.com/synthetic/b1.png'];

test('scope rejection leaves saved export status and failures untouched', async () => {
  const saved = { options: { mode: 'max', max: 10, since: '', images: true, testSubset: true }, status: 'complete', failures: [] };
  const app = harness({ run: saved });
  await app.run('export');
  assert.deepEqual(app.state.run, saved);
});

test('pausing during board downloads cannot report a complete export', async () => {
  const app = harness({ pauseOnDownload: true });
  await app.run('export');
  assert.equal(app.state.run.status, 'paused');
  assert(app.state.run.boards[0].images.filter(image => image.file).length < 3);
});

test('an export with a saved selection downloads and writes only the chosen jobs and boards', async () => {
  const app = harness({ feed: boardFeed, stored: { selection } });
  await app.run('new', allJobs);
  assert.deepEqual([...app.downloads].sort(), [...chosenDownloads].sort());
  const { jobs, boards, profiles, likes, meta } = app.output();
  assert.deepEqual(jobs.map(j => j.job_id), [1, 2, 4, 6].map(jobId));
  assert.deepEqual(boards.map(b => [b.id, b.generated_job_ids, b.generated_left_out, b.codes]), [['101', [jobId(1), jobId(2)], 0, ['code-a']], ['103', [], 1, []]]);
  assert.deepEqual(likes.map(l => l.job_id), [jobId(6)]);
  assert.deepEqual(profiles[0].codes, ['profile-shared']);
  assert.equal(meta.status, 'complete');
  assert.deepEqual(meta.selection, {
    chosen_at: '2026-02-01T00:00:00.000Z',
    moodboards: { available: 3, exported: 2, left_out: 1 },
    jobs: { available: 6, exported: 4, left_out: 1, not_reviewed: 1 },
    left_out_moodboard_ids: ['102'],
  });
  assert.deepEqual(Object.keys(meta).slice(0, 5), ['extension_version', 'export_time', 'username', 'scope', 'selection']);
  assert.deepEqual(meta.counts, { jobs: 4, moodboards: 2, profiles: 1, likes: 1, files: 10, completed_jobs: 4, download_bytes: 100 });
  assert.equal(meta.limitations.at(-1), 'Items the designer left out are not in this export.');
  assert.deepEqual(meta.parent_gaps, [{ job_id: jobId(4), parent_id: jobId(3), reason: 'The designer left the parent out of this export', left_out: true }]);
  // The rows of exported jobs keep the API data verbatim.
  assert.deepEqual(jobs[1].personalization_codes, boardFeed.jobs.data[1].personalization_codes);
  assert.deepEqual(app.state.run.selection, selection);
});

// A newer choice that ticks everything: it leaves out nothing.
const everything = { user_id: 'self', scope, chosen_at: '2026-03-01T00:00:00.000Z', jobs: Object.fromEntries([1, 2, 3, 4, 5, 6].map(n => [jobId(n), true])), moodboards: { 101: true, 102: true, 103: true } };
const refusal = 'You changed what to export after this export started. To export with your new choice, use New export. To finish this export as it started, use Clear selection first.';

test('resuming an export keeps the selection it started with after a newer choice that leaves out none of it', async () => {
  const first = harness({ feed: boardFeed, stored: { selection }, pauseOnDownload: true });
  await first.run('new', allJobs);
  assert.equal(first.state.run.status, 'paused');
  // A looser choice; one for another scope that says nothing about the exported items; one for another account.
  const unmentioned = { user_id: 'self', scope: { ...scope, mode: 'max', max: 3 }, chosen_at: '2026-03-02T00:00:00.000Z', jobs: { [jobId(1)]: true, [jobId(3)]: false }, moodboards: { 102: false } };
  const otherAccount = { ...everything, user_id: 'someone-else', jobs: { [jobId(1)]: false }, moodboards: { 101: false } };
  for (const newer of [everything, unmentioned, otherAccount]) {
    const second = harness({ feed: boardFeed, stored: { run: first.state.run, selection: newer } });
    await second.run('resume');
    const downloaded = [...first.downloads, ...second.downloads];
    for (const url of hiddenDownloads) assert(!downloaded.includes(url), `${url} was downloaded`);
    assert.deepEqual(new Set(downloaded), new Set(chosenDownloads));
    const { jobs, boards, meta } = second.output();
    assert.equal(meta.status, 'complete');
    assert.deepEqual(jobs.map(j => j.job_id), [1, 2, 4, 6].map(jobId));
    assert.deepEqual(boards.map(b => b.id), ['101', '103']);
    assert.equal(meta.selection.chosen_at, selection.chosen_at);
    assert.deepEqual(second.state.run.selection, selection);
    assert.match(second.lines.at(-1), /Export \/ resume continues your saved export with the choice it started with; New export uses this choice\./);
  }
});

test('a resume stops before it downloads or writes anything when a newer choice leaves out what it exports', async () => {
  // Paused without a choice; paused with a choice; completed with a choice.
  const plain = harness({ feed: boardFeed, pauseOnDownload: true });
  await plain.run('new', allJobs);
  const chosen = harness({ feed: boardFeed, stored: { selection }, pauseOnDownload: true });
  await chosen.run('new', allJobs);
  const done = harness({ feed: boardFeed, stored: { selection } });
  await done.run('new', allJobs);
  assert.deepEqual([plain, chosen, done].map(app => app.state.run.status), ['paused', 'paused', 'complete']);
  // Job 6 has a like, and board 102 has reference images.
  const stricter = { ...everything, jobs: { ...everything.jobs, [jobId(6)]: false }, moodboards: { ...everything.moodboards, 102: false } };
  const stricterThanSelection = { ...selection, chosen_at: '2026-03-01T00:00:00.000Z', jobs: { ...selection.jobs, [jobId(6)]: false } };
  for (const [run, newer, command] of [[plain.state.run, stricter, 'export'], [plain.state.run, { ...everything, moodboards: { ...everything.moodboards, 102: false } }, 'resume'], [chosen.state.run, stricterThanSelection, 'resume'], [done.state.run, stricterThanSelection, 'export']]) {
    const app = harness({ feed: boardFeed, stored: { run, selection: newer } });
    await app.run(command, allJobs);
    assert.equal(app.statuses.at(-1), refusal);
    assert.deepEqual(app.ops, []);
    assert.deepEqual(app.downloads, []);
    assert.deepEqual(app.files, {});
    assert.deepEqual(app.state.run, run);
    assert.match(app.lines.at(-1), /Your saved export includes something this choice leaves out, so Export \/ resume stops: use New export to export with this choice, or Clear selection to finish the saved export as it started\.$/);
  }
  // New export uses the newer choice, and Clear selection lets the saved export finish as it started.
  const fresh = harness({ feed: boardFeed, stored: { run: plain.state.run, selection: stricter } });
  await fresh.run('new', allJobs);
  assert.deepEqual(fresh.output().jobs.map(j => j.job_id), [1, 2, 3, 4, 5].map(jobId));
  const cleared = harness({ feed: boardFeed, stored: { run: plain.state.run, selection: stricter } });
  cleared.send('clear');
  await settle();
  await cleared.run('export', allJobs);
  assert.equal(cleared.output().meta.status, 'complete');
  assert.deepEqual(cleared.output().jobs.map(j => j.job_id), [1, 2, 3, 4, 5, 6].map(jobId));
});

test('a resume also stops when the work it reads now includes something a newer choice leaves out', async () => {
  const first = harness({ feed: boardFeed });
  await first.run('new', allJobs);
  // Job 7 was made after the export; the newer choice saw it and left it out.
  const feed = { ...boardFeed, jobs: { ...boardFeed.jobs, data: [apiJob(7, [['101', 'code-a']]), ...boardFeed.jobs.data] } };
  const app = harness({ feed, stored: { run: first.state.run, selection: { ...everything, jobs: { ...everything.jobs, [jobId(7)]: false } } } });
  await app.run('export', allJobs);
  assert.equal(app.statuses.at(-1), refusal);
  assert.deepEqual(app.ops, ['identity', 'identity', 'jobs', 'moodboards', 'profiles']);
  assert.deepEqual(app.downloads, []);
  assert.deepEqual(app.files, {});
  assert.deepEqual(app.state.run, first.state.run);
});

test('a saved selection for another scope or account is refused before anything is exported', async () => {
  const otherScope = harness({ feed: boardFeed, stored: { selection } });
  await otherScope.run('new', { ...allJobs, mode: 'max' });
  assert.equal(otherScope.statuses.at(-1), 'Your saved choice of what to export was made for All jobs, not Max 20 jobs. Choose again for this scope, or clear the selection.');
  assert.deepEqual(otherScope.ops, []);
  const otherAccount = harness({ feed: boardFeed, stored: { selection: { ...selection, user_id: 'someone-else' } } });
  await otherAccount.run('export', allJobs);
  assert.match(otherAccount.statuses.at(-1), /belongs to another Midjourney account\. Choose again, or clear the selection\./);
  assert.deepEqual(otherAccount.ops, ['identity']);
  for (const app of [otherScope, otherAccount]) {
    assert.equal(app.state.run, undefined);
    assert.deepEqual(app.downloads, []);
    assert.deepEqual(app.files, {});
  }
});

test('without a saved selection the export is unchanged and says so', async () => {
  const app = harness({ feed: boardFeed });
  await app.run('new', allJobs);
  assert.deepEqual(new Set(app.downloads), new Set([...chosenDownloads, ...hiddenDownloads]));
  const { jobs, boards, profiles, likes, meta } = app.output();
  assert.equal(meta.selection, null);
  assert.deepEqual(jobs.map(j => j.job_id), [1, 2, 3, 4, 5, 6].map(jobId));
  assert.deepEqual(boards.map(b => [b.id, b.generated_job_ids]), [['101', [jobId(1), jobId(2)]], ['102', [jobId(2), jobId(3)]], ['103', [jobId(5)]]]);
  assert(boards.every(board => !('generated_left_out' in board)));
  assert.deepEqual(Object.keys(boards[0]), ['id', 'code', 'codes', 'name', 'version', 'created_at', 'generated_job_ids', 'images']);
  assert.deepEqual(profiles[0].codes, ['profile-hidden', 'profile-shared']);
  assert.deepEqual(likes.map(l => l.job_id), [jobId(3), jobId(6)]);
  assert.deepEqual(meta.parent_gaps, []);
  assert.equal(meta.limitations.length, 3);
  assert.equal(app.state.run.selection, null);
});

test('choosing scans first when there is no scan for the account and scope, and leaves a saved export alone', async () => {
  const unfinished = { identity: { user_id: 'self', username_v2: 'synthetic' }, options: { ...allJobs }, selection: null, status: 'paused', failures: [], jobs: [], boards: [], profiles: [], done_ids: [], done_files: {} };
  let opened = 0;
  const chooser = chrome => ({ async open() {
    opened++;
    const { scan_data: scan } = await chrome.storage.local.get('scan_data');
    const saved = { user_id: scan.user_id, scope: scan.scope, chosen_at: '2026-02-03T00:00:00.000Z', jobs: Object.fromEntries(scan.jobs.map(j => [j.id, j.id !== jobId(3)])), moodboards: Object.fromEntries(scan.boards.map(b => [b.id, true])) };
    await chrome.storage.local.set({ selection: saved });
    return saved;
  } });
  const app = harness({ feed: boardFeed, run: unfinished, chooser });
  await app.run('choose', allJobs);
  assert.equal(opened, 1);
  assert.deepEqual(app.ops, ['identity', 'jobs', 'moodboards', 'profiles']);
  assert.equal(app.state.scan_data.user_id, 'self');
  assert.deepEqual(app.state.scan_data.scope, scope);
  assert.deepEqual(app.state.scan_data.boards.map(b => [b.id, b.jobs]), [['101', [jobId(1), jobId(2)]], ['102', [jobId(2), jobId(3)]], ['103', [jobId(5)]]]);
  assert.deepEqual(app.state.run, unfinished);
  assert(app.statuses.some(text => text.startsWith('There is no scan of this scope for this account yet, so it scans first.')));
  assert.equal(app.statuses.at(-1), 'Saved. Exporting 3 of 3 moodboards · 5 of 6 generations. Use New export to export with this choice.');
  // With a scan of this account and scope, choosing reads nothing but the account.
  const again = harness({ feed: boardFeed, stored: { scan_data: app.state.scan_data }, chooser });
  await again.run('choose', allJobs);
  assert.deepEqual(again.ops, ['identity']);
  assert.equal(again.statuses.at(-1), 'Saved. Exporting 3 of 3 moodboards · 5 of 6 generations. Export uses this choice.');
  // A scan of another scope does not count.
  const otherScope = harness({ feed: boardFeed, stored: { scan_data: app.state.scan_data }, chooser });
  await otherScope.run('choose', { ...allJobs, testSubset: true });
  assert.deepEqual(otherScope.ops, ['identity', 'jobs', 'moodboards', 'profiles']);
  assert.equal(otherScope.state.scan_data.scope.testSubset, true);
});

test('clearing the selection goes back to exporting everything', async () => {
  const app = harness({ stored: { selection } });
  await settle();
  assert.match(app.lines.at(-1), /^What to export: 2 of 3 moodboards · 4 of 5 generations \(chosen for All jobs\)\.$/);
  app.send('clear');
  await settle();
  assert.equal(app.state.selection, undefined);
  assert.equal(app.statuses.at(-1), 'Selection cleared. New exports include everything in the scope again.');
  assert.match(app.lines.at(-1), /^What to export: everything in the scope\./);
});
