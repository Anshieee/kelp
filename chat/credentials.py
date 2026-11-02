"""Private credential handoffs. Never imported by a skill or model executor.

The server and browser worker are trusted. Fernet protects disk, not a compromised
server. All public records are explicit projections; secret values have one
worker-only delivery path, consumed transactionally before returning plaintext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, NoReturn
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .errors import ChatError

MAX_BODY = 16_384
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_BINDING = re.compile(r"[0-9a-f]{64}\Z")


def fail(code: str = 'invalid_credential_request', status: int = 400) -> NoReturn:
    raise ChatError(code, status=status, message='Credential request could not be processed.')


def origin(value: str) -> str:
    """Canonical HTTPS origin only; never retain a path, query, or credentials."""
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password
                or p.path not in ('', '/') or p.query or p.fragment or p.port not in (None, 443)):
            fail()
        host = p.hostname.encode('idna').decode('ascii').lower()
        if not re.fullmatch(r'[a-z0-9.-]+', host) or '.' not in host:
            fail()
        return 'https://' + host
    except (ValueError, UnicodeError, AttributeError, TypeError):
        fail()
    raise AssertionError('unreachable')


def fields_spec(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        fail()
    result = []
    seen = set()
    for f in value:
        if not isinstance(f, dict) or set(f) != {'id', 'label', 'kind', 'selector'}:
            fail()
        if not all(isinstance(v, str) for v in f.values()):
            fail()
        if (not _ID.fullmatch(f['id']) or f['id'] in seen
                or f['kind'] not in {'username', 'password', 'otp', 'secret'}
                or not 1 <= len(f['label']) <= 80 or not 1 <= len(f['selector']) <= 256
                or any(ord(c) < 32 for c in f['label'])):
            fail()
        seen.add(f['id'])
        result.append(dict(f))
    return result


def validate_env(env: Mapping[str, str]) -> bool:
    enabled = env.get('KELP_CREDENTIALS_ENABLED', '0') == '1'
    if not enabled:
        return False
    try:
        Fernet(env.get('KELP_VAULT_KEY', '').encode())
        token = env.get('KELP_BROWSER_WORKER_TOKEN', '')
        if len(token) < 32 or token == env.get('KELP_AUTH_TOKEN'):
            raise ValueError()
        allowed = env.get('KELP_BROWSER_ALLOWED_ORIGINS', '').split(',')
        if not allowed or not all(x.strip() for x in allowed):
            raise ValueError()
        for item in allowed:
            origin(item.strip())
    except (ValueError, ChatError):
        raise ValueError('Invalid credential handoff configuration; check key, worker token and origins.') from None
    return True


class CredentialBroker:
    """Single-owner store, separate from chat content and excluded from tools."""

    def __init__(self, path: Path, key: str, worker_token: str, allowed_origins: list[str],
                 *, audit: Any = None, clock: Callable[[], float] = time.time):
        self._cipher = Fernet(key.encode())
        self._worker_digest = hashlib.sha256(worker_token.encode()).digest()
        self.allowed = frozenset(origin(x) for x in allowed_origins)
        self._audit = audit
        self._clock = clock
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite can place any records in the file.
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript('''
            PRAGMA journal_mode=DELETE;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, check_value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, origin TEXT NOT NULL,
                binding TEXT NOT NULL, device TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL, submission TEXT NOT NULL, state TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, payload BLOB, result TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS vault (
                id TEXT PRIMARY KEY, origin TEXT NOT NULL, spec TEXT NOT NULL,
                created REAL NOT NULL, payload BLOB NOT NULL);
        ''')
        columns = {row['name'] for row in self._db.execute('PRAGMA table_info(requests)')}
        if 'device' not in columns:
            self._db.execute("ALTER TABLE requests ADD COLUMN device TEXT NOT NULL DEFAULT ''")
        self._db.execute("UPDATE requests SET device=? WHERE device=''", (self._device_id,))
        try:
            row = self._db.execute('SELECT check_value FROM meta WHERE id=1').fetchone()
            if row:
                if self._cipher.decrypt(row[0]) != b'kelp-vault-v1':
                    raise InvalidToken()
            else:
                self._db.execute('INSERT INTO meta VALUES (1, ?)', (self._cipher.encrypt(b'kelp-vault-v1'),))
            # Never resume a delivered secret after a process crash.
            self._db.execute("UPDATE requests SET state='failed',payload=NULL,result='interrupted' WHERE state='claimed'")
        except InvalidToken:
            self._db.close()
            raise ValueError('Vault key does not unlock the existing credential store.') from None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def worker_authorized(self, token: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(token.encode()).digest(), self._worker_digest)

    @property
    def _device_id(self) -> str:
        """Stable server-derived identity for the configured trusted worker."""
        return self._worker_digest.hex()

    def issue_worker_token(self, ident: str) -> str:
        """Issue a request-only bearer token expiring with its credential request."""
        with self._transaction():
            row = self._row(ident)
            payload = json.dumps({'v': 1, 'id': ident, 'device': row['device'],
                                  'target': row['origin'], 'binding': row['binding'],
                                  'session': row['session'],
                                  'exp': int(row['expires']), 'scope': ['get', 'claim', 'finish']},
                                 sort_keys=True, separators=(',', ':')).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b'=').decode()
        signature = hmac.new(self._worker_digest, encoded.encode(), hashlib.sha256).digest()
        return encoded + '.' + base64.urlsafe_b64encode(signature).rstrip(b'=').decode()

    def worker_token_scope(self, token: str, ident: str, operation: str) -> bool:
        """Verify an unforgeable, unexpired token scoped to one request/device."""
        try:
            encoded, signature_text = token.split('.', 1)
            supplied = base64.urlsafe_b64decode(signature_text + '=' * (-len(signature_text) % 4))
            expected = hmac.new(self._worker_digest, encoded.encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(supplied, expected):
                return False
            raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
            claims = json.loads(raw)
            with self._lock:
                row = self._db.execute('SELECT * FROM requests WHERE id=?', (ident,)).fetchone()
            return (row is not None and claims.get('v') == 1 and claims.get('id') == ident
                    and claims.get('device') == row['device'] == self._device_id
                    and claims.get('target') == row['origin']
                    and claims.get('binding') == row['binding']
                    and claims.get('session') == row['session']
                    and operation in claims.get('scope', [])
                    and type(claims.get('exp')) is int
                    and claims['exp'] == int(row['expires']) and claims['exp'] > self._clock())
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            return False

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                self._db.execute("UPDATE requests SET state='expired',payload=NULL,result='expired' WHERE expires<=? AND state IN ('pending','submitted','claimed')", (self._clock(),))
                self._db.execute("DELETE FROM requests WHERE expires<?", (self._clock() - 86400,))
                yield
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def _event(self, state: str, ident: str) -> None:
        if self._audit is not None:
            try:
                self._audit.record('credential_handoff', actor='owner', outcome=state,
                                   extra={'handoff_id': ident})
            except Exception:
                pass

    def _row(self, ident: str) -> sqlite3.Row:
        row = self._db.execute('SELECT * FROM requests WHERE id=?', (ident,)).fetchone()
        if row is None:
            fail('not_found', 404)
        return row

    @staticmethod
    def _public(row: sqlite3.Row, *, worker: bool = False) -> dict[str, Any]:
        fields = json.loads(row['spec'])
        if not worker:
            fields = [{k: f[k] for k in ('id', 'label', 'kind')} for f in fields]
        return {'id': row['id'], 'session_id': row['session'], 'origin': row['origin'],
                'binding': row['binding'], 'fields': fields, 'submission': json.loads(row['submission']), 'status': row['state'],
                'created_at': row['created'], 'expires_at': row['expires'], 'outcome': row['result'] or None}

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        if set(data) != {'session_id', 'origin', 'binding', 'fields', 'submission'}:
            fail()
        if not isinstance(data['session_id'], str) or not _ID.fullmatch(data['session_id']):
            fail()
        if not isinstance(data['binding'], str) or not _BINDING.fullmatch(data['binding']):
            fail()
        site = origin(data['origin'])
        if site not in self.allowed:
            fail('credential_origin_denied', 403)
        spec = fields_spec(data['fields'])
        submission = data['submission']
        if (not isinstance(submission, dict) or set(submission) != {'enabled', 'label'}
                or type(submission['enabled']) is not bool or not isinstance(submission['label'], str)
                or len(submission['label']) > 80):
            fail()
        ident = 'cred_' + uuid.uuid4().hex
        now = self._clock()
        with self._transaction():
            if (self._db.execute("SELECT COUNT(*) FROM requests WHERE state IN ('pending','submitted','claimed','completed')").fetchone()[0] >= 128
                    or self._db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] >= 4096):
                fail('credential_capacity', 429)
            self._db.execute('INSERT INTO requests (id,session,origin,binding,device,spec,submission,state,created,expires,payload,result) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             (ident, data['session_id'], site, data['binding'], self._device_id, json.dumps(spec), json.dumps(submission), 'pending', now, now + 600, None, ''))
            result = self._public(self._row(ident), worker=True)
        self._event('requested', ident)
        return result

    def list_requests(self) -> list[dict[str, Any]]:
        with self._transaction():
            return [self._public(r) for r in self._db.execute("SELECT * FROM requests ORDER BY created DESC LIMIT 128")]

    def get(self, ident: str, *, worker: bool = False) -> dict[str, Any]:
        with self._transaction():
            return self._public(self._row(ident), worker=worker)

    def _context(self, prefix: str, row: sqlite3.Row, spec: list[Any]) -> str:
        if prefix == 'request':
            return json.dumps([prefix, row['id'], row['origin'], row['binding'],
                               row['session'], row['device'], spec], separators=(',', ':'))
        return json.dumps([prefix, row['id'], row['origin'], spec], separators=(',', ':'))

    def _seal(self, context: str, values: dict[str, str]) -> bytes:
        return self._cipher.encrypt(json.dumps({'context': context, 'values': values}).encode())

    def _open(self, context: str, payload: bytes) -> dict[str, str]:
        try:
            envelope = json.loads(self._cipher.decrypt(payload))
            if envelope['context'] != context:
                raise InvalidToken()
            return dict(envelope['values'])
        except (InvalidToken, ValueError, KeyError, TypeError):
            fail('credential_vault_locked', 503)

    def submit(self, ident: str, data: dict[str, Any]) -> dict[str, Any]:
        if set(data) - {'values', 'vault_id', 'save'} or type(data.get('save', False)) is not bool:
            fail()
        if ('values' in data) == ('vault_id' in data):
            fail()
        with self._transaction():
            row = self._row(ident)
            if row['state'] != 'pending':
                fail('credential_not_pending', 409)
            spec = json.loads(row['spec'])
            if 'vault_id' in data:
                if not isinstance(data['vault_id'], str):
                    fail()
                saved = self._db.execute('SELECT * FROM vault WHERE id=?', (data['vault_id'],)).fetchone()
                # Origin and field roles must match; nothing silently autofills.
                if saved is None or saved['origin'] != row['origin'] or saved['spec'] != json.dumps([(f['id'], f['kind']) for f in spec]):
                    fail('credential_vault_mismatch', 403)
                values = self._open(self._context('vault', saved, json.loads(saved['spec'])), saved['payload'])
            else:
                values = data['values']
            if (not isinstance(values, dict) or set(values) != {f['id'] for f in spec}
                    or not all(isinstance(v, str) and 0 < len(v) <= 4096 for v in values.values())):
                fail()
            if len(json.dumps(values).encode()) > 12_000:
                fail()
            roles = [(f['id'], f['kind']) for f in spec]
            encrypted = self._seal(self._context('request', row, roles), values)
            if data.get('save'):
                if any(f['kind'] == 'otp' for f in spec):
                    fail('verification_codes_cannot_be_saved')
                if self._db.execute('SELECT COUNT(*) FROM vault').fetchone()[0] >= 256:
                    fail('credential_capacity', 429)
                vault_id = 'vault_' + uuid.uuid4().hex
                vault_context = json.dumps(['vault', vault_id, row['origin'], roles], separators=(',', ':'))
                self._db.execute('INSERT INTO vault VALUES (?,?,?,?,?)',
                                 (vault_id, row['origin'], json.dumps(roles), self._clock(), self._seal(vault_context, values)))
            self._db.execute("UPDATE requests SET state='submitted',payload=? WHERE id=?", (encrypted, ident))
            result = self._public(self._row(ident))
        self._event('submitted', ident)
        return result

    def deny(self, ident: str) -> dict[str, Any]:
        with self._transaction():
            if self._row(ident)['state'] not in ('pending', 'submitted'):
                fail('credential_not_pending', 409)
            self._db.execute("UPDATE requests SET state='denied',payload=NULL,result='denied' WHERE id=?", (ident,))
            result = self._public(self._row(ident))
        self._event('denied', ident)
        return result

    def claim(self, ident: str, binding: str, session: str, site: str) -> dict[str, Any]:
        with self._transaction():
            row = self._row(ident)
            if row['state'] != 'submitted':
                fail('credential_not_available', 409)
            if row['binding'] != binding or row['session'] != session or row['origin'] != origin(site):
                fail('credential_binding_changed', 409)
            roles = [(f['id'], f['kind']) for f in json.loads(row['spec'])]
            values = self._open(self._context('request', row, roles), row['payload'])
            # Commit before returning. A delivery timeout requires fresh user input.
            self._db.execute("UPDATE requests SET state='claimed',payload=NULL WHERE id=?", (ident,))
        self._event('claimed', ident)
        return {'id': ident, 'values': values}

    def finish(self, ident: str, outcome: str) -> dict[str, Any]:
        if outcome not in ('filled', 'submitted', 'failed'):
            fail()
        with self._transaction():
            if self._row(ident)['state'] != 'claimed':
                fail('credential_not_available', 409)
            # "submitted" means form submitted, never "login succeeded".
            state = 'failed' if outcome == 'failed' else 'completed'
            self._db.execute('UPDATE requests SET state=?,payload=NULL,result=? WHERE id=?', (state, outcome, ident))
            result = self._public(self._row(ident), worker=True)
            result['outcome'] = outcome
        self._event(state, ident)
        return result

    def release(self, ident: str) -> dict[str, Any]:
        with self._transaction():
            if self._row(ident)['state'] not in ('completed', 'failed', 'denied', 'expired'):
                fail('credential_not_complete', 409)
            self._db.execute("UPDATE requests SET state='released' WHERE id=?", (ident,))
            result = self._public(self._row(ident))
        self._event('released', ident)
        return result

    def list_vault(self) -> list[dict[str, Any]]:
        with self._transaction():
            return [{'id': r['id'], 'origin': r['origin'], 'fields': json.loads(r['spec']), 'created_at': r['created']}
                    for r in self._db.execute('SELECT id,origin,spec,created FROM vault ORDER BY created DESC LIMIT 256')]

    def delete_vault(self, ident: str) -> None:
        with self._transaction():
            self._db.execute('DELETE FROM vault WHERE id=?', (ident,))
        self._event('vault_deleted', ident)
