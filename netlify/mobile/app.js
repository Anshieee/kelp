(() => {
  'use strict';
  const API = '/api/v1';
  const TASKS_KEY = 'kelp-mobile-task-ids-v1';
  const CURSOR_KEY = 'kelp-mobile-cursor-v1:';
  const DECISION_KEY = 'kelp-mobile-decision-v1:';
  const $ = (id) => document.getElementById(id);
  const screens = ['welcome', 'onboarding', 'workspace', 'task', 'review', 'result'];
  let identity = null;
  let token = null;
  let me = null;
  let workspace = null;
  let taskId = null;
  let currentTask = null;
  let candidate = null;
  let pollTimer = null;
  let after = 0;
  let pendingSubmission = null;

  function show(name) {
    document.body.dataset.screen = name;
    screens.forEach((screen) => { $(screen).hidden = screen !== name; });
    $("main").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  function notice(id, text, error = false) {
    const el = $(id);
    el.textContent = text;
    el.classList.toggle('error', error);
    el.hidden = false;
  }
  function clearNotice(id) { $(id).hidden = true; }
  function setConnection(state, text) {
    const el = $('connection');
    el.className = `connection ${state}`;
    el.textContent = text;
  }  function safeJson(value) { try { return JSON.parse(value); } catch { return []; } }
  function taskHistory() {
    const ids = safeJson(sessionStorage.getItem(TASKS_KEY) || '[]');
    return Array.isArray(ids) ? [...new Set(ids.filter((id) => typeof id === 'string'))].slice(0, 20) : [];
  }
  function rememberTask(id) {
    const ids = [id, ...taskHistory().filter((saved) => saved !== id)].slice(0, 20);
    sessionStorage.setItem(TASKS_KEY, JSON.stringify(ids));
  }
  function replaceTaskUrl(id) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('task', id); else url.searchParams.delete('task');
    window.history.replaceState({}, '', url);
  }
  async function api(path, options = {}) {
    if (!identity) throw new Error('Sign in to continue.');
    token = await identity.jwt();
    if (!token) throw new Error('Your sign-in session expired. Sign in again.');
    const headers = { Authorization: `Bearer ${token}`, Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers };
    const response = await fetch(`${API}${path}`, { ...options, headers, cache: 'no-store', credentials: 'same-origin', redirect: 'error' });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.ok) {
      const error = payload?.error;
      throw new Error(error?.message || `Request failed (${response.status}).`);
    }
    return payload;
  }
  function shortSha(value) { return value ? String(value).slice(0, 12) : 'Not available'; }
  function taskState(value) {
    $('task-state').textContent = value || 'unknown';
    $('task-state').className = `state-pill ${['failed', 'expired', 'cancelled'].includes(value) ? 'failed' : value === 'awaiting_approval' || value === 'publishing' ? 'busy' : ''}`;
  }
  function formatDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '' : date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  async function refreshMe() {
    const data = await api('/me');
    me = data;
    if (!data.entitlement?.enabled) throw new Error('This sign-in is not enabled for the demo.');
    if (!data.onboarding?.complete) {
      $('setup-step').textContent = data.provider?.configured ? '02 / 02' : '01 / 02';
      $('complete-onboarding').hidden = !data.provider?.configured;
      $('provider-form').hidden = Boolean(data.provider?.configured);
      show('onboarding');
      return;
    }
    await loadWorkspace();
  }
  async function loadWorkspace() {
    const data = await api('/workspace');
    workspace = data.workspace;
    const repo = workspace?.repo?.full_name;
    $('repo-name').textContent = repo || 'No repository configured';
    $('workspace-state').textContent = workspace?.status || 'offline';
    $('workspace-state').className = `state-pill ${workspace?.status === 'busy' ? 'busy' : workspace?.status !== 'ready' ? 'offline' : ''}`;
    $('backend-kind').textContent = workspace?.backend_kind === 'github_actions' ? 'GitHub Actions · real runner' : 'Backend unavailable';
    $('capabilities').replaceChildren(...(workspace?.capabilities || []).map((name) => {
      const pill = document.createElement('span');
      pill.textContent = name.replaceAll('_', ' ');
      return pill;
    }));
    $('task-form').querySelector('button').disabled = workspace?.status !== 'ready' || !repo;
    await renderHistory();
    show('workspace');
  }
  async function renderHistory() {
    const list = $('history');
    const ids = taskHistory();
    $('sidebar-history').replaceChildren();
    if (!ids.length) {
      list.replaceChildren(Object.assign(document.createElement('li'), { className: 'empty-history', textContent: 'Tasks you start here will appear in this session.' }));
      return;
    }
    list.replaceChildren();
    // One round trip per task, in parallel; a task that no longer exists
    // (purged, or not ours) drops out of the list silently.
    const results = await Promise.all(ids.map(async (id) => {
      try { return [id, (await api(`/tasks/${encodeURIComponent(id)}`)).task]; }
      catch { return [id, null]; }
    }));
    for (const [id, task] of results) {
      if (!task) continue;
      const row = document.createElement('li');
      const button = document.createElement('button');
      const left = document.createElement('span');
      const summary = document.createElement('strong');
      summary.textContent = task.summary || `Task ${shortSha(id)}`;
      const detail = document.createElement('small');
      detail.textContent = `${task.state} · ${formatDate(task.updated_at || task.created_at)}`;
      left.append(summary, detail);
      const arrow = document.createElement('span'); arrow.textContent = '→';
      button.append(left, arrow);
      button.addEventListener('click', () => openTask(id));
      row.append(button); list.append(row);
      const sidebarButton = button.cloneNode(true);
      sidebarButton.addEventListener('click', () => { closeSidebar(); openTask(id).catch((error) => notice('workspace-message', error.message, true)); });
      const sidebarRow = document.createElement('li'); sidebarRow.append(sidebarButton); $('sidebar-history').append(sidebarRow);
    }
    if (!list.children.length) list.append(Object.assign(document.createElement('li'), { className: 'empty-history', textContent: 'No tasks available in this session.' }));
  }
  async function openTask(id) {
    stopPolling();
    taskId = id;
    currentTask = null;
    candidate = null;
    after = Number(sessionStorage.getItem(CURSOR_KEY + id) || 0);
    $('task-id').textContent = id;
    $('events').replaceChildren();
    $('task-summary').textContent = 'Restoring task state…';
    $('reconnect-status').textContent = 'Reconnecting…';
    $('task-result').hidden = true;
    $('open-review').hidden = true;
    replaceTaskUrl(id);
    show('task');
    await refreshTask();
    if (currentTask && !['completed', 'failed', 'cancelled', 'rejected', 'expired', 'interrupted'].includes(currentTask.state)) startPolling();
  }
  function addEvents(events) {
    for (const event of events || []) {
      if (!Number.isSafeInteger(event.seq) || event.seq <= after) continue;
      after = event.seq;
      const row = document.createElement('li');
      const body = document.createElement('span');
      body.textContent = (event.summary || event.message || event.state || event.type || 'Task update').replaceAll('_', ' ');
      const date = document.createElement('small'); date.textContent = formatDate(event.created_at);
      body.append(date); row.append(body); $('events').append(row);
    }
    if (taskId) sessionStorage.setItem(CURSOR_KEY + taskId, String(after));
  }
  async function refreshTask() {
    try {
      const [taskResponse, eventsResponse] = await Promise.all([
        api(`/tasks/${encodeURIComponent(taskId)}`),
        api(`/tasks/${encodeURIComponent(taskId)}/events?after=${after}`),
      ]);
      currentTask = taskResponse.task;
      addEvents(eventsResponse.events);
      $('reconnect-status').textContent = 'Live · reconnect-safe';
      $('task-state').textContent = currentTask.state;
      taskState(currentTask.state);
      $('task-summary').textContent = currentTask.summary || 'Your request is being prepared.';
      $('task-title').innerHTML = currentTask.state === 'completed' ? 'Change published' : currentTask.state === 'awaiting_approval' ? 'Ready for review' : 'Working on your task';
      $('open-review').hidden = currentTask.state !== 'awaiting_approval';
      if (currentTask.state === 'awaiting_approval') await loadCandidate();
      if (currentTask.state === 'completed') showResult(currentTask);
      if (['failed', 'cancelled', 'rejected', 'expired', 'interrupted'].includes(currentTask.state)) {
        stopPolling();
        $('task-result').hidden = false;
        $('task-result').textContent = `Task ${currentTask.state}. No pull request was reported as completed.`;
      }
      clearNotice('task-message');
    } catch (error) {
      $('reconnect-status').textContent = 'Reconnecting…';
      notice('task-message', `Connection interrupted. Your task is saved; reconnecting. ${error.message}`, true);
    }
  }
  async function loadCandidate() {
    // Render each candidate digest once; re-rendering the diff on every poll
    // rewrites the node the user is reading. A new digest (a new candidate)
    // re-renders; the same digest does not.
    const digest = currentTask?.approval?.candidate_digest;
    if (digest && candidate && candidate.__digest === digest) return;
    try {
      const response = await api(`/tasks/${encodeURIComponent(taskId)}/candidate`);
      candidate = response.candidate;
      candidate.__digest = digest;
      $('open-review').hidden = false;
      renderCandidate();
    } catch (error) { notice('task-message', error.message, true); }
  }
  function startPolling() {
    stopPolling();
    pollTimer = window.setInterval(refreshTask, 3500);
  }
  function stopPolling() { if (pollTimer) window.clearInterval(pollTimer); pollTimer = null; }
  function showResult(task) {
    stopPolling();
    const url = task.pr_url;
    if (!url || !/^https:\/\//i.test(url)) {
      notice('task-message', 'The service has not confirmed a pull request URL yet.', true);
      return;
    }
    $('result-summary').textContent = task.summary || 'The approved candidate was published.';
    $('pr-link').href = url;
    $('commit-sha').textContent = task.commit_sha || 'Not reported';
    show('result');
  }
  function renderCandidate() {
    if (!candidate || !currentTask) return;
    $('base-sha').textContent = candidate.base_sha || currentTask.base_sha || 'Not available';
    $('candidate-digest').textContent = currentTask.approval?.candidate_digest || 'Missing digest';
    $('candidate-digest').classList.add('digest');
    $('candidate-summary').textContent = candidate.summary || currentTask.summary || 'Candidate ready';
    const checks = candidate.checks || currentTask.tests || {};
    const entries = Object.entries(checks);
    $('checks').replaceChildren(...(entries.length ? entries.map(([name, result]) => {
      const row = document.createElement('li');
      const title = document.createElement('span'); title.textContent = name;
      const status = document.createElement('strong');
      const passed = result === true || result?.passed === true || result?.status === 'passed';
      status.textContent = passed ? `Passed${Number.isInteger(result?.exit_code) ? ` · exit ${result.exit_code}` : ''}` : result?.passed === false || result?.status === 'failed' ? `Failed${Number.isInteger(result?.exit_code) ? ` · exit ${result.exit_code}` : ''}` : 'Reported';
      status.className = passed ? 'check-pass' : result?.passed === false || result?.status === 'failed' ? 'check-fail' : '';
      row.append(title, status); return row;
    }) : [Object.assign(document.createElement('li'), { textContent: 'No test evidence was reported.' })]));
    const files = Array.isArray(candidate.files) ? candidate.files : [];
    $('file-count').textContent = `${files.length} changed file${files.length === 1 ? '' : 's'}`;
    $('diff').textContent = typeof candidate.diff === 'string' && candidate.diff ? candidate.diff : 'No diff was returned by the service.';
  }
  async function makeDecision(decision) {
    if (!currentTask?.approval?.candidate_digest) return notice('review-message', 'Candidate identity is unavailable; cannot submit a decision.', true);
    const key = DECISION_KEY + taskId + ':' + currentTask.approval.candidate_digest;
    let decisionId = sessionStorage.getItem(key);
    if (!decisionId) {
      decisionId = crypto.randomUUID().replaceAll('-', '_');
      sessionStorage.setItem(key, decisionId);
    }
    const controls = [$('approve'), $('reject')]; controls.forEach((control) => { control.disabled = true; });
    try {
      await api(`/tasks/${encodeURIComponent(taskId)}/decision`, { method: 'POST', body: JSON.stringify({ decision, candidate_digest: currentTask.approval.candidate_digest, client_decision_id: decisionId }) });
      clearNotice('review-message');
      show('task');
      await refreshTask();
      if (currentTask && !['completed', 'failed', 'cancelled', 'rejected', 'expired', 'interrupted'].includes(currentTask.state)) startPolling();
    } catch (error) { notice('review-message', error.message, true); }
    finally { controls.forEach((control) => { control.disabled = false; }); }
  }

  let sidebarReturnFocus = null;
  function closeSidebar() {
    document.body.classList.remove('sidebar-open');
    $('sidebar-backdrop').hidden = true;
    $('open-sidebar').setAttribute('aria-expanded', 'false');
    if (sidebarReturnFocus) sidebarReturnFocus.focus();
  }
  $('open-sidebar').addEventListener('click', () => {
    sidebarReturnFocus = document.activeElement;
    document.body.classList.add('sidebar-open');
    $('sidebar-backdrop').hidden = false;
    $('open-sidebar').setAttribute('aria-expanded', 'true');
    $('close-sidebar').focus();
  });
  $('close-sidebar').addEventListener('click', closeSidebar);
  $('sidebar-backdrop').addEventListener('click', closeSidebar);
  document.addEventListener('keydown', (event) => {
    if (!document.body.classList.contains('sidebar-open')) return;
    if (event.key === 'Escape') closeSidebar();
    if (event.key === 'Tab') {
      const controls = [...$('sidebar').querySelectorAll('a, button')].filter((el) => el.getClientRects().length);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  async function newTask() {
    closeSidebar(); stopPolling(); replaceTaskUrl(null);
    if (!identity) { show('welcome'); return; }
    try { await refreshMe(); if (!me?.onboarding?.complete) return; $('prompt').focus(); }
    catch (error) { show('welcome'); notice('auth-message', error.message, true); }
  }
  $('new-chat').addEventListener('click', newTask);
  $('sidebar-home').addEventListener('click', newTask);
  $('theme-toggle').addEventListener('click', () => {
    const light = document.documentElement.dataset.skin !== 'meadow';
    document.documentElement.dataset.skin = light ? 'meadow' : 'midnight';
    $('theme-label').textContent = light ? 'Meadow' : 'Midnight';
    $('theme-toggle').setAttribute('aria-label', light ? 'Switch to Midnight dark theme' : 'Switch to Meadow light theme');
    document.querySelector('meta[name="theme-color"]').content = light ? '#f6f8f6' : '#141414';
  });

  $('login').addEventListener('click', () => {
    if (window.netlifyIdentity) window.netlifyIdentity.open('login');
    else notice('auth-message', 'Sign-in is unavailable. Please try again later.', true);
  });
  function isAuthError(message) {
    return /sign in|session expired|not enabled for the demo/i.test(String(message));
  }
  async function connect() {
    // Shared by the identity init event and the welcome screen's Retry
    // button. Auth problems send the user to sign in; server or network
    // problems keep the session and offer a retry — the pill and the
    // message must not claim the sign-in died when it did not.
    setConnection('', 'Loading workspace');
    try {
      const requestedTask = new URL(location.href).searchParams.get('task');
      if (requestedTask && /^[0-9a-f-]{36}$/i.test(requestedTask)) {
        const who = await api('/me');
        me = who;
        if (!who.entitlement?.enabled) throw new Error('This sign-in is not enabled for the demo.');
        if (!who.onboarding?.complete) { await refreshMe(); return; }
        rememberTask(requestedTask); await openTask(requestedTask);
      } else await refreshMe();
      setConnection('online', 'Connected');
      clearNotice('auth-message');
      $('retry-connect').hidden = true;
    } catch (error) {
      if (isAuthError(error.message)) {
        identity = null;
        setConnection('', 'Sign in required');
        $('retry-connect').hidden = true;
      } else {
        setConnection('error', 'Server unreachable');
        $('retry-connect').hidden = false;
      }
      notice('auth-message', error.message, true);
      show('welcome');
    }
  }
  $('retry-connect').addEventListener('click', () => {
    $('retry-connect').hidden = true;
    connect().catch(() => {});
  });
  if (window.netlifyIdentity) {
    window.netlifyIdentity.on('init', async (user) => {
      identity = user;
      if (!user) { setConnection(navigator.onLine ? '' : 'offline', navigator.onLine ? 'Sign in required' : 'Offline'); show('welcome'); return; }
      connect();
    });
    window.netlifyIdentity.on('login', (user) => { identity = user; window.netlifyIdentity.close(); refreshMe().then(() => setConnection('online', 'Connected')).catch((error) => notice('auth-message', error.message, true)); });
    window.netlifyIdentity.on('logout', () => { identity = null; token = null; me = null; stopPolling(); show('welcome'); setConnection('', 'Sign in required'); });
  } else { setConnection('', 'Sign-in unavailable'); }
  window.addEventListener('online', () => { if (identity) { setConnection('online', 'Reconnecting'); refreshTask(); } else setConnection('', 'Sign in required'); });
  window.addEventListener('offline', () => { setConnection('offline', 'Offline'); if (taskId) $('reconnect-status').textContent = 'Offline · reconnecting when online'; });

  $('provider-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = $('api-key');
    const secret = input.value;
    if (!secret || secret.length > 512) return notice('onboarding-message', 'Enter a valid OpenRouter key.', true);
    $('save-provider').disabled = true;
    try {
      const result = await api('/me/provider', { method: 'PUT', body: JSON.stringify({ provider: 'openrouter', api_key: secret, model: $('model').value }) });
      input.value = '';
      $('provider-form').hidden = true;
      $('complete-onboarding').hidden = false;
      $('setup-step').textContent = '02 / 02';
      notice('onboarding-message', `Provider validated · ${result.provider?.model || $('model').value}`);
    } catch (error) { input.value = ''; notice('onboarding-message', error.message, true); }
    finally { $('save-provider').disabled = false; }
  });
  $('complete-onboarding').addEventListener('click', async () => {
    $('complete-onboarding').disabled = true;
    try { await api('/me/onboarding/complete', { method: 'POST', body: '{}' }); await loadWorkspace(); }
    catch (error) { notice('onboarding-message', error.message, true); }
    finally { $('complete-onboarding').disabled = false; }
  });
  $('task-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const prompt = $('prompt').value.trim();
    if (!prompt || !workspace?.repo?.full_name) return notice('workspace-message', 'A prompt and entitled repository are required.', true);
    const submit = $('task-form').querySelector('button'); submit.disabled = true;
    try {
      if (!pendingSubmission || pendingSubmission.prompt !== prompt) pendingSubmission = { prompt, id: crypto.randomUUID().replaceAll('-', '_') };
      const response = await api('/tasks', { method: 'POST', body: JSON.stringify({ client_request_id: pendingSubmission.id, prompt, repo: workspace.repo.full_name }) });
      pendingSubmission = null;
      $('prompt').value = '';
      rememberTask(response.task_id);
      await openTask(response.task_id);
      if (currentTask && !['completed', 'failed', 'cancelled', 'rejected', 'expired', 'interrupted'].includes(currentTask.state)) startPolling();
    } catch (error) { notice('workspace-message', error.message, true); }
    finally { submit.disabled = workspace?.status !== 'ready'; }
  });
  $('refresh-workspace').addEventListener('click', () => loadWorkspace().catch((error) => notice('workspace-message', error.message, true)));
  $('logout').addEventListener('click', () => window.netlifyIdentity?.logout());
  $('back-workspace').addEventListener('click', () => { stopPolling(); replaceTaskUrl(null); loadWorkspace().catch((error) => notice('task-message', error.message, true)); });
  $('back-task').addEventListener('click', () => show('task'));
  $('open-review').addEventListener('click', () => { renderCandidate(); show('review'); });
  $('approve').addEventListener('click', () => makeDecision('approve'));
  $('reject').addEventListener('click', () => makeDecision('reject'));
  $('new-task').addEventListener('click', () => { replaceTaskUrl(null); loadWorkspace().catch((error) => notice('workspace-message', error.message, true)); });
  document.querySelectorAll('input, textarea, select').forEach((field) => field.addEventListener('focus', () => {
    window.requestAnimationFrame(() => field.scrollIntoView({ block: 'center', behavior: 'smooth' }));
  }));

  if ('serviceWorker' in navigator && location.protocol === 'https:') {
    navigator.serviceWorker.register('/mobile/sw.js?v=2', { scope: '/mobile/' }).catch(() => {});
  }
})();
