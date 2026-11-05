import test, { mock } from 'node:test';
import assert from 'node:assert/strict';

// Simulated Chrome for the service worker's run lease and downloads: these tests never access Midjourney.
const session = {};
let listener, holder = null;
// chrome.downloads: each download walks through the states its script gives, one more on every search.
const downloads = new Map(), calls = [], changed = new Set();
let script = () => [], downloadId = 0, start = null;
globalThis.chrome = {
  runtime: { id: 'test', onMessage: { addListener(fn) { listener = fn; } } },
  storage: { session: {
    async get(key) { return { [key]: session[key] }; },
    async set(value) { Object.assign(session, value); },
    async remove(key) { delete session[key]; },
  } },
  tabs: { async sendMessage(tab, message) { if (holder instanceof Error) throw holder; return message.type === 'run-state' ? holder : undefined; } },
  downloads: {
    // A script of null: Chrome has not created the download yet (no response); start() then creates it. An Error: Chrome refuses to start it.
    async download(options) {
      const id = ++downloadId, steps = script(options); calls.push(['download', options]);
      if (steps instanceof Error) throw steps; // Chrome could not start it
      downloads.set(id, { steps: steps || [], item: null });
      if (steps === null) await new Promise(resolve => { start = resolve; });
      return id;
    },
    async search({ id }) { const d = downloads.get(id); if (!d) return []; if (d.steps.length) d.item = { id, ...d.steps.shift() }; return d.item ? [d.item] : []; },
    async cancel(id) { calls.push(['cancel', id]); const d = downloads.get(id); if (d?.item?.state === 'in_progress') { d.item = { ...d.item, state: 'interrupted', error: 'USER_CANCELED' }; d.steps = []; } },
    async removeFile(id) { calls.push(['removeFile', id]); },
    async erase(query) { calls.push(['erase', query.id]); },
    onChanged: { addListener(fn) { changed.add(fn); }, removeListener(fn) { changed.delete(fn); } },
  },
};
await import('../src/service-worker.js');
const ask = (type, tab) => new Promise(resolve => listener({ type }, { tab: { id: tab }, url: 'https://www.midjourney.com/organize' }, resolve));
const save = (url, filename = requested) => new Promise(resolve => listener({ type: 'cdn-file', url, filename }, { tab: { id: 1 }, url: 'https://www.midjourney.com/organize' }, resolve));
const folder = 'mj_export_synthetic_20261105', requested = `${folder}/images/moodboard_b_0.jpeg`;
const saved = name => `/home/someone/Downloads/${folder}/images/${name}`;
const shortLink = 'https://s.mj.run/AbCdEfGhIjK', gate = 'Only local export blobs, the Midjourney image CDN or Midjourney short links may be downloaded';
const running = (extra = {}) => ({ state: 'in_progress', mime: 'image/png', finalUrl: 'https://cdn.example.invalid/u/x/y.png?sig=secret', danger: 'safe', filename: '', ...extra });
const done = (name, extra = {}) => running({ state: 'complete', filename: saved(name), fileSize: 2048, exists: true, ...extra });
const steps = (...list) => () => list;
const kinds = () => calls.map(([kind]) => kind);
// Lets the worker's pending promises run; mocked timers fire only on tick().
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
// Lets the first check run, then reports a change of state, as Chrome does when a download ends (nothing listens once the answer is known).
const settle = async answer => { await flush(); for (const fn of [...changed]) fn({ id: downloadId, state: { previous: 'in_progress', current: 'complete' } }); return answer; };
// A download that never settles fails its test instead of hanging the run.
const limit = { timeout: 10000 };
const misplaced = (filename, name) => `Chrome saved ${filename} as ${name}, not under that name in the export folder; another extension may be renaming downloads, or another name or folder was chosen in Chrome's save dialog (setting "Ask where to save each file before downloading")`;

