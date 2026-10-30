import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
const root = fileURLToPath(new URL('../', import.meta.url));
const read = name => JSON.parse(fs.readFileSync(path.join(root, name), 'utf8'));
const manifest = read('manifest.json');
assert.equal(manifest.manifest_version, 3);
assert.equal(manifest.version, read('package.json').version);
assert.deepEqual(manifest.host_permissions, ['https://www.midjourney.com/*', 'https://cdn.midjourney.com/*']);
assert(!manifest.permissions.includes('alarms'));
for (const name of [manifest.background.service_worker, manifest.action.default_popup, ...manifest.content_scripts.flatMap(s => s.js)]) {
  assert(fs.existsSync(path.join(root, name)), `Missing manifest resource: ${name}`);
}
for (const dir of ['src', 'lib', 'ui', 'scripts', 'tests']) {
  for (const name of fs.readdirSync(path.join(root, dir), { recursive: true })) {
    if (/\.(m?js)$/.test(name)) execFileSync(process.execPath, ['--check', path.join(root, dir, name)]);
  }
}
console.log('Manifest, versions, generated bundle, and JavaScript syntax checked.');
