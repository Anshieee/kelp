import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac, randomBytes } from 'node:crypto';
import { decryptProviderKey, encryptProviderKey, validateOpenRouter, verifyIdentity } from '../../netlify/functions/demo-core.mjs';

test('provider key encryption is authenticated and round-trips only with the server key', () => {
  process.env.DEMO_KEY_ENCRYPTION_SECRET = randomBytes(32).toString('base64');
  const encrypted = encryptProviderKey('sk-or-v1-a-valid-looking-key-value_1234567890');
  assert.equal(JSON.stringify(encrypted).includes('sk-or-v1'), false);
  assert.equal(decryptProviderKey({ key_ciphertext: encrypted.ciphertext, key_iv: encrypted.iv, key_tag: encrypted.tag }), 'sk-or-v1-a-valid-looking-key-value_1234567890');
  assert.throws(() => decryptProviderKey({ ...encrypted, key_ciphertext: Buffer.alloc(20).toString('base64'), key_iv: encrypted.iv, key_tag: encrypted.tag }));
});

test('managed identity verifies HS256 signatures and rejects expired or forged tokens', async () => {
  process.env.NETLIFY_IDENTITY_JWT_SECRET = 'test-netlify-secret';
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
  const header = encode({ alg: 'HS256', typ: 'JWT' });
  const payload = encode({ sub: 'verified-subject', exp: Math.floor(Date.now() / 1000) + 60, email: 'demo@example.test' });
  const signature = createHmac('sha256', process.env.NETLIFY_IDENTITY_JWT_SECRET).update(`${header}.${payload}`).digest('base64url');
  const token = `${header}.${payload}.${signature}`;
  assert.equal((await verifyIdentity({ headers: { authorization: `Bearer ${token}` } })).subject, 'verified-subject');
  assert.equal(await verifyIdentity({ headers: { authorization: `Bearer ${token}x` } }), null);
  const expired = `${header}.${encode({ sub: 'old', exp: 1 })}.${signature}`;
  assert.equal(await verifyIdentity({ headers: { authorization: `Bearer ${expired}` } }), null);
  const noExpiryBody = encode({ sub: 'forever' });
  const noExpirySignature = createHmac('sha256', process.env.NETLIFY_IDENTITY_JWT_SECRET).update(`${header}.${noExpiryBody}`).digest('base64url');
  assert.equal(await verifyIdentity({ headers: { authorization: `Bearer ${header}.${noExpiryBody}.${noExpirySignature}` } }), null);
});

test('OpenRouter validation uses a bounded tiny inference and never returns key material', async (t) => {
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  let seen;
  globalThis.fetch = async (url, init) => {
    seen = { url, init, body: JSON.parse(init.body) };
    return { ok: true, json: async () => ({ choices: [{ message: { content: 'OK' } }] }) };
  };
  const key = 'sk-or-v1-' + 'x'.repeat(24);
  assert.equal(await validateOpenRouter(key, 'openai/gpt-4o-mini'), 'openai/gpt-4o-mini');
  assert.equal(seen.url, 'https://openrouter.ai/api/v1/chat/completions');
  assert.equal(seen.init.headers.Authorization, `Bearer ${key}`);
  assert.equal(seen.body.max_tokens, 2);
  await assert.rejects(validateOpenRouter('bad-key', 'openai/gpt-4o-mini'));
});