test('another tab cannot take the run lease while a run or the chooser holds it', async () => {
  assert.deepEqual(await ask('acquire', 1), { ok: true });
  holder = { active: true, choosing: true };
  assert.deepEqual(await ask('acquire', 2), { error: 'Finish choosing first: Save or Cancel the open choice in your other Midjourney tab.' });
  holder = { active: true, choosing: false };
  assert.deepEqual(await ask('acquire', 2), { error: 'Another MJ Export tab is already running' });
  assert.equal(session.lease, 1);
  // A tab that finished, or that was closed, no longer holds it.
  holder = { active: false, choosing: false };
  assert.deepEqual(await ask('acquire', 2), { ok: true });
  holder = new Error('Could not establish connection. Receiving end does not exist.');
  assert.deepEqual(await ask('acquire', 3), { ok: true });
  assert.deepEqual(await ask('release', 3), { ok: true });
  assert.equal(session.lease, undefined);
});

test('a short link is downloaded once, under the suffix Chrome gives its type, with where it led and no query', limit, async () => {
  calls.length = 0;
  script = steps(running(), done('moodboard_b_0.png'));
  const answer = save(shortLink);
  await new Promise(resolve => setTimeout(resolve, 5));
  for (const fn of [...changed]) fn({ id: downloadId, state: { previous: 'in_progress', current: 'complete' } });
  assert.deepEqual(await answer, { status: 200, id: downloadId, bytes: 2048, filename: saved('moodboard_b_0.png'), final_url: 'https://cdn.example.invalid/u/x/y.png' });
  assert.deepEqual(calls, [['download', { url: shortLink, filename: requested, saveAs: false, conflictAction: 'overwrite' }]]);
  assert.equal(changed.size, 0);
  // A Windows path is the same name in the same folder.
  script = steps(done('x', { filename: `C:\\Users\\someone\\Downloads\\${folder}\\images\\moodboard_b_0.webp`, mime: 'image/webp' }));
  assert.equal((await save(shortLink)).status, 200);
});

test('anything but a short link or the image CDN is refused before Chrome is asked', limit, async () => {
  calls.length = 0;
  for (const url of ['https://example.invalid/p.png', 'http://s.mj.run/AbCdEfGhIjK', 'https://s.mj.run.example.invalid/AbCdEfGhIjK', 'https://s.mj.run/AbCdEfGhIjK?x=1', 'https://s.mj.run/a/b', 'https://user@s.mj.run/AbCdEfGhIjK'])
    assert.deepEqual(await save(url), { status: 0, error_detail: gate }, url);
  assert.deepEqual(calls, []);
});

