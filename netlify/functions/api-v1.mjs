import { randomUUID } from 'node:crypto';
import { addEvent, database, encryptProviderKey, ensureSchema, fail, ok, validateOpenRouter, verifyIdentity } from './demo-core.mjs';

const terminal = new Set(['completed', 'failed', 'cancelled', 'rejected', 'expired', 'interrupted']);
const approvalMinutes = 10;

function parseBody(event) {
  try { return event.body ? JSON.parse(event.body) : {}; } catch { return null; }
}
function cookieValue(event, name) {
  return (event.headers?.cookie || '').split(';').map((s) => s.trim()).find((s) => s.startsWith(`${name}=`))?.slice(name.length + 1) || '';
}
function csrfOk(event, identity) {
  if (!identity.cookieAuth || !['POST', 'PUT', 'PATCH', 'DELETE'].includes(event.httpMethod)) return true;
  const token = event.headers?.['x-csrf-token'] || event.headers?.['X-Csrf-Token'] || '';
  return Boolean(token && cookieValue(event, 'csrf_token') && token === cookieValue(event, 'csrf_token'));
}
function pathInfo(event) {
  const pathname = new URL(event.rawUrl || `https://local${event.path}`, 'https://local').pathname.replace(/\/$/, '');
  return pathname.replace(/^\/api\/v1/, '').replace(/^\/.netlify\/functions\/api-v1/, '') || '/';
}
function publicTask(row) {
  return { task_id: row.id, state: row.state, summary: row.summary || null, base_sha: row.base_sha || null,
    tests: row.test_outcome || null, approval: row.state === 'awaiting_approval' ? { expires_at: row.approval_expires_at, candidate_digest: row.candidate_digest } : null,
    decision: row.decision || null, pr_url: row.pr_url || null, commit_sha: row.commit_sha || null,
    created_at: row.created_at, updated_at: row.updated_at };
}
async function ownedTask(db, owner, id) {
  const result = await db.query('SELECT * FROM demo_tasks WHERE id=$1 AND owner=$2', [id, owner]);
  return result.rows[0] || null;
}

