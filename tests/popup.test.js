import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setTimeout(resolve, 0)); };

// Simulated popup DOM and Chrome, with timers that fire only when a test says so. No network.
function popup(stored = {}) {
  const state = structuredClone(stored), sent = [], timers = new Map();
  let next = 0;
  const node = () => {
    const classes = new Set();
    return {
      textContent: '', value: '', checked: false, type: '', children: [],
      classList: { add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name) },
      append(...items) { this.children.push(...items); },
    };
  };
  const ids = Object.fromEntries(['mode', 'max', 'since', 'images', 'testSubset', 'selection', 'choice', 'actions', 'status'].map(id => [id, node()]));
  Object.assign(ids.mode, { value: 'all' });
  Object.assign(ids.images, { type: 'checkbox', checked: true });
  Object.assign(ids.testSubset, { type: 'checkbox' });
  const chrome = {
    runtime: { async sendMessage(message) { sent.push(message); return { ok: true }; } },
    storage: {
      local: { async get() { return structuredClone(state); }, async remove(key) { delete state[key]; } },
      onChanged: { addListener() {} },
    },
  };
  const context = vm.createContext({
    chrome, document: { getElementById: id => ids[id], createElement: () => node() },
    setTimeout: (fn, ms) => { timers.set(++next, { fn, ms }); return next; }, clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync(new URL('../src/generated/core.js', import.meta.url), 'utf8'), context);
  vm.runInContext(fs.readFileSync(new URL('../ui/popup.js', import.meta.url), 'utf8'), context);
  return {
    state, sent, ids,
    button: label => [...ids.choice.children, ...ids.actions.children].find(button => button.textContent === label),
    fire(ms) { for (const [id, timer] of [...timers]) if (timer.ms === ms) { timers.delete(id); timer.fn(); } },
  };
}
const selection = { user_id: 'self', scope: { mode: 'all', max: 20, since: '' }, chosen_at: '2026-02-01T00:00:00.000Z', jobs: { a: true, b: false }, moodboards: { 101: false } };

test('Clear selection in the popup asks first, and only a second click forgets the choice', async () => {
  const app = popup({ selection });
  await settle();
  assert.match(app.ids.selection.textContent, /^What to export: 0 of 1 moodboard · 1 of 2 generations/);
  const clear = app.button('Clear selection');
  clear.onclick();
  await settle();
  assert.equal(clear.textContent, 'Forget my choice?');
  assert(clear.classList.contains('confirm'));
  assert.deepEqual(app.state.selection, selection);
  // Unanswered, it turns back after a few seconds, and the next click asks again.
  app.fire(5000);
  assert.equal(clear.textContent, 'Clear selection');
  assert(!clear.classList.contains('confirm'));
  clear.onclick();
  await settle();
  assert.deepEqual(app.state.selection, selection);
  clear.onclick();
  await settle();
  assert.equal(clear.textContent, 'Clear selection');
  assert.equal(app.state.selection, undefined);
  assert.equal(app.ids.status.textContent, 'Selection cleared. New exports include everything in the scope again.');
  // Clearing needs no Midjourney tab.
  assert.deepEqual(app.sent, []);
});

test('the panel, the popup and the chooser never open a browser dialog', () => {
  for (const file of ['../src/content.js', '../src/chooser.js', '../ui/popup.js']) {
    const source = fs.readFileSync(new URL(file, import.meta.url), 'utf8');
    assert(!/\b(alert|confirm|prompt)\(/.test(source), `${file} opens a browser dialog`);
  }
});