test('a short link that leads to something other than an HTTPS picture is removed and fails alone', limit, async () => {
  const notPicture = type => `Short link led to ${type}, not a JPEG, PNG, WebP, GIF or AVIF picture; not kept`;
  // A type that is not a picture ends the download as soon as it shows, before the file is complete, and is answered as a server failure, which the
  // content script retries: a server's error page shows as its type first (Chrome reports its error code only once it has named the file).
  const cases = [
    [running({ mime: 'text/html', finalUrl: 'https://www.example.invalid/gone' }), 503, ['cancel', 'erase'], notPicture('text/html at www.example.invalid')],
    [done('moodboard_b_0.txt', { mime: 'text/plain', finalUrl: 'https://www.example.invalid/gone' }), 503, ['cancel', 'removeFile', 'erase'], notPicture('text/plain at www.example.invalid')],
    [running({ mime: 'image/svg+xml' }), 503, ['cancel', 'erase'], notPicture('image/svg+xml at cdn.example.invalid')],
    [running({ mime: 'application/octet-stream' }), 503, ['cancel', 'erase'], notPicture('application/octet-stream at cdn.example.invalid')],
    [running({ mime: 'Application/X-MSDownload; charset=binary' }), 503, ['cancel', 'erase'], notPicture('application/x-msdownload at cdn.example.invalid')],
    // An address that is not HTTPS, or a danger verdict, ends it too, with no retry.
    [running({ finalUrl: 'http://cdn.example.invalid/y.png' }), 0, ['cancel', 'erase'], 'Short link led to a non-HTTPS address at cdn.example.invalid; not kept'],
    [running({ danger: 'url' }), 0, ['cancel', 'erase'], "Chrome flagged the short link's download as dangerous (url) at cdn.example.invalid; cancelled"],
    // A type and an address must be known once it is complete.
    [done('moodboard_b_0.png', { mime: '' }), 503, ['cancel', 'removeFile', 'erase'], notPicture('a response with no type at cdn.example.invalid')],
    [done('moodboard_b_0.png', { finalUrl: '' }), 0, ['cancel', 'removeFile', 'erase'], 'Short link led to a non-HTTPS address; not kept'],
    // The type a server sent is shortened in the reason, which the export keeps.
    [running({ mime: `x/${'a'.repeat(100000)}` }), 503, ['cancel', 'erase'], notPicture(`x/${'a'.repeat(98)}… at cdn.example.invalid`)],
  ];
  for (const [item, status, removal, reason] of cases) {
    calls.length = 0;
    script = steps(item);
    assert.deepEqual(await save(shortLink), { status, item: true, error_detail: reason }, reason.slice(0, 80));
    assert.deepEqual(kinds(), ['download', ...removal], reason.slice(0, 80));
  }
  // Once Chrome has named the file it reports the type of the suffix it saved under (image/jpeg for a .jpeg it kept): a server's own type that shows
  // only as the earlier value of a change, even one reported before Chrome answered with the download's id, still counts.
  calls.length = 0;
  script = steps(running({ mime: 'image/jpeg' }), running({ mime: 'image/jpeg' }));
  let answer = save(shortLink);
  await flush();
  for (const fn of [...changed]) fn({ id: downloadId, mime: { previous: 'application/octet-stream', current: 'image/jpeg' } });
  assert.deepEqual(await answer, { status: 503, item: true, error_detail: notPicture('application/octet-stream at cdn.example.invalid') });
  assert.deepEqual(kinds(), ['download', 'cancel', 'erase']);
  script = () => null;
  answer = save(shortLink);
  await flush();
  for (const fn of [...changed]) {
    fn({ id: downloadId + 1, mime: { previous: 'application/pdf', current: 'image/jpeg' } }); // another download's change is ignored
    fn({ id: downloadId, mime: { previous: 'text/html', current: 'image/jpeg' } });
  }
  downloads.get(downloadId).steps = [running({ mime: 'image/jpeg' })];
  start();
  assert.deepEqual(await answer, { status: 503, item: true, error_detail: notPicture('text/html at cdn.example.invalid') });
  // A picture type throughout is kept, under the suffix Chrome gave it.
  script = steps(running({ mime: 'image/png' }), done('moodboard_b_0.jpeg', { mime: 'image/jpeg' }));
  assert.equal((await settle(save(shortLink))).status, 200);
  // While Chrome scans a download, it waits for the verdict.
  for (const danger of ['asyncScanning', 'asyncLocalPasswordScanning']) {
    script = steps(running({ danger }), done('moodboard_b_0.png'));
    const answer = save(shortLink);
    await flush();
    for (const fn of [...changed]) fn({ id: downloadId, state: { previous: 'in_progress', current: 'complete' } });
    assert.equal((await answer).status, 200, danger);
  }
});

