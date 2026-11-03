import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import api, { handler } from '../../netlify/functions/api-v1.mjs';

const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
function session(secret) {
  const header = encode({ alg: 'HS256', typ: 'JWT' });
  const body = encode({ sub: 'configuration-test-user', exp: Math.floor(Date.now() / 1000) + 60 });
  return `${header}.${body}.${createHmac('sha256', secret).update(`${header}.${body}`).digest('base64url')}`;
}

test('missing server settings report the exact setting without exposing tokens or values', async (t) => {
  const previousIdentity = process.env.NETLIFY_IDENTITY_JWT_SECRET;
  const previousDatabase = process.env.DATABASE_URL;
  const previousError = console.error;
  const logs = [];
  console.error = (...args) => logs.push(args);
  t.after(() => {
    console.error = previousError;
    if (previousIdentity === undefined) delete process.env.NETLIFY_IDENTITY_JWT_SECRET;
    else process.env.NETLIFY_IDENTITY_JWT_SECRET = previousIdentity;
    if (previousDatabase === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = previousDatabase;
  });
  const secret = 'local-test-signing-value';
  const token = session(secret);
  const event = { httpMethod: 'GET', path: '/api/v1/me', headers: { authorization: `Bearer ${token}` } };
  delete process.env.NETLIFY_IDENTITY_JWT_SECRET;
  delete process.env.DATABASE_URL;

  const identity = await handler(event);
  assert.equal(identity.statusCode, 503);
  assert.equal(JSON.parse(identity.body).error.code, 'configuration_missing');
  assert.match(JSON.parse(identity.body).error.message, /NETLIFY_IDENTITY_JWT_SECRET/);
  assert.equal(JSON.parse(identity.body).error.retryable, false);
  assert.deepEqual(logs.at(-1), ['demo API request failed', {
    name: 'ConfigurationError', stage: 'identity', setting: 'NETLIFY_IDENTITY_JWT_SECRET',
  }]);

  process.env.NETLIFY_IDENTITY_JWT_SECRET = secret;
  const database = await handler(event);
  assert.equal(database.statusCode, 503);
  assert.equal(JSON.parse(database.body).error.code, 'configuration_missing');
  assert.match(JSON.parse(database.body).error.message, /DATABASE_URL/);
  assert.equal(logs.at(-1)[1].stage, 'database');
  assert.equal(logs.at(-1)[1].setting, 'DATABASE_URL');
  for (const value of [identity.body, database.body, JSON.stringify(logs)]) {
    assert.ok(!value.includes(secret));
    assert.ok(!value.includes(token));
  }
});

test('malformed bearer tokens remain unauthorized even before Identity is configured', async (t) => {
  const previous = process.env.NETLIFY_IDENTITY_JWT_SECRET;
  t.after(() => {
    if (previous === undefined) delete process.env.NETLIFY_IDENTITY_JWT_SECRET;
    else process.env.NETLIFY_IDENTITY_JWT_SECRET = previous;
  });
  delete process.env.NETLIFY_IDENTITY_JWT_SECRET;
  for (const token of ['', 'invalid-diagnostic-token', 'not.a.jwt']) {
    const response = await handler({ httpMethod: 'GET', path: '/api/v1/me', headers: { authorization: `Bearer ${token}` } });
    assert.equal(response.statusCode, 401);
    assert.equal(JSON.parse(response.body).error.code, 'unauthorized');
  }
});

test('deployed entrypoint uses managed Identity instead of requiring a shared signing secret', async (t) => {
  const previousContext = globalThis.netlifyIdentityContext;
  const previousIdentity = process.env.NETLIFY_IDENTITY_JWT_SECRET;
  const previousDatabase = process.env.DATABASE_URL;
  const previousError = console.error;
  console.error = () => {};
  t.after(() => {
    console.error = previousError;
    if (previousContext === undefined) delete globalThis.netlifyIdentityContext;
    else globalThis.netlifyIdentityContext = previousContext;
    if (previousIdentity === undefined) delete process.env.NETLIFY_IDENTITY_JWT_SECRET;
    else process.env.NETLIFY_IDENTITY_JWT_SECRET = previousIdentity;
    if (previousDatabase === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = previousDatabase;
  });
  delete process.env.NETLIFY_IDENTITY_JWT_SECRET;
  delete process.env.DATABASE_URL;
  // Only the Netlify runtime supplies this verified context, never request JSON.
  globalThis.netlifyIdentityContext = { user: { sub: 'managed-user', exp: Math.floor(Date.now() / 1000) + 60 } };
  const response = await api(new Request('https://demo.test/api/v1/me', {
    headers: { authorization: 'Bearer platform-verified-token' },
  }));
  assert.equal(response.status, 503);
  assert.match((await response.json()).error.message, /DATABASE_URL/);

  globalThis.netlifyIdentityContext = {};
  const forged = await api(new Request('https://demo.test/api/v1/me', {
    headers: { authorization: 'Bearer attacker-token', 'x-user-id': 'managed-user' },
  }));
  assert.equal(forged.status, 401);
});

test('managed cookie sessions still require CSRF protection for mutations', async (t) => {
  const previous = globalThis.netlifyIdentityContext;
  t.after(() => {
    if (previous === undefined) delete globalThis.netlifyIdentityContext;
    else globalThis.netlifyIdentityContext = previous;
  });
  globalThis.netlifyIdentityContext = { user: { sub: 'managed-cookie-user' } };
  const response = await api(new Request('https://demo.test/api/v1/me/onboarding/complete', {
    method: 'POST', headers: { cookie: 'nf_jwt=cookie-session' }, body: '{}',
  }));
  assert.equal(response.status, 403);
  assert.equal((await response.json()).error.code, 'csrf_failed');
});
