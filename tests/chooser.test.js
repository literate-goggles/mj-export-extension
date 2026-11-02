import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

// The chooser runs inside the Midjourney page: prompts and names may reach its DOM only as text.
const source = fs.readFileSync(new URL('../src/chooser.js', import.meta.url), 'utf8');

test('the chooser never parses data as HTML', () => {
  for (const sink of ['innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'createContextualFragment', 'DOMParser', 'srcdoc', 'eval(', 'Function(']) {
    assert(!source.includes(sink), `src/chooser.js uses ${sink}`);
  }
  assert.match(source, /attachShadow\(\{mode:'closed'\}\)/);
});

test('the chooser loads images only from the Midjourney CDN', () => {
  assert.deepEqual([...new Set(source.match(/https:\/\/[a-z0-9.-]+/g))], ['https://cdn.midjourney.com']);
  assert.match(source, /JOB_ID=\/\^\[a-f0-9-\]\{36\}\$\/i/);
  assert.match(source, /if\(parsed\.origin===CDN\)src=parsed\.href/);
  assert(!/\bfetch\(|XMLHttpRequest|sendBeacon|WebSocket/.test(source), 'the chooser makes no requests of its own');
});

test('the chooser says when the list was read and that profiles are always exported', () => {
  assert(source.includes('`Your work as of ${asOf(scan.scanned_at)} · ${scopeLabel(scan.scope)}. Work you make later is not exported until you choose again.`'));
  assert(source.includes("'Your profiles are always exported: they are not part of this choice.'"));
});