test('Chrome errors fail a short link alone and erase its partial download, keep their CDN meaning, and local errors still stop the run', limit, async () => {
  for (const [code, status] of [['SERVER_BAD_CONTENT', 404], ['SERVER_FAILED', 503], ['SERVER_FORBIDDEN', 403], ['SERVER_UNAUTHORIZED', 403], ['NETWORK_FAILED', 503], ['SERVER_CERT_PROBLEM', 0], ['FILE_VIRUS_INFECTED', 0], ['FILE_BLOCKED', 0], ['FILE_SECURITY_CHECK_FAILED', 0], ['FILE_TOO_LARGE', 0]]) {
    calls.length = 0;
    script = steps({ state: 'interrupted', error: code, finalUrl: shortLink, mime: '', danger: 'safe' });
    assert.deepEqual(await save(shortLink), { status, item: true, error_detail: `Short link could not be downloaded: Chrome reported ${code} at s.mj.run` }, code);
    // Erasing an interrupted download also deletes the partial file Chrome keeps for a resume.
    assert.deepEqual(kinds(), ['download', 'cancel', 'erase'], code);
    calls.length = 0;
    script = steps({ state: 'interrupted', error: code, finalUrl: 'https://cdn.midjourney.com/x/0_0.jpeg', mime: '', danger: 'safe' });
    assert.deepEqual(await save('https://cdn.midjourney.com/x/0_0.jpeg'), { status, error_detail: `Download interrupted: ${code}` }, code);
    assert.deepEqual(kinds(), ['download'], code);
  }
  for (const code of ['FILE_NO_SPACE', 'FILE_ACCESS_DENIED', 'USER_CANCELED', 'USER_SHUTDOWN', 'CRASH']) {
    calls.length = 0;
    script = steps({ state: 'interrupted', error: code, finalUrl: shortLink, mime: '', danger: 'safe' });
    assert.deepEqual(await save(shortLink), { status: 0, error_detail: `Download interrupted: ${code}` }, code);
    assert.deepEqual(kinds(), ['download', 'cancel', 'erase'], code);
  }
});

test('a server error page is refused by its type before Chrome reports its error code, and retried like a server error', limit, async () => {
  calls.length = 0;
  const page = { mime: 'text/html', finalUrl: 'https://www.example.invalid/gone' };
  script = steps(running(page), { ...running(page), state: 'interrupted', error: 'SERVER_FORBIDDEN' });
  assert.deepEqual(await settle(save(shortLink)), { status: 503, item: true, error_detail: 'Short link led to text/html at www.example.invalid, not a JPEG, PNG, WebP, GIF or AVIF picture; not kept' });
  assert.deepEqual(kinds(), ['download', 'cancel', 'erase']);
});

test('an error code Chrome gives when it cannot start a download is mapped as an interruption', limit, async () => {
  script = () => new Error('NETWORK_FAILED');
  assert.deepEqual(await save(shortLink), { status: 503, item: true, error_detail: 'Short link could not be downloaded: Chrome reported NETWORK_FAILED' });
  assert.deepEqual(await save('https://cdn.midjourney.com/x/0_0.jpeg'), { status: 503, error_detail: 'Download interrupted: NETWORK_FAILED' });
  script = () => new Error('Invalid filename');
  assert.deepEqual(await save(shortLink), { status: 0, error_detail: 'Invalid filename' });
  assert.equal(changed.size, 0);
});

test('a picture Chrome saved under another name or folder stops the run and is left where it is', limit, async () => {
  // Words of Chrome's error codes in the folder (from the username) or in the saved name do not change that.
  for (const [filename, path] of [[requested, '/home/someone/Downloads/AbCdEfGhIjK.png'], ['mj_export_NETWORK_FORBIDDEN_20261105/images/moodboard_b_0.jpeg', '/home/someone/Downloads/0_0.jpeg'], [requested, '/home/someone/Downloads/SERVER_FAILED_BAD_CONTENT_UNAUTHORIZED.png']]) {
    for (const url of [shortLink, 'https://cdn.midjourney.com/u/x/y.jpg']) {
      calls.length = 0;
      script = steps(done('x', { filename: path }));
      assert.deepEqual(await save(url, filename), { status: 0, error_detail: misplaced(filename, path.split('/').pop()) }, `${url} ${path}`);
      assert.deepEqual(kinds(), ['download']);
    }
  }
});

test('a CDN picture is recorded under the name Chrome gave it, and nothing else about CDN downloads changes', limit, async () => {
  script = steps(done('moodboard_b_0.webp', { mime: 'image/webp', finalUrl: 'https://cdn.midjourney.com/u/x/y.jpg', danger: 'uncommon' }));
  assert.deepEqual(await save('https://cdn.midjourney.com/u/x/y.jpg', `${folder}/images/moodboard_b_0.jpg`), { status: 200, id: downloadId, bytes: 2048, filename: saved('moodboard_b_0.webp') });
});

