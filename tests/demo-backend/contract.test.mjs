import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dispatchTask, handler as apiHandler } from '../../netlify/functions/api-v1.mjs';
import { handler as workerHandler, isGitCommitSha } from '../../netlify/functions/demo-worker.mjs';

test('candidate base is a real Git SHA-1 and remains distinct from the SHA-256 payload digest', () => {
  assert.equal(isGitCommitSha('a'.repeat(40)), true);
  assert.equal(isGitCommitSha('a'.repeat(64)), false);
  assert.equal(isGitCommitSha('a'.repeat(40).toUpperCase()), false);
});

test('dispatch failures are persisted as observable, retryable task events', async () => {
  const queries = [];
  const db = { query: async (...args) => { queries.push(args); return { rows: [] }; } };
  const response = await dispatchTask(db, 'task-123', 'publish', async () => {
    throw Object.assign(new Error('private token must not be exposed'), { status: 502 });
  });
  assert.equal(response.statusCode, 503);
  const body = JSON.parse(response.body);
  assert.equal(body.error.code, 'dispatch_failed');
  assert.equal(body.error.retryable, true);
  assert.equal(body.task_id, 'task-123');
  assert.doesNotMatch(response.body, /private token/);
  assert.equal(queries.length, 1);
  assert.equal(queries[0][1][1], 'dispatch_failed');
  assert.deepEqual(JSON.parse(queries[0][1][2]), {
    action: 'publish', state: 'dispatch_failed', retryable: true, status: 502,
  });
});

test('the frozen v1 user facade is routed to Netlify Functions', async () => {
  const toml = await readFile(new URL('../../netlify.toml', import.meta.url), 'utf8');
  const api = await readFile(new URL('../../netlify/functions/api-v1.mjs', import.meta.url), 'utf8');
  for (const path of ['/me', '/me/provider', '/me/onboarding/complete', '/workspace', '/tasks', 'candidate', 'decision', 'cancel', 'events']) assert.ok(api.includes(path), `facade includes ${path}`);
  assert.match(toml, /from = "\/api\/v1\/\*"/);
  assert.match(toml, /api-v1\/:splat/);
});

test('every route in the shared mobile contract is handled by the server', async () => {
  const contract = await readFile(new URL('../../docs/HACKATHON_DEMO_CONTRACT.md', import.meta.url), 'utf8');
  const api = await readFile(new URL('../../netlify/functions/api-v1.mjs', import.meta.url), 'utf8');
  const routes = [...contract.matchAll(/^\| `?(GET|PUT|POST) ([^`|]+)`? \|/gm)]
    .map(([, method, path]) => ({ method, path: path.split('?')[0] }));
  assert.ok(routes.length > 0, 'shared contract must declare user routes');

  const fixedRoutes = new Set([
    'GET /me',
    'PUT /me/provider',
    'POST /me/onboarding/complete',
    'GET /workspace',
    'POST /tasks',
  ]);
  const taskActions = new Map([
    ['GET /tasks/{id}', "if (!action && method === 'GET')"],
    ['GET /tasks/{id}/events', "if (action === 'events' && method === 'GET')"],
    ['GET /tasks/{id}/candidate', "if (action === 'candidate' && method === 'GET')"],
    ['POST /tasks/{id}/decision', "if (action === 'decision' && method === 'POST')"],
    ['POST /tasks/{id}/cancel', "if (action === 'cancel' && method === 'POST')"],
  ]);
  assert.match(api, /const taskRoute = path\.match\(\/\^\\\/tasks\\\/\(\[0-9a-f-\]\{36\}\)/i,
    'server must match task IDs before handling task routes');

  for (const { method, path } of routes) {
    const route = `${method} ${path}`;
    if (fixedRoutes.has(route)) {
      const [, verb, pathname] = route.match(/^(\w+) (.+)$/);
      assert.ok(api.includes(`path === '${pathname}' && method === '${verb}'`), `${route} must be handled server-side`);
      continue;
    }
    const taskHandler = taskActions.get(route);
    assert.ok(taskHandler, `${route} must be declared as a supported server route`);
    assert.ok(api.includes(taskHandler), `${route} must be handled server-side`);
  }
});

test('every API route called by the mobile fallback is supported by the server contract', async () => {
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const contract = await readFile(new URL('../../docs/HACKATHON_DEMO_CONTRACT.md', import.meta.url), 'utf8');
  const mobile = execFileSync('git', ['show', 'origin/feat/mobile-demo-fallback:netlify/mobile/app.js'], { cwd: root, encoding: 'utf8' });
  const supportedRoutes = new Set([...contract.matchAll(/^\| `?(GET|PUT|POST) ([^`|]+)`? \|/gm)]
    .map(([, method, path]) => `${method} ${path.split('?')[0]}`));
  const mobileRoutes = [...mobile.matchAll(/\bapi\(\s*(['"`])([^'"`]*)\1/g)].map((match) => {
    const rawPath = match[2];
    const path = rawPath.replace(/\$\{encodeURIComponent\([^)]*\)\}/g, '{id}').split('?')[0];
    const rest = mobile.slice(match.index + match[0].length, match.index + match[0].length + 120);
    const method = rest.match(/^\s*,\s*\{\s*method:\s*['"]([A-Z]+)['"]/s)?.[1] || 'GET';
    return `${method} ${path}`;
  });

  assert.ok(mobileRoutes.length > 0, 'mobile fallback must call the user API');
  for (const route of mobileRoutes) assert.ok(supportedRoutes.has(route), `${route} must be supported by the server`);
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