export async function handler(event) {
  try {
    const identity = await verifyIdentity(event);
    if (!identity) return fail(401, 'unauthorized', 'A valid Netlify Identity session is required');
    if (!csrfOk(event, identity)) return fail(403, 'csrf_failed', 'CSRF verification failed');
    const db = database();
    await ensureSchema(db);
    await db.query('INSERT INTO demo_users(subject) VALUES($1) ON CONFLICT(subject) DO NOTHING', [identity.subject]);
    const path = pathInfo(event);
    const method = event.httpMethod;
    const body = parseBody(event);
    if (body === null) return fail(400, 'invalid_json', 'Request body must be valid JSON');

    if (path === '/me' && method === 'GET') {
      const result = await db.query('SELECT provider,model,onboarding_complete FROM demo_users WHERE subject=$1', [identity.subject]);
      const user = result.rows[0];
      return ok({ identity: { subject: identity.subject, email: identity.email }, onboarding: { complete: user.onboarding_complete },
        provider: { configured: Boolean(user.model), provider: user.provider, model: user.model || null },
        entitlement: { enabled: isEntitled(identity.subject) }, workspace: workspaceMetadata() });
    }

    if (path === '/me/provider' && method === 'PUT') {
      if (body.provider !== 'openrouter' || typeof body.api_key !== 'string' || body.api_key.length > 512) return fail(400, 'invalid_provider', 'Only a valid OpenRouter key is supported');
      const model = typeof body.model === 'string' && body.model ? body.model : process.env.DEMO_DEFAULT_MODEL || 'openai/gpt-4o-mini';
      let validated;
      try { validated = await validateOpenRouter(body.api_key, model); } catch { return fail(422, 'provider_validation_failed', 'The OpenRouter key or model could not be validated'); }
      const encrypted = encryptProviderKey(body.api_key);
      await db.query(`UPDATE demo_users SET provider='openrouter',key_ciphertext=$2,key_iv=$3,key_tag=$4,model=$5 WHERE subject=$1`,
        [identity.subject, encrypted.ciphertext, encrypted.iv, encrypted.tag, validated]);
      return ok({ provider: { configured: true, provider: 'openrouter', model: validated } });
    }

    if (path === '/me/onboarding/complete' && method === 'POST') {
      const config = await db.query('SELECT model FROM demo_users WHERE subject=$1', [identity.subject]);
      if (!config.rows[0]?.model) return fail(409, 'provider_required', 'Configure and validate OpenRouter before completing onboarding');
      await db.query('UPDATE demo_users SET onboarding_complete=true WHERE subject=$1', [identity.subject]);
      return ok({ onboarding: { complete: true } });
    }

    if (path === '/workspace' && method === 'GET') {
      const busy = await db.query(`SELECT t.state FROM demo_workspace_lease l LEFT JOIN demo_tasks t ON t.id=l.task_id WHERE l.id=1 AND l.lease_until>now()`);
      return ok({ workspace: { ...workspaceMetadata(), status: busy.rows[0]?.state ? 'busy' : (process.env.GITHUB_REPOSITORY ? 'ready' : 'offline'), active_task_state: busy.rows[0]?.state || null } });
    }

    if (path === '/tasks' && method === 'POST') {
      if (!isEntitled(identity.subject)) return fail(403, 'not_entitled', 'This user is not enabled for the demo');
      if (!/^[A-Za-z0-9_-]{8,100}$/.test(body.client_request_id || '') || typeof body.prompt !== 'string' || !body.prompt.trim() || body.prompt.length > 12000) return fail(400, 'invalid_task', 'client_request_id and a prompt of at most 12000 characters are required');
      const allowlist = (process.env.DEMO_ALLOWED_REPOS || process.env.GITHUB_REPOSITORY || '').split(',').map((v) => v.trim()).filter(Boolean);
      if (typeof body.repo !== 'string' || !allowlist.includes(body.repo)) return fail(403, 'repo_not_allowed', 'The requested repository is not allowlisted');
      const configured = await db.query('SELECT model FROM demo_users WHERE subject=$1 AND onboarding_complete=true', [identity.subject]);
      if (!configured.rows.length) return fail(409, 'onboarding_required', 'Complete provider setup before creating a task');
      const id = randomUUID();
      const client = await db.connect();
      let created = false;
      let createdTask;
      try {
        await client.query('BEGIN');
        const result = await client.query(`INSERT INTO demo_tasks(id,owner,client_request_id,prompt,repo,state) VALUES($1,$2,$3,$4,$5,'queued')
          ON CONFLICT(owner,client_request_id) DO UPDATE SET client_request_id=EXCLUDED.client_request_id RETURNING *, (xmax=0) AS inserted`,
          [id, identity.subject, body.client_request_id, body.prompt.trim(), body.repo]);
        createdTask = result.rows[0]; created = createdTask.inserted === true;
        if (created) await addEvent(client, createdTask.id, 'queued', { state: 'queued' });
        await client.query('COMMIT');
      } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
      if (created || createdTask.state === 'queued') void dispatchPrepare().catch(() => {});
      return ok({ task_id: createdTask.id, state: createdTask.state }, 202);
    }

    const taskRoute = path.match(/^\/tasks\/([0-9a-f-]{36})(?:\/(events|candidate|decision|cancel))?$/i);
    if (taskRoute) {
      const [, taskId, action] = taskRoute;
      let task = await ownedTask(db, identity.subject, taskId);
      if (!task) return fail(404, 'task_not_found', 'Task not found');
      if (task.state === 'awaiting_approval' && new Date(task.approval_expires_at).getTime() <= Date.now()) {
        const client = await db.connect();
        try {
          await client.query('BEGIN');
          const locked = await client.query("SELECT state,approval_expires_at FROM demo_tasks WHERE id=$1 AND owner=$2 FOR UPDATE", [taskId, identity.subject]);
          if (locked.rows[0]?.state === 'awaiting_approval' && new Date(locked.rows[0].approval_expires_at).getTime() <= Date.now()) {
            await client.query("UPDATE demo_tasks SET state='expired',updated_at=now() WHERE id=$1", [taskId]);
            await addEvent(client, taskId, 'expired', { state: 'expired' });
          }
          await client.query('COMMIT');
        } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
        task = await ownedTask(db, identity.subject, taskId);
      }
      if (!action && method === 'GET') return ok({ task: publicTask(task) });
      if (action === 'events' && method === 'GET') {
        const after = Number(new URL(event.rawUrl || `https://local${event.path}`, 'https://local').searchParams.get('after') || '0');
        if (!Number.isSafeInteger(after) || after < 0) return fail(400, 'invalid_cursor', 'after must be a non-negative integer');
        const result = await db.query('SELECT seq,type,data,created_at FROM demo_events WHERE task_id=$1 AND seq>$2 ORDER BY seq LIMIT 100', [taskId, after]);
        const events = result.rows.map((row) => ({ seq: Number(row.seq), type: row.type, ...row.data, created_at: row.created_at }));
        return ok({ events, next_cursor: events.length ? events[events.length - 1].seq : after, has_more: events.length === 100 });
      }
      if (action === 'candidate' && method === 'GET') {
        if (task.state !== 'awaiting_approval' || !task.candidate) return fail(409, 'candidate_unavailable', 'No review candidate is available');
        return ok({ candidate: task.candidate });
      }
      if (action === 'decision' && method === 'POST') {
        if (!['approve', 'reject'].includes(body.decision) || body.candidate_digest !== task.candidate_digest || !/^[A-Za-z0-9_-]{8,100}$/.test(body.client_decision_id || '')) return fail(400, 'invalid_decision', 'Decision, digest, and client_decision_id are required');
        const client = await db.connect();
        try {
          await client.query('BEGIN');
          const locked = await client.query('SELECT *, approval_expires_at<=now() AS approval_expired FROM demo_tasks WHERE id=$1 AND owner=$2 FOR UPDATE', [taskId, identity.subject]);
          const current = locked.rows[0];
          if (!current) { await client.query('ROLLBACK'); return fail(404, 'task_not_found', 'Task not found'); }
          if (current.decision_id === body.client_decision_id) {
            if (current.decision !== body.decision || current.candidate_digest !== body.candidate_digest) { await client.query('ROLLBACK'); return fail(409, 'idempotency_conflict', 'Decision id was already used with a different payload'); }
            await client.query('COMMIT');
            if (current.decision === 'approve' && current.state === 'publishing') void dispatchPublish(taskId, current.candidate_digest).catch(() => {});
            return ok({ task_id: taskId, state: current.state, decision: current.decision });
          }
          if (current.state !== 'awaiting_approval' || current.candidate_digest !== body.candidate_digest) { await client.query('ROLLBACK'); return fail(409, 'approval_consumed', 'This approval is no longer pending'); }
          if (current.approval_expired) {
            await client.query("UPDATE demo_tasks SET state='expired',updated_at=now() WHERE id=$1", [taskId]);
            await addEvent(client, taskId, 'expired', { state: 'expired' }); await client.query('COMMIT');
            return fail(410, 'approval_expired', 'The approval window has expired');
          }
          const newState = body.decision === 'approve' ? 'publishing' : 'rejected';
          await client.query('UPDATE demo_tasks SET state=$2,decision=$3,decision_id=$4,updated_at=now() WHERE id=$1', [taskId, newState, body.decision, body.client_decision_id]);
          await addEvent(client, taskId, body.decision, { state: newState });
          await client.query('COMMIT');
          if (body.decision === 'approve') void dispatchPublish(taskId, current.candidate_digest).catch(() => {});
          return ok({ task_id: taskId, state: newState, decision: body.decision });
        } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
      }
      if (action === 'cancel' && method === 'POST') {
        const client = await db.connect();
        try {
          await client.query('BEGIN');
          const current = await client.query('SELECT * FROM demo_tasks WHERE id=$1 AND owner=$2 FOR UPDATE', [taskId, identity.subject]);
          const row = current.rows[0];
          if (!row) { await client.query('ROLLBACK'); return fail(404, 'task_not_found', 'Task not found'); }
          if (row.state === 'cancelled') { await client.query('COMMIT'); return ok({ task_id: taskId, state: 'cancelled' }); }
          if (terminal.has(row.state)) { await client.query('COMMIT'); return ok({ task_id: taskId, state: row.state }); }
          await client.query("UPDATE demo_tasks SET state='cancelled',cancel_requested=true,updated_at=now() WHERE id=$1", [taskId]);
          await addEvent(client, taskId, 'cancelled', { state: 'cancelled', external_effects_undone: false });
          await client.query('UPDATE demo_workspace_lease SET task_id=NULL,lease_until=NULL WHERE id=1 AND task_id=$1', [taskId]);
          await client.query('COMMIT'); return ok({ task_id: taskId, state: 'cancelled', external_effects_undone: false });
        } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
      }
    }
    return fail(404, 'not_found', 'Endpoint not found');
  } catch (error) {
    console.error('demo API request failed', { name: error?.name || 'Error' });
    return fail(500, 'internal_error', 'The request could not be completed', true);
  }
}

