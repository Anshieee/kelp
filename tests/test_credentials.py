"""Adversarial checks for the private credential channel and encrypted vault."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet

from chat import ChatAPI, Request
from chat.credential_browser import CredentialWorker, PrivateBrowserSession
from chat.credentials import CredentialBroker, origin, validate_env
from chat.errors import ChatError
from openshell.audit import AuditLog
from tests.chatkit import make_service, text_script

OWNER = 'owner-test-authorization-value'
WORKER = 'worker-test-authorization-value-123456789'
SITE = 'https://login.example.test'
VALUE = 'synthetic-credential-never-public-72641'
FIELDS = [{'id': 'password', 'label': 'Password', 'kind': 'password', 'selector': '#password'}]


def descriptor(**changes):
    return {'session_id': 'browser_test', 'origin': SITE, 'binding': 'a'*64,
            'fields': FIELDS, 'submission': {'enabled': True, 'label': 'Sign in'}, **changes}


@pytest.fixture
def broker(tmp_path):
    audit = AuditLog(tmp_path/'audit.jsonl')
    b = CredentialBroker(tmp_path/'private'/'vault.db', Fernet.generate_key().decode(), WORKER, [SITE], audit=audit)
    yield b
    b.close()


def submit(b, **data):
    r = b.create(descriptor())
    b.submit(r['id'], {'values': {'password': VALUE}, **data})
    return r


def test_single_delivery_has_no_plaintext_on_disk_or_public_surfaces(broker, tmp_path):
    r = submit(broker)
    assert VALUE not in json.dumps(broker.get(r['id']))
    assert VALUE not in json.dumps(broker.list_requests())
    delivery = broker.claim(r['id'], r['binding'], r['session_id'], r['origin'])
    assert delivery['values'] == {'password': VALUE}
    with pytest.raises(ChatError):
        broker.claim(r['id'], r['binding'], r['session_id'], r['origin'])
    done = broker.finish(r['id'], 'submitted')
    assert done['status'] == 'completed'
    assert done['outcome'] == 'submitted'  # never claim "authenticated"
    assert broker.release(r['id'])['status'] == 'released'
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert VALUE.encode() not in path.read_bytes()


def test_concurrent_claims_deliver_exactly_once(broker):
    r = submit(broker)
    def claim():
        try:
            return broker.claim(r['id'], r['binding'], r['session_id'], r['origin'])
        except ChatError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: claim(), range(8)))
    assert sum(x is not None for x in outcomes) == 1


@pytest.mark.parametrize('change', [{'binding': 'b'*64}, {'session_id': 'different'}, {'origin': 'https://evil.example.test'}])
def test_bound_origin_session_and_form(broker, change):
    r = submit(broker)
    request = {**r, **change}
    with pytest.raises(ChatError):
        broker.claim(r['id'], request['binding'], request['session_id'], request['origin'])
    assert broker.get(r['id'])['status'] == 'submitted'


def test_deny_and_expiry_prevent_delivery(broker):
    r = submit(broker)
    broker.deny(r['id'])
    with pytest.raises(ChatError):
        broker.claim(r['id'], r['binding'], r['session_id'], r['origin'])
    r = submit(broker)
    broker._clock = lambda: r['expires_at'] + 1
    assert broker.get(r['id'])['status'] == 'expired'
    assert broker._db.execute('SELECT payload FROM requests WHERE id=?', (r['id'],)).fetchone()[0] is None
    with pytest.raises(ChatError):
        broker.claim(r['id'], r['binding'], r['session_id'], r['origin'])


def test_otp_never_enters_vault(broker):
    r = broker.create(descriptor(fields=[{**FIELDS[0], 'kind': 'otp'}]))
    with pytest.raises(ChatError):
        broker.submit(r['id'], {'values': {'password': '123456'}, 'save': True})
    assert broker.list_vault() == []
    broker.submit(r['id'], {'values': {'password': '123456'}})


def test_vault_reuse_requires_fresh_consent_and_exact_origin_and_roles(broker):
    submit(broker, save=True)
    saved = broker.list_vault()[0]
    assert VALUE not in json.dumps(saved)
    r = broker.create(descriptor())
    assert broker.get(r['id'])['status'] == 'pending'
    broker.submit(r['id'], {'vault_id': saved['id']})
    assert broker.claim(r['id'], r['binding'], r['session_id'], r['origin'])['values']['password'] == VALUE
    other = 'https://other.example.test'
    broker.allowed = broker.allowed | {other}
    bad = broker.create(descriptor(origin=other))
    with pytest.raises(ChatError):
        broker.submit(bad['id'], {'vault_id': saved['id']})
    bad_role = broker.create(descriptor(fields=[{**FIELDS[0], 'kind': 'secret'}]))
    with pytest.raises(ChatError):
        broker.submit(bad_role['id'], {'vault_id': saved['id']})
    broker.delete_vault(saved['id'])
    assert broker.list_vault() == []


def test_wrong_key_fails_on_reopen_and_claimed_work_never_replays(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path/'vault.db'
    b = CredentialBroker(path, key, WORKER, [SITE])
    r = submit(b)
    b.claim(r['id'], r['binding'], r['session_id'], r['origin'])
    b.close()
    with pytest.raises(ValueError, match='Vault key'):
        CredentialBroker(path, Fernet.generate_key().decode(), WORKER, [SITE])
    b = CredentialBroker(path, key, WORKER, [SITE])
    assert b.get(r['id'])['status'] == 'failed'
    b.close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_ciphertext_cannot_be_swapped_between_requests(broker):
    first, second = submit(broker), submit(broker)
    cipher = broker._db.execute('SELECT payload FROM requests WHERE id=?', (first['id'],)).fetchone()[0]
    broker._db.execute('UPDATE requests SET payload=? WHERE id=?', (cipher, second['id']))
    with pytest.raises(ChatError):
        broker.claim(second['id'], second['binding'], second['session_id'], second['origin'])


@pytest.mark.parametrize('site', ['http://login.example.test', 'https://user:pass@login.example.test',
                                 'https://login.example.test/path', 'https://login.example.test?token=foo',
                                 'https://login.example.test:444', 'file:///secret', 'https://localhost'])
def test_origin_validation(site):
    with pytest.raises(ChatError):
        origin(site)


def test_configuration_requires_key_distinct_worker_and_explicit_origins():
    assert not validate_env({})
    with pytest.raises(ValueError):
        validate_env({'KELP_CREDENTIALS_ENABLED': '1'})
    env = {'KELP_CREDENTIALS_ENABLED': '1', 'KELP_VAULT_KEY': Fernet.generate_key().decode(),
           'KELP_BROWSER_WORKER_TOKEN': WORKER, 'KELP_BROWSER_ALLOWED_ORIGINS': SITE}
    assert validate_env(env)
    with pytest.raises(ValueError):
        validate_env({**env, 'KELP_AUTH_TOKEN': WORKER})


@pytest.fixture
def api(tmp_path, broker):
    service, store, _ = make_service(tmp_path, [text_script('hello')])
    app = ChatAPI(service, auth_token=OWNER, credentials=broker)
    yield app
    app.credentials = None  # broker fixture owns its lifetime
    app.close()
    store.close()


def call(api, method, path, data=None, token=OWNER, headers=None):
    response = api.handle(Request(method, path,
                                 headers={'Authorization': 'Bearer '+token, **(headers or {})},
                                 body=b'' if data is None else json.dumps(data).encode()))
    return response.status, json.loads(response.body)


def test_owner_and_worker_routes_are_separate_and_no_response_echoes_values(api):
    assert call(api, 'POST', '/api/credential-worker', descriptor())[0] == 401
    assert call(api, 'GET', '/api/credential-requests', token=WORKER)[0] == 401
    assert call(api, 'POST', '/api/credential-worker', descriptor(), token=WORKER,
                headers={'Origin': SITE})[0] == 401
    status, created = call(api, 'POST', '/api/credential-worker', descriptor(), token=WORKER)
    assert status == 201
    ident = created['request']['id']
    status, public = call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})
    assert status == 200 and VALUE not in json.dumps(public)
    assert 'selector' not in json.dumps(call(api, 'GET', '/api/credential-requests')[1])
    assert call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})[0] == 409
    assert call(api, 'GET', f'/api/credential-worker/{ident}/claim', token=WORKER)[0] == 404
    status, delivery = call(api, 'POST', f'/api/credential-worker/{ident}/claim',
                            {k: descriptor()[k] for k in ('session_id','binding','origin')}, token=WORKER)
    assert status == 200 and delivery['values']['password'] == VALUE
    assert call(api, 'GET', '/api/credential-requests/vault', token=WORKER)[0] == 401
    assert call(api, 'POST', f'/api/credential-worker/{ident}/finish', {'outcome': 'filled'}, token=WORKER)[0] == 200
    assert call(api, 'POST', f'/api/credential-requests/{ident}/release', {})[0] == 200
    assert api.body_limit_for('/api/credential-worker')[0] == 16384


@pytest.mark.parametrize('route', ['credential-requests', 'credential-worker'])
def test_credential_body_limit_requires_api_prefix(api, route):
    assert api.body_limit_for(f'/api/{route}') == (16_384, 413, 'credential_body_too_large')
    assert api.body_limit_for(f'/alias/{route}') == api.body_limit_for('/alias/unknown')


def test_cookie_mutations_keep_existing_csrf_gate(api):
    session, csrf, _record = api.sessions.create(label='test browser')
    ident = api.credentials.create(descriptor())['id']
    path = f'/api/credential-requests/{ident}/submit'
    headers = {'Cookie': f'session={session}; csrf={csrf}', 'Host': 'kelp.example.test',
               'Origin': 'https://kelp.example.test'}
    body = json.dumps({'values': {'password': VALUE}}).encode()
    assert api.handle(Request('POST', path, headers=headers, body=body)).status == 403
    headers['X-CSRF-Token'] = csrf
    assert api.handle(Request('POST', path, headers=headers, body=body)).status == 200


class FakeLocator:
    def __init__(self, page):
        self.page = page
    async def fill(self, value):
        self.page.filled = value
    async def click(self):
        self.page.clicked = True
    async def count(self):
        return 1
    async def evaluate(self, script):
        return 'Sign in'


class FakePage:
    def __init__(self):
        self.context = type('Context', (), {'browser': self})()
        self.site = SITE
        self.version = 1
        self.filled = ''
        self.clicked = False
    async def new_browser_cdp_session(self):
        return self
    async def send(self, method):
        return {'arguments': ['--user-data-dir=/tmp/credential-browser']}
    async def detach(self):
        pass
    async def evaluate(self, script, arg=None):
        if arg is None:
            return self.site
        return {'origin': self.site, 'version': self.version}
    def locator(self, selector):
        return FakeLocator(self)


async def bridge_roundtrip(api, *, mutate=False):
    def transport(req):
        status, body = call(api, req.method, req.url.path,
                            json.loads(req.content) if req.content else None, token=WORKER)
        return httpx.Response(status, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        worker = CredentialWorker('https://kelp.example.test', WORKER, client=http)
        page = FakePage()
        session = PrivateBrowserSession(page)
        record = await worker.request(session, FIELDS, '#submit')
        with pytest.raises(RuntimeError, match='private_credential'):
            session.assert_observable()
        call(api, 'POST', f"/api/credential-requests/{record['id']}/submit", {'values': {'password': VALUE}})
        if mutate:
            page.version += 1
            with pytest.raises(RuntimeError, match='form_changed'):
                await worker.deliver(session, record['id'])
            assert page.filled == ''
            assert api.credentials.get(record['id'])['status'] == 'submitted'
        else:
            result = await worker.deliver(session, record['id'])
            assert result['status'] == 'completed' and result['outcome'] == 'submitted'
            assert VALUE not in json.dumps(result)
            assert page.filled == VALUE and page.clicked
            with pytest.raises(RuntimeError):
                session.assert_observable()
            call(api, 'POST', f"/api/credential-requests/{record['id']}/release", {})
            await worker.resume(session, record['id'])
            session.assert_observable()
            assert page.filled == ''


def test_worker_fills_only_private_channel_and_keeps_agent_paused(api):
    asyncio.run(bridge_roundtrip(api))


def test_worker_detects_form_drift_before_claiming_secret(api):
    asyncio.run(bridge_roundtrip(api, mutate=True))


def test_disabled_feature_stays_closed(api):
    saved = api.credentials
    api.credentials = None
    try:
        assert call(api, 'GET', '/api/credential-requests')[0] == 404
        assert call(api, 'POST', '/api/credential-worker', descriptor(), token=WORKER)[0] == 404
    finally:
        api.credentials = saved


def test_model_history_and_http_logs_never_receive_credential_values(api, capsys):
    from chat.logging import configure_logging
    configure_logging()
    r = api.credentials.create(descriptor())
    assert call(api, 'POST', f"/api/credential-requests/{r['id']}/submit",
                {'values': {'password': VALUE}})[0] == 200
    assert VALUE not in capsys.readouterr().out
    assert api.service.store._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_worker_cannot_release_the_owner_observation_lock(api):
    r = submit(api.credentials)
    api.credentials.claim(r['id'], r['binding'], r['session_id'], r['origin'])
    api.credentials.finish(r['id'], 'filled')
    assert call(api, 'POST', f"/api/credential-worker/{r['id']}/release", {}, token=WORKER)[0] == 404


def test_pending_payload_survives_restart_encrypted(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path/'vault.db'
    b = CredentialBroker(path, key, WORKER, [SITE])
    r = submit(b, save=True)
    b.close()
    assert VALUE.encode() not in path.read_bytes()
    b = CredentialBroker(path, key, WORKER, [SITE])
    assert b.claim(r['id'], r['binding'], r['session_id'], r['origin'])['values']['password'] == VALUE
    assert len(b.list_vault()) == 1
    b.close()
