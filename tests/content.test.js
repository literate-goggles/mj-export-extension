import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { randomUUID } from 'node:crypto';

// Simulated Chrome and page bridge: these tests never access Midjourney.
function harness(savedRun, pauseOnDownload = false) {
  const options = { mode: 'max', max: 20, since: '', images: true, testSubset: true };
  const state = savedRun ? { run: structuredClone(savedRun) } : {};
  let messageHandler;
  let downloadId = 0;
  let release;
  const finished = new Promise(resolve => { release = resolve; });
  const listeners = [];
  const window = {
    addEventListener: (_, fn) => listeners.push(fn),
    postMessage(message) {
      if (message.source !== 'mj-export-request') return;
      const data = {
        identity: { user_id: 'self', username_v2: 'synthetic' },
        jobs: { data: [], cursor: null, checkpoint: null },
        moodboards: [{ moodboard_id: 'board', title: 'Synthetic', images: [0, 1, 2].map(i => ({ url: `https://cdn.midjourney.com/test${i}.png` })) }],
        profiles: { profiles: [], counts: {} },
      }[message.op];
      queueMicrotask(() => listeners.forEach(fn => fn({
        source: window, origin: 'https://www.midjourney.com',
        data: { source: 'mj-export-response', id: message.id, result: { status: 200, data } },
      })));
    },
  };
  const chrome = {
    runtime: {
      onMessage: { addListener(fn) { messageHandler = fn; } },
      getManifest: () => ({ version: 'test' }),
      async sendMessage(message) {
        if (message.type === 'release') release();
        if (message.type === 'cdn-file') {
          if (pauseOnDownload) messageHandler({ type: 'command', command: 'pause', options }, {}, () => {});
          return { status: 200, id: ++downloadId, bytes: 10, filename: message.filename };
        }
        if (message.type === 'text-file') return { id: ++downloadId, filename: message.filename };
        return { ok: true };
      },
    },
    storage: { local: {
      async get() { return structuredClone(state); },
      async set(value) { Object.assign(state, structuredClone(value)); },
    } },
  };
  const context = vm.createContext({
    window, chrome, crypto: { randomUUID }, URL, structuredClone, queueMicrotask,
    setTimeout, clearTimeout,
    location: { origin: 'https://www.midjourney.com', pathname: '/organize' },
    document: { readyState: 'loading', addEventListener() {} },
  });
  vm.runInContext(fs.readFileSync(new URL('../src/generated/core.js', import.meta.url), 'utf8'), context);
  vm.runInContext('ReadScheduler.prototype.sleep = async () => {};', context);
  // Replace the default timer for scheduler waits without changing RPC timeouts.
  vm.runInContext('const OriginalScheduler = ReadScheduler; ReadScheduler = class extends OriginalScheduler { constructor() { super({sleep: async () => {}}); } };', context);
  vm.runInContext(fs.readFileSync(new URL('../src/content.js', import.meta.url), 'utf8'), context);
  return {
    state, options,
    async start(customOptions = options) {
      messageHandler({ type: 'command', command: 'export', options: customOptions }, {}, () => {});
      await finished;
    },
  };
}

test('scope rejection leaves saved export status and failures untouched', async () => {
  const saved = { options: { mode: 'max', max: 10, since: '', images: true, testSubset: true }, status: 'complete', failures: [] };
  const app = harness(saved);
  await app.start();
  assert.deepEqual(app.state.run, saved);
});

test('pausing during board downloads cannot report a complete export', async () => {
  const app = harness(null, true);
  await app.start();
  assert.equal(app.state.run.status, 'paused');
  assert(app.state.run.boards[0].images.filter(image => image.file).length < 3);
});