function isEntitled(subject) {
  const users = (process.env.DEMO_ALLOWED_SUBJECTS || '').split(',').map((v) => v.trim()).filter(Boolean);
  return users.includes(subject);
}
function workspaceMetadata() {
  const [owner, repo] = (process.env.GITHUB_REPOSITORY || '').split('/');
  return { backend_kind: 'github_actions', repo: owner && repo ? { owner, name: repo, full_name: `${owner}/${repo}` } : null,
    capabilities: ['prepare', 'tests', 'review_candidate', 'approval_gated_publish'] };
}
async function dispatchPublish(taskId, candidateDigest) {
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const repository = process.env.GITHUB_REPOSITORY;
  if (!token || !repository) throw new Error('Publish dispatch is not configured');
  const [owner, repo] = repository.split('/');
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/dispatches`, { method: 'POST',
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_type: 'kelp-demo-publish', client_payload: { task_id: taskId, candidate_digest: candidateDigest } }) });
  if (!response.ok) throw new Error(`GitHub dispatch failed (${response.status})`);
}
async function dispatchPrepare() {
  const token = process.env.GITHUB_DISPATCH_TOKEN;
  const repository = process.env.GITHUB_REPOSITORY;
  if (!token || !repository) throw new Error('Prepare dispatch is not configured');
  const [owner, repo] = repository.split('/');
  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/dispatches`, { method: 'POST',
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_type: 'kelp-demo-prepare' }) });
  if (!response.ok) throw new Error(`GitHub dispatch failed (${response.status})`);
}
