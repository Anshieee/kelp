import { createHash } from 'node:crypto';
import { addEvent, database, decryptProviderKey, digest, ensureSchema, fail, json, ok, safeEqual } from './demo-core.mjs';

function bodyOf(event) { try { return event.body ? JSON.parse(event.body) : {}; } catch { return null; } }
function workerAuthorized(event) {
  const token = event.headers?.authorization || event.headers?.Authorization || '';
  const expected = process.env.DEMO_WORKER_TOKEN || '';
  return token.startsWith('Bearer ') && expected.length >= 32 && safeEqual(token.slice(7), expected);
}
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
function payloadDigest(candidate) { return createHash('sha256').update(canonical(candidate)).digest('hex'); }
export function isGitCommitSha(value) { return typeof value === 'string' && /^[0-9a-f]{40}$/.test(value); }
function cleanCandidate(candidate) {
  if (!candidate || typeof candidate !== 'object' || !Array.isArray(candidate.files) || candidate.files.length > 200) return false;
  const serialized = JSON.stringify(candidate);
  return serialized.length < 2_000_000 && !/(sk-or-v1-|github_pat_|gh[pousr]_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}|api_key|authorization|secret)/i.test(serialized);
}
function sanitizeEvent(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
  const safe = {};
  for (const [key, value] of Object.entries(data)) {
    if (/key|token|secret|credential|authorization|cookie/i.test(key)) continue;
    if (typeof value === 'string') safe[key] = value.replace(/(?:sk-or-v1-|gh[pousr]_)[A-Za-z0-9_-]{12,}/g, '[redacted]').slice(0, 1000);
    else if (typeof value === 'boolean' || value === null || typeof value === 'number') safe[key] = value;
  }
  return safe;
}

