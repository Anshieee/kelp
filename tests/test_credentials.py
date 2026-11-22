"""Adversarial checks for the private credential channel and encrypted vault."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
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
SIGNING_KEY = 'server-only-signing-key-12345678901234567890'
SITE = 'https://login.example.test'
VALUE = 'synthetic-credential-never-public-72641'
FIELDS = [{'id': 'password', 'label': 'Password', 'kind': 'password', 'selector': '#password'}]


def descriptor(**changes):
    return {'session_id': 'browser_test', 'origin': SITE, 'binding': 'a'*64,
            'fields': FIELDS, 'submission': {'enabled': True, 'label': 'Sign in'}, **changes}


@pytest.fixture
def broker(tmp_path):
    audit = AuditLog(tmp_path/'audit.jsonl')
    b = CredentialBroker(tmp_path/'private'/'vault.db', Fernet.generate_key().decode(), WORKER, [SITE], signing_key=SIGNING_KEY, audit=audit)
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
    b = CredentialBroker(path, key, WORKER, [SITE], signing_key=SIGNING_KEY)
    r = submit(b)
    b.claim(r['id'], r['binding'], r['session_id'], r['origin'])
    b.close()
    with pytest.raises(ValueError, match='Vault key'):
        CredentialBroker(path, Fernet.generate_key().decode(), WORKER, [SITE], signing_key=SIGNING_KEY)
    b = CredentialBroker(path, key, WORKER, [SITE], signing_key=SIGNING_KEY)
    assert b.get(r['id'])['status'] == 'failed'
    b.close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_ciphertext_cannot_be_swapped_between_requests(broker):
    first, second = submit(broker), submit(broker)
    cipher = broker._db.execute('SELECT payload FROM requests WHERE id=?', (first['id'],)).fetchone()[0]
    broker._db.execute('UPDATE requests SET payload=? WHERE id=?', (cipher, second['id']))
    with pytest.raises(ChatError):
        broker.claim(second['id'], second['binding'], second['session_id'], second['origin'])


def test_ciphertext_is_bound_to_request_target_device_and_form(broker):
    r = submit(broker)
    broker._db.execute('UPDATE requests SET binding=? WHERE id=?', ('b' * 64, r['id']))
    with pytest.raises(ChatError) as error:
        broker.claim(r['id'], 'b' * 64, r['session_id'], r['origin'])
    assert error.value.code == 'credential_vault_locked'


def test_worker_tokens_are_request_scoped_and_expire(broker):
    first = broker.create(descriptor())
    second = broker.create(descriptor())
    token = broker.issue_worker_token(first['id'])
    assert broker.worker_token_scope(token, first['id'], 'get')
    assert not broker.worker_token_scope(token, second['id'], 'get')
    assert not broker.worker_token_scope(token, first['id'], 'delete')
    broker._clock = lambda: first['expires_at']
    assert not broker.worker_token_scope(token, first['id'], 'claim')
    assert not broker.worker_token_scope(token, first['id'], 'finish')
    assert broker.worker_token_scope(token, first['id'], 'get')  # status poll outlives the deadline
    broker._clock = lambda: first['expires_at'] + 86400
    assert not broker.worker_token_scope(token, first['id'], 'get')


def test_bootstrap_holder_cannot_forge_request_scoped_token(broker):
    request = broker.create(descriptor())
    claims = {'v': 1, 'id': request['id'], 'device': broker._device_id,
              'target': request['origin'], 'binding': request['binding'],
              'session': request['session_id'], 'exp': int(request['expires_at']),
              'scope': ['get', 'claim', 'finish']}
    encoded = base64.urlsafe_b64encode(json.dumps(
        claims, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=').decode()
    bootstrap_digest = hashlib.sha256(WORKER.encode()).digest()
    forged_signature = hmac.new(bootstrap_digest, encoded.encode(), hashlib.sha256).digest()
    forged = encoded + '.' + base64.urlsafe_b64encode(forged_signature).rstrip(b'=').decode()
    assert not broker.worker_token_scope(forged, request['id'], 'claim')


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
           'KELP_BROWSER_WORKER_TOKEN': WORKER, 'KELP_CREDENTIAL_SIGNING_KEY': SIGNING_KEY,
           'KELP_BROWSER_ALLOWED_ORIGINS': SITE}
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
    worker_token = created['worker_token']
    assert call(api, 'GET', f'/api/credential-worker/{ident}', token=WORKER)[0] == 401
    assert call(api, 'GET', f'/api/credential-worker/{ident}', token=worker_token)[0] == 200
    status, public = call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})
    assert status == 200 and VALUE not in json.dumps(public)
    assert 'selector' not in json.dumps(call(api, 'GET', '/api/credential-requests')[1])
    assert call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})[0] == 409
    assert call(api, 'GET', f'/api/credential-worker/{ident}/claim', token=WORKER)[0] == 401
    status, delivery = call(api, 'POST', f'/api/credential-worker/{ident}/claim',
                            {k: descriptor()[k] for k in ('session_id','binding','origin')}, token=worker_token)
    assert status == 200 and delivery['values']['password'] == VALUE
    assert call(api, 'GET', '/api/credential-requests/vault', token=WORKER)[0] == 401
    assert call(api, 'POST', f'/api/credential-worker/{ident}/finish', {'outcome': 'filled'}, token=worker_token)[0] == 200
    assert call(api, 'POST', f'/api/credential-requests/{ident}/release', {})[0] == 200
    assert api.body_limit_for('/api/credential-worker')[0] == 16384


def test_worker_registration_authenticates_device_instead_of_accepting_claim(api):
    forged = {**descriptor(), 'device': 'caller-selected-device'}
    assert call(api, 'POST', '/api/credential-worker', forged, token=WORKER)[0] == 400
    status, created = call(api, 'POST', '/api/credential-worker', descriptor(), token=WORKER)
    assert status == 201
    token = created['worker_token']
    ident = created['request']['id']
    assert call(api, 'GET', f'/api/credential-worker/{ident}', token=token + 'x')[0] == 401
    assert call(api, 'GET', '/api/credential-worker/not-this-request', token=token)[0] == 401
    assert 'device' not in json.dumps(created['request'])


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
        self.site = SITE
        self.version = 1
        self.filled = ''
        self.clicked = False
    async def evaluate(self, script, arg=None):
        if arg is None:
            return self.site
        return {'origin': self.site, 'version': self.version}
    def locator(self, selector):
        return FakeLocator(self)


async def bridge_roundtrip(api, *, mutate=False):
    def transport(req):
        status, body = call(api, req.method, req.url.path,
                            json.loads(req.content) if req.content else None,
                            token=req.headers['Authorization'][7:])
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
    assert call(api, 'POST', f"/api/credential-worker/{r['id']}/release", {}, token=WORKER)[0] == 401


def test_pending_payload_survives_restart_encrypted(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path/'vault.db'
    b = CredentialBroker(path, key, WORKER, [SITE], signing_key=SIGNING_KEY)
    r = submit(b, save=True)
    b.close()
    assert VALUE.encode() not in path.read_bytes()
    b = CredentialBroker(path, key, WORKER, [SITE], signing_key=SIGNING_KEY)
    assert b.claim(r['id'], r['binding'], r['session_id'], r['origin'])['values']['password'] == VALUE
    assert len(b.list_vault()) == 1
    b.close()


# ---- Request-scoped worker token review (KEL-30) ---------------------------------

def make_scoped(api, **descriptor_changes):
    """Create a request through the real route; return (id, scoped token)."""
    status, created = call(api, 'POST', '/api/credential-worker', descriptor(**descriptor_changes), token=WORKER)
    assert status == 201
    return created['request']['id'], created['worker_token']


def claim_body():
    return {k: descriptor()[k] for k in ('session_id', 'binding', 'origin')}


async def recording_worker(api, scenario):
    """Run scenario(worker, seen) with a CredentialWorker wired to the real routes."""
    seen: list[tuple[str, str, str]] = []

    def transport(req):
        token = req.headers['Authorization'][7:]
        seen.append((req.method, req.url.path, token))
        status, body = call(api, req.method, req.url.path,
                            json.loads(req.content) if req.content else None, token=token)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        worker = CredentialWorker('https://kelp.example.test', WORKER, client=http)
        await scenario(worker, seen)


def test_one_worker_serves_two_sequential_requests_with_per_request_tokens(api):
    async def scenario(worker, seen):
        ids = []
        for _ in range(2):
            page = FakePage()
            session = PrivateBrowserSession(page)
            record = await worker.request(session, FIELDS, '#submit')
            ids.append(record['id'])
            call(api, 'POST', f"/api/credential-requests/{record['id']}/submit", {'values': {'password': VALUE}})
            result = await worker.deliver(session, record['id'])
            assert result['status'] == 'completed' and page.filled == VALUE
            call(api, 'POST', f"/api/credential-requests/{record['id']}/release", {})
            await worker.resume(session, record['id'])
            session.assert_observable()
        assert ids[0] != ids[1]
        # The root token appears only on the create POST; every other call carries
        # a token scoped to exactly the id in its own path.
        creates = [s for s in seen if s[0] == 'POST' and s[1] == '/api/credential-worker']
        assert len(creates) == 2 and all(t == WORKER for _, _, t in creates)
        for method, path, token in seen:
            if (method, path) == ('POST', '/api/credential-worker'):
                continue
            assert token != WORKER
            ident = path.split('/')[3]
            op = 'get' if method == 'GET' else path.split('/')[4]
            assert api.credentials.worker_token_scope(token, ident, op)
            other = ids[1] if ident == ids[0] else ids[0]
            assert not api.credentials.worker_token_scope(token, other, op)
    asyncio.run(recording_worker(api, scenario))


def test_worker_never_falls_back_to_root_token_for_unknown_request(api):
    async def scenario(worker, seen):
        with pytest.raises(RuntimeError, match='unknown_credential_request'):
            await worker.call('GET', '/cred_not_created_by_this_worker')
        with pytest.raises(RuntimeError, match='unknown_credential_request'):
            await worker.call('POST', '/cred_x/claim', claim_body())
        assert seen == []  # nothing was sent, in particular not the root token
    asyncio.run(recording_worker(api, scenario))


def test_worker_rejects_create_reply_without_a_scoped_token(api):
    def transport(req):
        return httpx.Response(201, json={'ok': True, 'request': {'id': 'cred_x'}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            worker = CredentialWorker('https://kelp.example.test', WORKER, client=http)
            with pytest.raises(RuntimeError, match='credential_service_rejected'):
                await worker.request(PrivateBrowserSession(FakePage()), FIELDS, '#submit')
    asyncio.run(scenario())


def test_token_for_request_a_cannot_touch_request_b(api):
    a, token_a = make_scoped(api)
    b, token_b = make_scoped(api)
    call(api, 'POST', f'/api/credential-requests/{b}/submit', {'values': {'password': VALUE}})
    assert call(api, 'GET', f'/api/credential-worker/{b}', token=token_a)[0] == 401
    assert call(api, 'POST', f'/api/credential-worker/{b}/claim', claim_body(), token=token_a)[0] == 401
    assert call(api, 'POST', f'/api/credential-worker/{b}/finish', {'outcome': 'failed'}, token=token_a)[0] == 401
    assert api.credentials.get(b)['status'] == 'submitted'  # nothing was consumed
    assert call(api, 'POST', f'/api/credential-worker/{b}/claim', claim_body(), token=token_b)[0] == 200
    assert a != b


@pytest.mark.parametrize('operation', ['get', 'claim', 'finish'])
def test_root_worker_token_never_authorizes_request_operations(api, operation):
    ident, _ = make_scoped(api)
    call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})
    method = 'GET' if operation == 'get' else 'POST'
    body = {'get': None, 'claim': claim_body(), 'finish': {'outcome': 'failed'}}[operation]
    path = f'/api/credential-worker/{ident}' + ('' if operation == 'get' else '/' + operation)
    assert call(api, method, path, body, token=WORKER)[0] == 401
    assert api.credentials.get(ident)['status'] == 'submitted'


def test_scoped_token_cannot_create_requests_or_use_other_operations(api):
    ident, token = make_scoped(api)
    assert call(api, 'POST', '/api/credential-worker', descriptor(), token=token)[0] == 401
    for op in ('release', 'deny', 'submit', 'delete'):
        assert call(api, 'POST', f'/api/credential-worker/{ident}/{op}', {}, token=token)[0] == 401
    assert call(api, 'GET', f'/api/credential-worker/{ident}/claim', token=token)[0] == 401
    assert call(api, 'GET', '/api/credential-requests', token=token)[0] == 401


def test_claim_still_checks_the_current_browser_binding(api):
    ident, token = make_scoped(api)
    call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})
    drifted = {**claim_body(), 'binding': 'c' * 64}
    assert call(api, 'POST', f'/api/credential-worker/{ident}/claim', drifted, token=token)[0] == 409
    assert api.credentials.get(ident)['status'] == 'submitted'
    assert call(api, 'POST', f'/api/credential-worker/{ident}/claim', claim_body(), token=token)[0] == 200


def test_scoped_token_is_useless_for_claim_after_finish_and_replay(api):
    ident, token = make_scoped(api)
    call(api, 'POST', f'/api/credential-requests/{ident}/submit', {'values': {'password': VALUE}})
    assert call(api, 'POST', f'/api/credential-worker/{ident}/claim', claim_body(), token=token)[0] == 200
    assert call(api, 'POST', f'/api/credential-worker/{ident}/finish', {'outcome': 'filled'}, token=token)[0] == 200
    status, replay = call(api, 'POST', f'/api/credential-worker/{ident}/claim', claim_body(), token=token)
    assert status == 409 and VALUE not in json.dumps(replay)
    assert call(api, 'POST', f'/api/credential-worker/{ident}/finish', {'outcome': 'filled'}, token=token)[0] == 409


def forge(api, ident, **changes):
    row = api.credentials._row(ident)
    claims = {'v': 1, 'id': ident, 'device': row['device'], 'target': row['origin'],
              'binding': row['binding'], 'session': row['session'], 'exp': int(row['expires']),
              'read_exp': int(row['expires']) + 86400, 'scope': ['get', 'claim', 'finish'], **changes}
    encoded = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=').decode()
    return encoded


def test_tampered_payload_or_signature_is_refused(api):
    ident, token = make_scoped(api)
    encoded, signature = token.split('.')
    # Re-signed with the wrong key, and a valid signature over a different payload.
    wrong_key = hmac.new(WORKER.encode(), forge(api, ident).encode(), hashlib.sha256).digest()
    wrong = forge(api, ident) + '.' + base64.urlsafe_b64encode(wrong_key).rstrip(b'=').decode()
    swapped = forge(api, ident, scope=['get', 'claim', 'finish', 'release']) + '.' + signature
    flipped = encoded + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:]
    for bad in (wrong, swapped, flipped, encoded, encoded + '.', '.' + signature, '', '.', 'a.b.c', token + 'x'):
        assert call(api, 'GET', f'/api/credential-worker/{ident}', token=bad)[0] == 401, bad


@pytest.mark.parametrize('suffix', ['!', '!!!!', '====', '=', '==', '\n', '\n\n\n\n', ' ', '.', '.junk', '\x00'])
def test_signature_encoding_is_not_malleable(api, suffix):
    # base64's lenient decoder discards characters outside the alphabet, so a
    # decode-then-compare check accepts many distinct strings for one signature.
    ident, token = make_scoped(api)
    assert not api.credentials.worker_token_scope(token + suffix, ident, 'get')
    assert api.credentials.worker_token_scope(token, ident, 'get')


def test_expired_token_is_refused_for_claim_and_finish(api):
    ident, token = make_scoped(api)
    expires = api.credentials.get(ident)['expires_at']
    api.credentials._clock = lambda: expires + 1
    assert call(api, 'POST', f'/api/credential-worker/{ident}/claim', claim_body(), token=token)[0] == 401
    assert call(api, 'POST', f'/api/credential-worker/{ident}/finish', {'outcome': 'failed'}, token=token)[0] == 401


def test_worker_can_still_poll_and_resume_after_the_request_deadline(api):
    # The owner may release long after the ten-minute request deadline (they read
    # the outcome first). The worker's GET poll must keep working, or the page
    # stays private forever and the CLI exits without ever seeing 'released'.
    ident, token = make_scoped(api)
    expires = api.credentials.get(ident)['expires_at']
    api.credentials._clock = lambda: expires + 3600
    status, body = call(api, 'GET', f'/api/credential-worker/{ident}', token=token)
    assert status == 200 and body['request']['status'] == 'expired'
    assert call(api, 'POST', f'/api/credential-requests/{ident}/release', {})[0] == 200
    status, body = call(api, 'GET', f'/api/credential-worker/{ident}', token=token)
    assert status == 200 and body['request']['status'] == 'released'
    assert call(api, 'POST', f'/api/credential-worker/{ident}/claim', claim_body(), token=token)[0] == 401


def test_read_access_ends_when_the_record_is_pruned(api):
    ident, token = make_scoped(api)
    expires = api.credentials.get(ident)['expires_at']
    api.credentials._clock = lambda: expires + 86400 + 1
    assert call(api, 'GET', f'/api/credential-worker/{ident}', token=token)[0] == 401


def test_broker_rejects_weak_or_shared_signing_keys(tmp_path):
    key = Fernet.generate_key().decode()
    for bad in ('', 'short', 'x' * 31, WORKER, key):
        with pytest.raises(ValueError):
            CredentialBroker(tmp_path / 'weak.db', key, WORKER, [SITE], signing_key=bad)


@pytest.mark.parametrize('field', ['KELP_CREDENTIAL_SIGNING_KEY'])
def test_signing_key_validation_rules(field):
    key = Fernet.generate_key().decode()
    env = {'KELP_CREDENTIALS_ENABLED': '1', 'KELP_VAULT_KEY': key, 'KELP_BROWSER_WORKER_TOKEN': WORKER,
           'KELP_CREDENTIAL_SIGNING_KEY': SIGNING_KEY, 'KELP_BROWSER_ALLOWED_ORIGINS': SITE,
           'KELP_AUTH_TOKEN': OWNER}
    assert validate_env(env)
    for bad in (None, '', 'x' * 31, WORKER, key, OWNER):
        changed = {k: v for k, v in env.items() if k != field}
        if bad is not None:
            changed[field] = bad
        with pytest.raises(ValueError):
            validate_env(changed)


def legacy_store(path, key, rows):
    """Write a pre-device-column credentials.db the way main created it."""
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE meta (id INTEGER PRIMARY KEY, check_value BLOB NOT NULL);
        CREATE TABLE requests (
            id TEXT PRIMARY KEY, session TEXT NOT NULL, origin TEXT NOT NULL,
            binding TEXT NOT NULL, spec TEXT NOT NULL, submission TEXT NOT NULL, state TEXT NOT NULL,
            created REAL NOT NULL, expires REAL NOT NULL, payload BLOB, result TEXT NOT NULL DEFAULT '');
        CREATE TABLE vault (id TEXT PRIMARY KEY, origin TEXT NOT NULL, spec TEXT NOT NULL,
            created REAL NOT NULL, payload BLOB NOT NULL);
    ''')
    cipher = Fernet(key.encode())
    db.execute('INSERT INTO meta VALUES (1, ?)', (cipher.encrypt(b'kelp-vault-v1'),))
    spec = json.dumps(FIELDS)
    roles = [('password', 'password')]
    for ident, state in rows:
        context = json.dumps(['request', ident, SITE, roles], separators=(',', ':'))
        payload = cipher.encrypt(json.dumps({'context': context, 'values': {'password': VALUE}}).encode()) \
            if state in ('submitted', 'claimed') else None
        db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (ident, 'browser_test', SITE, 'a' * 64, spec, json.dumps({'enabled': True, 'label': 'x'}),
                    state, 1.0, 4_000_000_000.0, payload, ''))
    db.commit()
    db.close()


def test_in_flight_requests_from_before_the_upgrade_fail_closed(tmp_path):
    key = Fernet.generate_key().decode()
    path = tmp_path / 'vault.db'
    legacy_store(path, key, [('cred_pend', 'pending'), ('cred_sub', 'submitted'),
                             ('cred_done', 'completed')])
    b = CredentialBroker(path, key, WORKER, [SITE], signing_key=SIGNING_KEY)
    try:
        # No worker holds a scoped token for these, and the sealed payloads use the
        # old context: do not leave them looking live, and do not attribute them
        # to today's worker device.
        assert b.get('cred_pend')['status'] == 'failed' and b.get('cred_pend')['outcome'] == 'upgraded'
        assert b.get('cred_sub')['status'] == 'failed'
        assert b._db.execute("SELECT payload FROM requests WHERE id='cred_sub'").fetchone()[0] is None
        assert b.get('cred_done')['status'] == 'completed'
        rows = {r['id']: r['device'] for r in b._db.execute('SELECT id, device FROM requests')}
        assert set(rows.values()) == {''}
        with pytest.raises(ChatError):
            b.issue_worker_token('cred_done')  # never mint for a row with no known device
        new = b.create(descriptor())
        assert b._row(new['id'])['device'] == b._device_id
        assert b.worker_token_scope(b.issue_worker_token(new['id']), new['id'], 'get')
    finally:
        b.close()


def test_scoped_token_response_is_no_store_and_never_reaches_logs_or_audit(api, capsys):
    from chat.logging import configure_logging
    configure_logging()
    response = api.handle(Request('POST', '/api/credential-worker',
                                  headers={'Authorization': 'Bearer ' + WORKER},
                                  body=json.dumps(descriptor()).encode()))
    assert response.status == 201
    assert response.headers.get('Cache-Control') == 'no-store'
    token = json.loads(response.body)['worker_token']
    ident = json.loads(response.body)['request']['id']
    call(api, 'GET', f'/api/credential-worker/{ident}', token=token)
    call(api, 'GET', f'/api/credential-worker/{ident}', token=token + 'x')
    out = capsys.readouterr()
    audit_text = api.credentials._audit.path.read_text()
    for secret in (token, token.split('.')[1], WORKER, SIGNING_KEY):
        assert secret not in out.out + out.err + audit_text
    assert ident in audit_text  # only the handoff id is recorded
