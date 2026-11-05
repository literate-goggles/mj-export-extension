import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { isShortLink } from '../lib/transport.js';

// The page bridge in a simulated Organize page with synthetic responses: these tests never access Midjourney.
async function bridge(moodboards) {
  const listeners = [], posted = [];
  const json = data => ({ ok: true, status: 200, headers: { get: name => (name.toLowerCase() === 'content-type' ? 'application/json' : null) }, json: async () => structuredClone(data), clone() { return this; } });
  const window = {
    fetch: async url => json(new URL(String(url)).pathname === '/api/moodboards' ? moodboards : { data: [], cursor: null, checkpoint: null }),
    addEventListener: (type, fn) => { if (type === 'message') listeners.push(fn); },
    postMessage: message => posted.push(message),
  };
  const context = vm.createContext({ window, URL, setTimeout, location: { origin: 'https://www.midjourney.com', pathname: '/organize' }, document: { body: { innerText: '' } } });
  vm.runInContext(fs.readFileSync(new URL('../src/page-bridge.js', import.meta.url), 'utf8'), context);
  await window.fetch('https://www.midjourney.com/api/imagine?user_id=self&page_size=20'); // the site's own feed request names the account
  let n = 0;
  const ask = async (op, args = {}) => {
    const id = String(++n);
    await Promise.all(listeners.map(fn => fn({ source: window, origin: 'https://www.midjourney.com', data: { source: 'mj-export-request', id, op, args } })));
    return { ...posted.find(message => message.source === 'mj-export-response' && message.id === id).result }; // a plain object of this realm
  };
  return ask;
}

// The cases of tests/transport.test.js and both length limits, so that a change to either copy of the pattern shows here.
const candidates = ['https://s.mj.run/AbCdEfGhIjK', 'https://s.mj.run/a-_9', 'http://s.mj.run/AbCdEfGhIjK', 'https://s.mj.run.example.invalid/AbCdEfGhIjK', 'https://s.mj.run/AbCdEfGhIjK?x=1', 'https://s.mj.run/AbCdEfGhIjK#x', 'https://s.mj.run/a/b', 'https://s.mj.run/AbCdEfGhIjK/', 'https://user@s.mj.run/AbCdEfGhIjK', 'https://s.mj.run:443/AbCdEfGhIjK', 'https://S.MJ.RUN/AbCdEfGhIjK', 'https://s.mj.run/', 'https://s.mj.run/AbCdEfGhIjK\n', `https://s.mj.run/${'a'.repeat(64)}`, `https://s.mj.run/${'a'.repeat(65)}`, 'https://s.mj.run/%41b', 'https://example.invalid/p.png'];

test('own-board short links are authorized exactly when lib/transport.js isShortLink accepts them', async () => {
  const ask = await bridge([
    { moodboard_id: 'own', user_id: 'self', title: 'Own', images: [...candidates, 'https://cdn.midjourney.com/u/x/y.jpg'].map(url => ({ url, width: 1200, height: 1500 })) },
    { moodboard_id: 'theirs', user_id: 'someone-else', title: 'Theirs', images: [{ url: 'https://s.mj.run/ZZZZZZZZZZZ' }] },
  ]);
  const read = await ask('moodboards');
  assert.equal(read.status, 200);
  assert.deepEqual([...read.data].map(b => b.moodboard_id), ['own']);
  for (const url of candidates) {
    const answer = await ask('authorize-image', { url });
    assert.equal(answer.status === 200, isShortLink(url), url);
    if (answer.status !== 200) assert.deepEqual(answer, { status: 0, error: 'Image not in this account’s observed export records' });
  }
  assert.deepEqual(await ask('authorize-image', { url: 'https://cdn.midjourney.com/u/x/y.jpg' }), { status: 200 });
  assert.equal((await ask('authorize-image', { url: 'https://s.mj.run/ZZZZZZZZZZZ' })).status, 0);
});
