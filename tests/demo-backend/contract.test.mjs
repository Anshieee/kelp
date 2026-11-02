import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { handler as apiHandler } from '../../netlify/functions/api-v1.mjs';
import { handler as workerHandler } from '../../netlify/functions/demo-worker.mjs';

test('the frozen v1 user facade is routed to Netlify Functions', async () => {
  const toml = await readFile(new URL('../../netlify.toml', import.meta.url), 'utf8');
  const api = await readFile(new URL('../../netlify/functions/api-v1.mjs', import.meta.url), 'utf8');
  for (const path of ['/me', '/me/provider', '/me/onboarding/complete', '/workspace', '/tasks', 'candidate', 'decision', 'cancel', 'events']) assert.ok(api.includes(path), `facade includes ${path}`);
  assert.match(toml, /from = "\/api\/v1\/\*"/);
  assert.match(toml, /api-v1\/:splat/);
});

test('user endpoints reject missing and invalid identity before accessing persistence', async () => {
  const missing = await apiHandler({ httpMethod: 'GET', path: '/api/v1/me', headers: {} });
  assert.equal(missing.statusCode, 401);
  process.env.NETLIFY_IDENTITY_JWT_SECRET = 'test-secret';
  const forged = await apiHandler({ httpMethod: 'GET', path: '/api/v1/me', headers: { authorization: 'Bearer not.a.jwt' } });
  assert.equal(forged.statusCode, 401);
  assert.equal(JSON.parse(missing.body).ok, false);
  assert.equal(missing.headers['cache-control'], 'no-store');
});

test('worker-only endpoints reject user access tokens and do not disclose operational details', async () => {
  process.env.DEMO_WORKER_TOKEN = 'w'.repeat(48);
  const result = await workerHandler({ httpMethod: 'POST', path: '/.netlify/functions/demo-worker', rawUrl: 'https://demo.test/.netlify/functions/demo-worker?action=claim', headers: { authorization: 'Bearer user-token' }, body: '{}' });
  assert.equal(result.statusCode, 401);
  assert.equal(JSON.parse(result.body).error.code, 'worker_auth_required');
  assert.equal(result.headers['cache-control'], 'no-store');
});

test('workflow uses separate read and publish credentials and only accepts action dispatches', async () => {
  const workflow = await readFile(new URL('../../.github/workflows/kelp-demo.yml', import.meta.url), 'utf8');
  assert.match(workflow, /types: \[kelp-demo-prepare, kelp-demo-publish\]/);
  assert.match(workflow, /DEMO_READ_TOKEN/);
  assert.match(workflow, /DEMO_REPO_TOKEN/);
  assert.match(workflow, /DEMO_WORKER_TOKEN/);
  assert.doesNotMatch(workflow, /api_key:|provider_key:/i);
});
