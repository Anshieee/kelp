import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac, randomUUID } from 'node:crypto';
import { database, ensureSchema } from '../../netlify/functions/demo-core.mjs';
import { handler } from '../../netlify/functions/api-v1.mjs';

const enabled = Boolean(process.env.DEMO_TEST_DATABASE_URL);
const jwt = (subject) => {
  const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');
  const header = encode({ alg: 'HS256' });
  const payload = encode({ sub: subject, exp: Math.floor(Date.now() / 1000) + 600 });
  const signature = createHmac('sha256', process.env.NETLIFY_IDENTITY_JWT_SECRET).update(`${header}.${payload}`).digest('base64url');
  return `${header}.${payload}.${signature}`;
};
const req = (method, path, subject, body) => handler({ httpMethod: method, path: `/api/v1${path}`, rawUrl: `https://demo.test/api/v1${path}`,
  headers: { authorization: `Bearer ${jwt(subject)}` }, body: body === undefined ? undefined : JSON.stringify(body) });

test('Postgres contract: user isolation, idempotent task creation, ordered cursors, and one-time approval', { skip: !enabled }, async () => {
  process.env.DATABASE_URL = process.env.DEMO_TEST_DATABASE_URL;
  process.env.DATABASE_SSL = 'disable';
  process.env.NETLIFY_IDENTITY_JWT_SECRET = 'integration-secret';
  process.env.DEMO_ALLOWED_SUBJECTS = 'contract-user';
  process.env.DEMO_ALLOWED_REPOS = 'example/kelp';
  process.env.GITHUB_REPOSITORY = 'example/kelp';
  const db = database();
  await ensureSchema(db);
  await db.query(`INSERT INTO demo_users(subject,model,onboarding_complete) VALUES('contract-user','openai/gpt-4o-mini',true)
    ON CONFLICT(subject) DO UPDATE SET model=EXCLUDED.model,onboarding_complete=true`);
  const clientRequestId = `contract-${randomUUID()}`;
  const create = () => req('POST', '/tasks', 'contract-user', { client_request_id: clientRequestId, prompt: 'unit test', repo: 'example/kelp' });
  const [first, retry] = await Promise.all([create(), create()]);
  assert.equal(first.statusCode, 202);
  assert.equal(JSON.parse(first.body).task_id, JSON.parse(retry.body).task_id);
  const taskId = JSON.parse(first.body).task_id;
  const outsider = await req('GET', `/tasks/${taskId}`, 'another-user');
  assert.equal(outsider.statusCode, 404);
  const digest = 'a'.repeat(64);
  await db.query(`UPDATE demo_tasks SET state='awaiting_approval',candidate='{}'::jsonb,candidate_digest=$2,
    approval_digest=$2,approval_expires_at=now()+interval '10 minutes' WHERE id=$1`, [taskId, digest]);
  await db.query("INSERT INTO demo_events(task_id,seq,type,data) SELECT $1,COALESCE(MAX(seq),0)+1,'awaiting_approval','{}'::jsonb FROM demo_events WHERE task_id=$1", [taskId]);
  const decisionIds = [`decision-${randomUUID()}`, `decision-${randomUUID()}`];
  const decisions = await Promise.all(decisionIds.map((client_decision_id) => req('POST', `/tasks/${taskId}/decision`, 'contract-user', { decision: 'approve', candidate_digest: digest, client_decision_id })));
  assert.equal(decisions.filter((response) => response.statusCode === 200).length, 1);
  assert.equal(decisions.filter((response) => response.statusCode === 409).length, 1);
  const finalTask = await req('GET', `/tasks/${taskId}`, 'contract-user');
  assert.equal(JSON.parse(finalTask.body).task.state, 'publishing');
  const accepted = await db.query('SELECT decision_id FROM demo_tasks WHERE id=$1', [taskId]);
  const decisionReplay = await req('POST', `/tasks/${taskId}/decision`, 'contract-user', { decision: 'approve', candidate_digest: digest, client_decision_id: accepted.rows[0].decision_id });
  assert.equal(decisionReplay.statusCode, 200);
  const events = await req('GET', `/tasks/${taskId}/events?after=0`, 'contract-user');
  const list = JSON.parse(events.body).events;
  assert.deepEqual(list.map((event) => event.seq), list.map((_, index) => index + 1));
  await db.query('DELETE FROM demo_tasks WHERE owner=$1', ['contract-user']);
  await db.query('DELETE FROM demo_users WHERE subject=$1', ['contract-user']);
  await db.end();
});
