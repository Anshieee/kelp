"""Owner credential inbox and an isolated worker-only delivery surface."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..credentials import MAX_BODY, fail
from ..errors import ChatError
from ._util import _json_response, _rate_limited_response
from .body import _json_body

if TYPE_CHECKING:
    from ..api import ChatAPI, Request, Response


def credentials(api: ChatAPI, request: Request, rest: list[str], *, worker: bool = False) -> Response:
    broker = api.credentials
    if broker is None or not api.auth_token:
        fail('not_found', 404)
    method = request.method.upper()
    if worker:
        # Cookies and owner tokens never authorize a browser worker.
        token = request.header('authorization') or ''
        bearer = token[7:] if token.startswith('Bearer ') else ''
        root = bool(bearer) and broker.worker_authorized(bearer)
        operation = 'create' if not rest and method == 'POST' else (
            'get' if len(rest) == 1 and method == 'GET' else
            rest[1] if len(rest) == 2 and method == 'POST' else '')
        valid = root if operation == 'create' else (
            operation in {'get', 'claim', 'finish'} and bool(rest)
            and broker.worker_token_scope(bearer, rest[0], operation))
        allowed, retry = api.limiter.consume('read' if valid else 'auth', 'credential-worker')
        if not allowed:
            return _rate_limited_response(retry, class_name='auth', reason='rate', metrics=api.metrics)
        if not valid or request.header('origin'):
            raise ChatError('unauthorized', status=401)
        if not rest and method == 'POST':
            created = broker.create(_json_body(api, request, limit=MAX_BODY))
            return _json_response(201, {'ok': True, 'request': created,
                                        'worker_token': broker.issue_worker_token(created['id'])})
        if len(rest) == 1 and method == 'GET':
            return _json_response(200, {'ok': True, 'request': broker.get(rest[0], worker=True)})
        if len(rest) == 2 and method == 'POST':
            body = _json_body(api, request, limit=MAX_BODY)
            if rest[1] == 'claim' and set(body) == {'binding', 'session_id', 'origin'}:
                if not all(isinstance(v, str) for v in body.values()):
                    fail()
                return _json_response(200, {'ok': True, **broker.claim(rest[0], body['binding'], body['session_id'], body['origin'])})
            if rest[1] == 'finish' and set(body) == {'outcome'}:
                return _json_response(200, {'ok': True, 'request': broker.finish(rest[0], body['outcome'])})
    else:
        if not rest and method == 'GET':
            return _json_response(200, {'ok': True, 'requests': broker.list_requests()})
        if rest == ['vault'] and method == 'GET':
            return _json_response(200, {'ok': True, 'credentials': broker.list_vault()})
        if len(rest) == 2 and rest[0] == 'vault' and method == 'DELETE':
            broker.delete_vault(rest[1])
            return _json_response(200, {'ok': True})
        if len(rest) == 1 and method == 'GET':
            return _json_response(200, {'ok': True, 'request': broker.get(rest[0])})
        if len(rest) == 2 and method == 'POST':
            body = _json_body(api, request, limit=MAX_BODY)
            if rest[1] == 'submit':
                return _json_response(200, {'ok': True, 'request': broker.submit(rest[0], body)})
            if rest[1] == 'release' and not body:
                return _json_response(200, {'ok': True, 'request': broker.release(rest[0])})
            if rest[1] == 'deny' and not body:
                return _json_response(200, {'ok': True, 'request': broker.deny(rest[0])})
    fail('not_found', 404)
    raise AssertionError('unreachable')