export async function handler(event) {
  if (!workerAuthorized(event)) return fail(401, 'worker_auth_required', 'Worker credentials are required');
  if (event.httpMethod !== 'POST') return fail(405, 'method_not_allowed', 'Use POST');
  const body = bodyOf(event);
  if (!body) return fail(400, 'invalid_json', 'Request body must be valid JSON');
  try {
    const db = database(); await ensureSchema(db);
    const action = new URL(event.rawUrl || `https://local${event.path}`, 'https://local').searchParams.get('action');
    if (action === 'claim') {
      const workerId = typeof body.worker_id === 'string' ? body.worker_id.slice(0, 100) : '';
      if (!workerId) return fail(400, 'worker_id_required', 'worker_id is required');
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        await client.query('SELECT id FROM demo_workspace_lease WHERE id=1 FOR UPDATE');
        const stale = await client.query(`UPDATE demo_tasks SET state='interrupted',updated_at=now() WHERE id=(SELECT task_id FROM demo_workspace_lease WHERE id=1 AND lease_until<=now())
          AND state IN ('preparing','working','testing','publishing') RETURNING id`);
        if (stale.rows[0]) await addEvent(client, stale.rows[0].id, 'interrupted', { state: 'interrupted', reason: 'worker_lease_expired' });
        await client.query(`UPDATE demo_workspace_lease SET task_id=NULL,lease_until=NULL WHERE id=1 AND lease_until<=now()`);
        const active = await client.query('SELECT task_id FROM demo_workspace_lease WHERE id=1 AND lease_until>now()');
        if (active.rows.length) { await client.query('COMMIT'); return ok({ task: null }); }
        const next = await client.query("SELECT * FROM demo_tasks WHERE state='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1");
        if (!next.rows.length) { await client.query('COMMIT'); return ok({ task: null }); }
        const task = next.rows[0];
        const user = await client.query('SELECT model,key_ciphertext,key_iv,key_tag FROM demo_users WHERE subject=$1', [task.owner]);
        if (!user.rows[0]?.key_ciphertext) { await client.query("UPDATE demo_tasks SET state='failed',summary='Provider key missing',updated_at=now() WHERE id=$1", [task.id]); await addEvent(client, task.id, 'failed', { state: 'failed', reason: 'provider_missing' }); await client.query('COMMIT'); return ok({ task: null }); }
        await client.query("UPDATE demo_tasks SET state='preparing',worker_id=$2,lease_until=now()+interval '5 minutes',updated_at=now() WHERE id=$1", [task.id, workerId]);
        await client.query('UPDATE demo_workspace_lease SET task_id=$1,lease_until=now()+interval \'5 minutes\' WHERE id=1', [task.id]);
        await addEvent(client, task.id, 'preparing', { state: 'preparing' });
        await client.query('COMMIT');
        const key = decryptProviderKey(user.rows[0]);
        return ok({ task: { task_id: task.id, prompt: task.prompt, repo: task.repo, model: user.rows[0].model, provider_key: key, base_sha: task.base_sha || null, lease_seconds: 300 } }, 200);
      } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
    }

    if (action === 'heartbeat') {
      const result = await db.query(`UPDATE demo_tasks SET lease_until=now()+interval '5 minutes',updated_at=now()
        WHERE id=$1 AND worker_id=$2 AND lease_until>now() AND state IN ('preparing','working','testing','publishing') RETURNING id`, [body.task_id, body.worker_id]);
      if (!result.rows.length) return fail(409, 'lease_lost', 'Worker lease is no longer valid');
      await db.query("UPDATE demo_workspace_lease SET lease_until=now()+interval '5 minutes' WHERE id=1 AND task_id=$1", [body.task_id]);
      return ok({ renewed: true, lease_seconds: 300 });
    }

    if (action === 'event') {
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        const locked = await client.query("SELECT state FROM demo_tasks WHERE id=$1 AND worker_id=$2 AND lease_until>now() AND state IN ('preparing','working','testing','publishing') FOR UPDATE", [body.task_id, body.worker_id]);
        if (!locked.rows.length) { await client.query('ROLLBACK'); return fail(409, 'lease_lost', 'Worker lease is no longer valid'); }
        const eventType = typeof body.type === 'string' && /^[a-z_]{2,40}$/.test(body.type) ? body.type : 'progress';
        await addEvent(client, body.task_id, eventType, sanitizeEvent(body.data)); await client.query('COMMIT');
        return ok({ appended: true });
      } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
    }

    if (action === 'candidate') {
      const candidate = body.candidate;
      if (!cleanCandidate(candidate) || !body.task_id || !body.worker_id || !isGitCommitSha(body.base_sha)) return fail(400, 'invalid_candidate', 'Candidate must contain safe files and a pinned base SHA');
      const calculated = payloadDigest(candidate);
      if (!safeEqual(calculated, body.candidate_digest)) return fail(400, 'digest_mismatch', 'Candidate digest did not match');
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        const locked = await client.query("SELECT * FROM demo_tasks WHERE id=$1 AND worker_id=$2 AND state IN ('preparing','working','testing') AND lease_until>now() FOR UPDATE", [body.task_id, body.worker_id]);
        if (!locked.rows.length) { await client.query('ROLLBACK'); return fail(409, 'lease_lost', 'Worker lease is no longer valid'); }
        const saved = await client.query(`UPDATE demo_tasks SET state='awaiting_approval',base_sha=$2,summary=$3,test_outcome=$4,candidate=$5,
          candidate_digest=$6,approval_digest=$6,approval_expires_at=now()+interval '10 minutes',updated_at=now() WHERE id=$1 RETURNING approval_expires_at`,
          [body.task_id, body.base_sha, String(candidate.summary || 'Candidate ready').slice(0, 500), JSON.stringify(candidate.checks || {}), JSON.stringify(candidate), calculated]);
        const expires = saved.rows[0].approval_expires_at;
        await addEvent(client, body.task_id, 'awaiting_approval', { state: 'awaiting_approval', candidate_digest: calculated, expires_at: expires.toISOString() });
        await client.query('UPDATE demo_workspace_lease SET task_id=NULL,lease_until=NULL WHERE id=1 AND task_id=$1', [body.task_id]);
        await client.query('COMMIT');
        return ok({ candidate_digest: calculated, expires_at: expires.toISOString() });
      } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
    }

    if (action === 'publish-candidate') {
      const result = await db.query(`SELECT t.*,u.model FROM demo_tasks t JOIN demo_users u ON u.subject=t.owner
        WHERE t.id=$1 AND t.state='publishing' AND t.decision='approve' AND t.candidate_digest=$2`, [body.task_id, body.candidate_digest]);
      if (!result.rows.length) return fail(409, 'publish_not_authorized', 'Candidate is not currently approved and unexpired');
      const task = result.rows[0];
      return ok({ task: { task_id: task.id, repo: task.repo, base_sha: task.base_sha, candidate: task.candidate, candidate_digest: task.candidate_digest, model: task.model } });
    }

    if (action === 'finish') {
      const approved = body.publish_marker === `kelp-demo:${body.task_id}:${body.candidate_digest}`;
      if (!approved || !/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/pull\/\d+$/.test(body.pr_url || '') || !/^[0-9a-f]{40}$/.test(body.commit_sha || '')) return fail(400, 'invalid_publish_result', 'A marker, real PR URL, and commit SHA are required');
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        const locked = await client.query("SELECT * FROM demo_tasks WHERE id=$1 AND candidate_digest=$2 AND state='publishing' AND decision='approve' FOR UPDATE", [body.task_id, body.candidate_digest]);
        if (!locked.rows.length) { await client.query('ROLLBACK'); return fail(409, 'publish_not_authorized', 'Candidate is not authorized to publish'); }
        const task = locked.rows[0];
        if (task.publish_marker && task.publish_marker !== body.publish_marker) { await client.query('ROLLBACK'); return fail(409, 'publish_replay', 'A different publish result already exists'); }
        await client.query("UPDATE demo_tasks SET state='completed',pr_url=$2,commit_sha=$3,publish_marker=$4,updated_at=now() WHERE id=$1", [body.task_id, body.pr_url, body.commit_sha, body.publish_marker]);
        await addEvent(client, body.task_id, 'completed', { state: 'completed', pr_url: body.pr_url, commit_sha: body.commit_sha });
        await client.query('COMMIT'); return ok({ state: 'completed', pr_url: body.pr_url, commit_sha: body.commit_sha });
      } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
    }

    if (action === 'fail') {
      const message = typeof body.message === 'string' ? body.message.replace(/sk-or-v1-[A-Za-z0-9_-]+/g, '[redacted]').slice(0, 500) : 'Worker failed';
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        const locked = await client.query("SELECT state FROM demo_tasks WHERE id=$1 AND worker_id=$2 FOR UPDATE", [body.task_id, body.worker_id]);
        if (!locked.rows.length || ['completed','cancelled','rejected','expired','interrupted'].includes(locked.rows[0].state)) { await client.query('ROLLBACK'); return fail(409, 'task_not_active', 'Task is not active for this worker'); }
        await client.query("UPDATE demo_tasks SET state='failed',summary=$2,updated_at=now() WHERE id=$1", [body.task_id, message]);
        await addEvent(client, body.task_id, 'failed', { state: 'failed', summary: message });
        await client.query("UPDATE demo_workspace_lease SET task_id=NULL,lease_until=NULL WHERE id=1 AND task_id=$1", [body.task_id]);
        await client.query('COMMIT'); return ok({ state: 'failed' });
      } catch (error) { await client.query('ROLLBACK').catch(() => {}); throw error; } finally { client.release(); }
    }

    return fail(404, 'not_found', 'Worker endpoint not found');
  } catch (error) {
    console.error('demo worker request failed', { name: error?.name || 'Error' });
    return fail(500, 'internal_error', 'Worker request could not be completed', true);
  }
}
