import { createCipheriv, createDecipheriv, createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import pg from 'pg';

const { Pool } = pg;
let pool;
let schemaReady;

export class ConfigurationError extends Error {
  constructor(setting) {
    super(`The demo server is missing ${setting}. Contact the demo owner.`);
    this.name = 'ConfigurationError';
    this.setting = setting;
  }
}

export function database() {
  if (!process.env.DATABASE_URL) throw new ConfigurationError('DATABASE_URL');
  if (!pool) pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 4, ssl: process.env.DATABASE_SSL === 'disable' ? false : { rejectUnauthorized: true } });
  return pool;
}

export async function ensureSchema(db = database()) {
  if (!schemaReady) schemaReady = db.query(`
    CREATE TABLE IF NOT EXISTS demo_users (
      subject text PRIMARY KEY, provider text NOT NULL DEFAULT 'openrouter', key_ciphertext text,
      key_iv text, key_tag text, model text, onboarding_complete boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS demo_tasks (
      id uuid PRIMARY KEY, owner text NOT NULL REFERENCES demo_users(subject), client_request_id text NOT NULL,
      prompt text NOT NULL, repo text NOT NULL, state text NOT NULL, base_sha text, summary text,
      test_outcome jsonb, candidate jsonb, candidate_digest text, approval_digest text,
      approval_expires_at timestamptz, decision_id text, decision text, pr_url text, commit_sha text,
      cancel_requested boolean NOT NULL DEFAULT false, lease_until timestamptz, worker_id text,
      publish_marker text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(owner, client_request_id)
    );
    CREATE TABLE IF NOT EXISTS demo_events (
      task_id uuid NOT NULL REFERENCES demo_tasks(id) ON DELETE CASCADE, seq bigint NOT NULL,
      type text NOT NULL, data jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(task_id, seq)
    );
    CREATE TABLE IF NOT EXISTS demo_workspace_lease (id integer PRIMARY KEY DEFAULT 1 CHECK (id=1), task_id uuid UNIQUE, lease_until timestamptz);
    INSERT INTO demo_workspace_lease(id) VALUES(1) ON CONFLICT DO NOTHING;
  `).catch((error) => { schemaReady = undefined; throw error; });
  return schemaReady;
}

function encryptionKey() {
  const key = Buffer.from(process.env.DEMO_KEY_ENCRYPTION_SECRET || '', 'base64');
  if (key.length !== 32) throw new Error('DEMO_KEY_ENCRYPTION_SECRET must be a base64-encoded 32-byte key');
  return key;
}

export function encryptProviderKey(value) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', encryptionKey(), iv);
  const ciphertext = Buffer.concat([cipher.update(value, 'utf8'), cipher.final()]);
  return { ciphertext: ciphertext.toString('base64'), iv: iv.toString('base64'), tag: cipher.getAuthTag().toString('base64') };
}

export function decryptProviderKey(record) {
  const decipher = createDecipheriv('aes-256-gcm', encryptionKey(), Buffer.from(record.key_iv, 'base64'));
  decipher.setAuthTag(Buffer.from(record.key_tag, 'base64'));
  return Buffer.concat([decipher.update(Buffer.from(record.key_ciphertext, 'base64')), decipher.final()]).toString('utf8');
}

export function digest(value) { return createHash('sha256').update(value).digest('hex'); }
export function safeEqual(a, b) {
  const left = Buffer.from(String(a || ''));
  const right = Buffer.from(String(b || ''));
  return left.length === right.length && timingSafeEqual(left, right);
}

export async function validateOpenRouter(key, model) {
  if (!/^sk-or-v1-[A-Za-z0-9_-]{20,}$/.test(key)) throw new Error('Invalid OpenRouter key format');
  if (!/^[A-Za-z0-9_.:/-]{1,160}$/.test(model)) throw new Error('Invalid model identifier');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST', signal: controller.signal,
      headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, messages: [{ role: 'user', content: 'Reply with OK.' }], max_tokens: 2, temperature: 0 }),
    });
    if (!response.ok) throw new Error(`OpenRouter rejected key or model (HTTP ${response.status})`);
    const result = await response.json();
    if (typeof result?.choices?.[0]?.message?.content !== 'string') throw new Error('OpenRouter returned an invalid response');
    return model;
  } finally { clearTimeout(timer); }
}

export async function verifyIdentity(event) {
  const header = event.headers?.authorization || event.headers?.Authorization || '';
  let token = header.startsWith('Bearer ') ? header.slice(7) : '';
  if (!token) {
    const cookie = event.headers?.cookie || '';
    token = cookie.split(';').map((part) => part.trim()).find((part) => part.startsWith('nf_jwt='))?.slice(7) || '';
  }
  if (!token) return null;
  const [head, body, signature, ...rest] = token.split('.');
  if (!head || !body || !signature || rest.length) return null;
  let headerObj; let claims;
  try { headerObj = JSON.parse(Buffer.from(head, 'base64url')); claims = JSON.parse(Buffer.from(body, 'base64url')); } catch { return null; }
  if (headerObj.alg !== 'HS256' || typeof claims.sub !== 'string' || !claims.sub || typeof claims.exp !== 'number' || claims.exp <= Date.now() / 1000 ||
      (typeof claims.nbf === 'number' && claims.nbf > Date.now() / 1000) ||
      (claims.iss && process.env.NETLIFY_IDENTITY_ISSUER && claims.iss !== process.env.NETLIFY_IDENTITY_ISSUER)) return null;
  const secret = process.env.NETLIFY_IDENTITY_JWT_SECRET;
  if (!secret) throw new ConfigurationError('NETLIFY_IDENTITY_JWT_SECRET');
  // Direct event-handler tests use an explicitly supplied local HS256 secret.
  const { createHmac } = await import('node:crypto');
  const actual = createHmac('sha256', secret).update(`${head}.${body}`).digest();
  if (Buffer.from(signature, 'base64url').length !== actual.length || !timingSafeEqual(Buffer.from(signature, 'base64url'), actual)) return null;
  return { subject: String(claims.sub), email: claims.email || null, cookieAuth: !header.startsWith('Bearer ') };
}

export function json(statusCode, body, headers = {}) {
  return { statusCode, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff', ...headers }, body: JSON.stringify(body) };
}

export function ok(data, statusCode = 200) { return json(statusCode, { ok: true, ...data }); }
export function fail(statusCode, code, message, retryable = false) { return json(statusCode, { ok: false, error: { code, message, retryable } }); }

export async function addEvent(client, taskId, type, data = {}) {
  const seq = await client.query('SELECT COALESCE(MAX(seq),0)+1 AS seq FROM demo_events WHERE task_id=$1', [taskId]);
  const n = Number(seq.rows[0].seq);
  await client.query('INSERT INTO demo_events(task_id,seq,type,data) VALUES($1,$2,$3,$4)', [taskId, n, type, JSON.stringify(data)]);
  return n;
}

export const allowedStates = new Set(['queued', 'preparing', 'working', 'testing', 'awaiting_approval', 'publishing', 'completed', 'failed', 'cancelled', 'rejected', 'expired', 'interrupted']);