test('a short link that does not finish within 2 minutes is cancelled and fails alone', limit, async () => {
  calls.length = 0;
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  try {
    script = () => Array.from({ length: 100 }, () => running());
    const answer = save(shortLink);
    await flush();
    mock.timers.tick(119999);
    await flush();
    assert.deepEqual(kinds().filter(kind => kind !== 'download'), []);
    mock.timers.tick(1);
    assert.deepEqual(await answer, { status: 0, item: true, error_detail: 'Short link download did not finish within 2 minutes at cdn.example.invalid; cancelled' });
    assert.deepEqual(kinds(), ['download', 'cancel', 'erase']);
  } finally { mock.timers.reset(); }
});

test('check-file says where the recorded download was saved', limit, async () => {
  script = steps(done('moodboard_b_0.png'));
  const { id } = await save(shortLink);
  assert.deepEqual(await new Promise(resolve => listener({ type: 'check-file', id }, { tab: { id: 1 }, url: 'https://www.midjourney.com/organize' }, resolve)), { complete: true, bytes: 2048, filename: saved('moodboard_b_0.png') });
});

test('a short link whose server never answers is given up after 2 minutes, and its download is removed once Chrome creates it', limit, async () => {
  calls.length = 0;
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  try {
    script = () => null;
    const answer = save(shortLink);
    await flush();
    mock.timers.tick(120000);
    assert.deepEqual(await answer, { status: 0, item: true, error_detail: 'Short link download did not finish within 2 minutes; cancelled' });
    downloads.get(downloadId).steps = [running()];
    start();
    await flush();
    assert.deepEqual(kinds(), ['download', 'cancel', 'erase']);
    assert.equal(changed.size, 0);
  } finally { mock.timers.reset(); }
});

test('a short link is checked every 2 seconds, and at once on any change Chrome reports', limit, async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  try {
    // Polling alone sees it finish.
    let answer;
    script = steps(running(), running(), done('moodboard_b_0.png'));
    save(shortLink).then(value => { answer = value; });
    await flush();
    mock.timers.tick(2000);
    await flush();
    assert.equal(answer, undefined);
    mock.timers.tick(2000);
    await flush();
    assert.equal(answer?.status, 200);
    // A change that is not of the state, such as a danger verdict, is checked before the next poll.
    answer = undefined;
    script = steps(running(), running({ danger: 'url' }));
    save(shortLink).then(value => { answer = value; });
    await flush();
    for (const fn of [...changed]) fn({ id: downloadId, danger: { previous: 'safe', current: 'url' } });
    await flush();
    assert.deepEqual(answer, { status: 0, item: true, error_detail: "Chrome flagged the short link's download as dangerous (url) at cdn.example.invalid; cancelled" });
  } finally { mock.timers.reset(); }
});

test('a CDN picture has no time limit', limit, async () => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  try {
    let answer;
    script = steps(running({ mime: 'image/jpeg', finalUrl: 'https://cdn.midjourney.com/u/x/y.jpg' }), done('moodboard_b_0.jpg', { mime: 'image/jpeg', finalUrl: 'https://cdn.midjourney.com/u/x/y.jpg' }));
    save('https://cdn.midjourney.com/u/x/y.jpg', `${folder}/images/moodboard_b_0.jpg`).then(value => { answer = value; });
    await flush();
    mock.timers.tick(120000);
    await flush();
    assert.equal(answer, undefined);
    for (const fn of [...changed]) fn({ id: downloadId, state: { previous: 'in_progress', current: 'complete' } });
    await flush();
    assert.equal(answer?.status, 200);
  } finally { mock.timers.reset(); }
});

test('a short link whose final address cannot be read stops the run instead of waiting forever', limit, async () => {
  let answer;
  script = steps(done('moodboard_b_0.png', { finalUrl: 'https://' }));
  save(shortLink).then(value => { answer = value; });
  await flush();
  assert.equal(answer?.status, 0);
  assert.equal(answer.item, undefined);
  assert.equal(changed.size, 0);
});
