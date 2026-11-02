import test from 'node:test';
import assert from 'node:assert/strict';

// Simulated Chrome for the service worker's run lease: these tests never access Midjourney.
const session = {};
let listener, holder = null;
globalThis.chrome = {
  runtime: { id: 'test', onMessage: { addListener(fn) { listener = fn; } } },
  storage: { session: {
    async get(key) { return { [key]: session[key] }; },
    async set(value) { Object.assign(session, value); },
    async remove(key) { delete session[key]; },
  } },
  tabs: { async sendMessage(tab, message) { if (holder instanceof Error) throw holder; return message.type === 'run-state' ? holder : undefined; } },
};
await import('../src/service-worker.js');
const ask = (type, tab) => new Promise(resolve => listener({ type }, { tab: { id: tab }, url: 'https://www.midjourney.com/organize' }, resolve));

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
