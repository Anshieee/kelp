import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const read = (file) => readFile(new URL(`../${file}`, import.meta.url), 'utf8');

test('mobile app exposes the frozen v1 user routes without an invented history API', async () => {
  const [app, contract] = await Promise.all([read('netlify/mobile/app.js'), read('docs/HACKATHON_DEMO_CONTRACT.md')]);
  for (const route of ['/me', '/me/provider', '/me/onboarding/complete', '/workspace', '/tasks', '/candidate', '/events', '/decision']) assert.ok(app.includes(route), `client uses ${route}`);
  assert.match(contract, /facade has no workspace task-list\/history endpoint/);
  assert.doesNotMatch(app, /\/tasks\/(?:history|list)|\/workspace\/history/);
});

test('provider secret is masked, transient, and excluded from storage and logs', async () => {
  const [html, app, worker] = await Promise.all([read('netlify/mobile/index.html'), read('netlify/mobile/app.js'), read('netlify/mobile/sw.js')]);
  assert.match(html, /type="password"[^>]*autocomplete="new-password"/);
  assert.match(app, /api_key: secret/);
  assert.match(app, /input\.value = ''/);
  assert.doesNotMatch(app, /localStorage|console\.(?:log|error|warn)/);
  assert.match(worker, /kelp-mobile-public-shell-v3/);
  assert.doesNotMatch(worker, /caches\.open\([^)]*api|cache\.put\(/);
});

test('PWA shell has mobile viewport, safe area, manifest and install guidance', async () => {
  const [html, css, manifest] = await Promise.all([read('netlify/mobile/index.html'), read('netlify/mobile/app.css'), read('netlify/mobile/manifest.webmanifest')]);
  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /manifest\.webmanifest/);
  assert.match(html, /Add to Home Screen/);
  assert.match(css, /env\(safe-area-inset-top/);
  assert.match(css, /min-height:52px/);
  assert.match(manifest, /"display": "standalone"/);
});

test('review and result surfaces require service-provided candidate evidence and PR identity', async () => {
  const [html, app] = await Promise.all([read('netlify/mobile/index.html'), read('netlify/mobile/app.js')]);
  for (const field of ['base-sha', 'candidate-digest', 'checks', 'diff', 'pr-link', 'commit-sha']) assert.ok(html.includes(`id="${field}"`), `${field} is visible`);
  assert.match(app, /currentTask\.approval\?\.candidate_digest/);
  assert.match(app, /task\.pr_url/);
  assert.match(app, /client_decision_id: decisionId/);
  assert.match(app, /client_request_id: pendingSubmission\.id/);
  assert.match(app, /pendingSubmission = \{ prompt, id: crypto\.randomUUID/);
});

test('reconnect restores task state and does not claim unsupported desktop connectivity', async () => {
  const [html, app, contract] = await Promise.all([read('netlify/mobile/index.html'), read('netlify/mobile/app.js'), read('docs/HACKATHON_DEMO_CONTRACT.md')]);
  assert.match(app, /\/tasks\/\$\{encodeURIComponent\(taskId\)\}\/events\?after=/);
  assert.match(app, /kelp-mobile-cursor-v1:/);
  assert.match(app, /reconnect-safe/);
  assert.match(contract, /no computer-preview or\s+desktop-connectivity capability/);
  assert.doesNotMatch(html + app, /VNC|remote desktop|desktop connected/i);
});

test('reuse note records upstream palette adaptation and Apache-2.0 attribution', async () => {
  const note = await read('docs/MOBILE_REUSE_NOTES.md');
  assert.match(note, /f52effc1a5871e4e54b3d2c699336a28c6cb1f97/);
  assert.match(note, /Apache-2\.0/);
  assert.match(note, /palette/);
  assert.match(note, /third-party\/OpenMausBot/);
});
