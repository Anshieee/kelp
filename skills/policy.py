"""Invoke-time enforcement of declared skill effects and scopes (KEL-20).

A manifest is a *declaration*; this module is the *enforcement*. Every call to
:meth:`skills.loader.SkillRegistry.invoke` builds an :class:`InvocationPolicy`
from the loaded manifest and checks the concrete arguments before the handler
runs. The same policy backs the handler-facing chokepoints:

* ``context.read_path`` / ``context.write_path`` confine file access to the
  declared ``${WORKSPACE}/…`` prefixes (symlinks are resolved first);
* ``context.check_repo`` confines a GitHub repo to the declared set;
* ``context.egress()`` is the only sanctioned network path. It checks the host
  and method against the declared ``hosts`` before a request is made.

Network egress has a second, independent guard: while a skill handler runs,
process-wide socket entry points refuse direct connections, datagram sends, and
DNS lookups. A handler that ignores ``context.egress()`` and reaches for
``socket`` or ``urllib`` fails closed instead of escaping its allowlist. The
guard is a ContextVar, so code outside an invocation is unaffected. Threads
started during an invocation inherit it through ``contextvars.copy_context()``.

The guard is cooperative and covers the Python-level ``socket`` module, plus
``threading.Thread`` and ``ThreadPoolExecutor`` context propagation. It does
not intercept a raw ``_socket`` object, a child process, a C extension calling
``connect(2)`` directly, or a thread that was already running before the
invocation and receives work through a queue or shared state rather than
``submit``. Those boundaries are the deployment's, not this module's.

A policy ``deny`` is terminal. It never becomes an approval request; only a
manifest-level ``requires_approval`` (or an in-scope write, which the manifest
must already gate) can ask a human.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import ipaddress
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from openshell.audit import AuditLog
from openshell.errors import PolicyDenied
from openshell.policy import (
    Capability,
    Decision,
    Effect,
    PolicyConfig,
    PolicyEngine,
    PolicyVerdict,
    host_allowed,
)

__all__ = [
    "DEFAULT_MAX_EGRESS_BYTES",
    "EgressClient",
    "EgressResponse",
    "GuardedSkillContext",
    "InvocationPolicy",
    "PolicyDenied",
    "SCOPE_FORMATS",
    "active_invocation",
    "install_network_guard",
    "repo_allowed",
]

_RANK = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}

#: The largest response body the egress client will read into memory.
DEFAULT_MAX_EGRESS_BYTES = 2 * 1024 * 1024

_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: JSON-Schema ``format`` values that type one input argument as a scope
#: target. The runtime never guesses from an argument name: a skill opts in
#: per property, in its own ``input_schema`` (see ``docs/SKILL_SPEC.md`` §8.5).
SCOPE_FORMATS: Mapping[str, str] = {
    "kelp-path": "read_path",
    "kelp-write-path": "write_path",
    "kelp-host": "host",
    "kelp-repo": "repo",
}


# ---------------------------------------------------------------------------
# Active invocation guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ActiveInvocation:
    policy: "InvocationPolicy"
    tool: str
    audit: AuditLog | None = None
    turn_id: str | None = None


_ACTIVE: contextvars.ContextVar[_ActiveInvocation | None] = contextvars.ContextVar(
    "kelp_skill_active_invocation", default=None
)
#: Set only by :class:`EgressClient` around its own transport call. A handler
#: that reaches a socket directly never has this flag.
_EGRESS_ALLOWED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kelp_skill_egress_allowed", default=False
)


@contextmanager
def active_invocation(
    policy: "InvocationPolicy",
    *,
    tool: str,
    audit: AuditLog | None = None,
    turn_id: str | None = None,
) -> Iterator[None]:
    """Make ``policy`` the guard for the current task/thread for the body."""

    token = _ACTIVE.set(
        _ActiveInvocation(policy=policy, tool=tool, audit=audit, turn_id=turn_id)
    )
    try:
        yield
    finally:
        _ACTIVE.reset(token)


# ---------------------------------------------------------------------------
# Socket guard
# ---------------------------------------------------------------------------

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_GETHOSTBYNAME = socket.gethostbyname
_REAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_REAL_GETHOSTBYADDR = socket.gethostbyaddr
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_SOCKET_SENDTO = socket.socket.sendto
_REAL_SOCKET_SENDMSG = socket.socket.sendmsg
_REAL_THREAD_START = threading.Thread.start
_REAL_SUBMIT = concurrent.futures.ThreadPoolExecutor.submit
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def _normalise_socket_host(host: Any) -> str:
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            raise PolicyDenied(
                "network access requires a valid ASCII host", ("net:unresolved",)
            ) from None
    if not isinstance(host, str) or not host.strip():
        raise PolicyDenied(
            "network access requires a resolvable host", ("net:unresolved",)
        )
    return host.strip().lower().rstrip(".")


def _guard_socket_target(host: Any) -> None:
    active = _ACTIVE.get()
    if active is None:
        return  # normal application traffic, outside a skill invocation
    candidate = _normalise_socket_host(host)
    if not _EGRESS_ALLOWED.get():
        raise PolicyDenied(
            "direct network access is not allowed from a skill; use context.egress()",
            (f"net:{candidate}", "egress:direct"),
        )
    # The egress client already checked the method and URL. Re-check the host
    # here so a transport-level redirect or a DNS change cannot escape scope.
    active.policy.check_host(candidate, "GET")


def _refuse_direct_egress() -> None:
    raise PolicyDenied(
        "direct network access is not allowed from a skill; use context.egress()",
        ("net:direct", "egress:direct"),
    )


def _guard_address(address: Any) -> None:
    """Check the ``(host, port)`` tuple a datagram send would target."""

    if isinstance(address, tuple) and address:
        _guard_socket_target(address[0])
    else:
        _guard_socket_target(address)


def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
    _guard_socket_target(host)
    return _REAL_GETADDRINFO(host, port, *args, **kwargs)


def _guarded_gethostbyname(host: Any) -> Any:
    _guard_socket_target(host)
    return _REAL_GETHOSTBYNAME(host)


def _guarded_gethostbyname_ex(host: Any) -> Any:
    _guard_socket_target(host)
    return _REAL_GETHOSTBYNAME_EX(host)


def _guarded_gethostbyaddr(host: Any) -> Any:
    _guard_socket_target(host)
    return _REAL_GETHOSTBYADDR(host)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    _guard_address(address)
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    if _ACTIVE.get() is not None and not _EGRESS_ALLOWED.get():
        _refuse_direct_egress()
    return _REAL_SOCKET_CONNECT(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> Any:
    if _ACTIVE.get() is not None and not _EGRESS_ALLOWED.get():
        _refuse_direct_egress()
    return _REAL_SOCKET_CONNECT_EX(self, address)


def _guarded_sendto(self: socket.socket, data: Any, *args: Any) -> Any:
    if args:
        _guard_address(args[0])
    elif _ACTIVE.get() is not None and not _EGRESS_ALLOWED.get():
        # Connected socket with no address: ``connect`` predates the
        # invocation, so refuse the direct send itself.
        _refuse_direct_egress()
    return _REAL_SOCKET_SENDTO(self, data, *args)


def _guarded_sendmsg(
    self: socket.socket,
    buffers: Any,
    ancdata: Any = (),
    flags: int = 0,
    address: Any = None,
) -> Any:
    if address is not None:
        _guard_address(address)
    elif _ACTIVE.get() is not None and not _EGRESS_ALLOWED.get():
        _refuse_direct_egress()
    return _REAL_SOCKET_SENDMSG(self, buffers, ancdata, flags, address)


def _is_pool_worker(target: Any) -> bool:
    """ThreadPoolExecutor workers outlive a turn; ``submit`` covers them."""

    return getattr(target, "__module__", None) == "concurrent.futures.thread"


def _guarded_thread_start(self: threading.Thread) -> None:
    if _ACTIVE.get() is not None and not _is_pool_worker(getattr(self, "_target", None)):
        # A new thread starts with an empty context, which would hide the
        # guard. Run its body inside a copy of the caller's context instead.
        context = contextvars.copy_context()
        body = self.run

        def _run_guarded() -> None:
            context.run(body)

        self.run = _run_guarded  # type: ignore[method-assign]
    return _REAL_THREAD_START(self)


def _guarded_submit(self: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    if _ACTIVE.get() is not None:
        # Pool workers are created once and reused; bind the guard per task.
        context = contextvars.copy_context()
        submitted = fn

        def _run_guarded(*call_args: Any, **call_kwargs: Any) -> Any:
            return context.run(submitted, *call_args, **call_kwargs)

        fn = _run_guarded
    return _REAL_SUBMIT(self, fn, *args, **kwargs)


def install_network_guard() -> None:
    """Install the process-wide socket guard exactly once.

    The patched functions are transparent whenever no invocation is active, so
    the rest of the process keeps normal networking. Importing this module
    installs it; the explicit function exists for tests and diagnostics.
    """

    global _INSTALLED
    if _INSTALLED:
        return
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
        socket.gethostbyname = _guarded_gethostbyname  # type: ignore[assignment]
        socket.gethostbyname_ex = _guarded_gethostbyname_ex  # type: ignore[assignment]
        socket.gethostbyaddr = _guarded_gethostbyaddr  # type: ignore[assignment]
        socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
        socket.socket.connect = _guarded_connect  # type: ignore[assignment]
        socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[assignment]
        socket.socket.sendto = _guarded_sendto  # type: ignore[assignment]
        socket.socket.sendmsg = _guarded_sendmsg  # type: ignore[assignment]
        threading.Thread.start = _guarded_thread_start  # type: ignore[assignment]
        concurrent.futures.ThreadPoolExecutor.submit = _guarded_submit  # type: ignore[assignment]
        _INSTALLED = True


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


def _effect_value(effect: Effect | str) -> str:
    return effect.value if isinstance(effect, Effect) else str(effect)


def repo_allowed(repo: str, patterns: Iterable[str]) -> bool:
    """Exact ``owner/name`` or ``*``. GitHub names are case-insensitive."""

    if not isinstance(repo, str):
        return False
    candidate = repo.strip()
    if candidate.count("/") != 1 or not candidate:
        return False
    owner, name = candidate.split("/", 1)
    if not owner or not name:
        return False
    lowered = candidate.lower()
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        normalised = pattern.strip()
        if normalised == "*":
            return True
        if normalised.lower() == lowered:
            return True
    return False


def _strings(key: str, value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, str) for item in value):
            return [str(item) for item in value]
        raise PolicyDenied(
            f"argument {key!r} must contain only strings", (f"arg:{key}",)
        )
    if value is None:
        return []
    raise PolicyDenied(
        f"argument {key!r} has an unresolvable scope value", (f"arg:{key}",)
    )


def _walk_schema(
    schema: Any, root: Any, segments: tuple[str, ...] = (), *, depth: int = 0
) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield ``(segments, kind)`` for every scope-typed input argument.

    ``segments`` address the value inside the argument object; ``"*"`` stands
    for an array index. Only local ``$ref`` pointers are followed; the loader
    rejects remote references.
    """

    if depth > 32 or not isinstance(schema, Mapping):
        return
    declared = schema.get("format")
    if segments and isinstance(declared, str) and declared in SCOPE_FORMATS:
        yield segments, SCOPE_FORMATS[declared]
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        target: Any = root
        try:
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError):
            return
        yield from _walk_schema(target, root, segments, depth=depth + 1)
    for branch in ("allOf", "anyOf", "oneOf"):
        subschemas = schema.get(branch)
        if isinstance(subschemas, list):
            for subschema in subschemas:
                yield from _walk_schema(subschema, root, segments, depth=depth + 1)
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            if isinstance(name, str) and isinstance(child, Mapping):
                yield from _walk_schema(
                    child, root, (*segments, name), depth=depth + 1
                )
    items = schema.get("items")
    if isinstance(items, Mapping):
        yield from _walk_schema(items, root, (*segments, "*"), depth=depth + 1)


def _values_at(value: Any, segments: tuple[str, ...]) -> Iterator[Any]:
    if not segments:
        yield value
        return
    head, rest = segments[0], segments[1:]
    if head == "*":
        if isinstance(value, (list, tuple)):
            for item in value:
                yield from _values_at(item, rest)
        return
    if isinstance(value, Mapping) and head in value:
        yield from _values_at(value[head], rest)


def _worst(current: PolicyVerdict, candidate: PolicyVerdict) -> PolicyVerdict:
    return candidate if _RANK[candidate.decision] > _RANK[current.decision] else current


@dataclass(frozen=True)
class InvocationPolicy:
    """The manifest's declared capability, checked against one invocation."""

    skill: str
    version: str
    effects: frozenset[str]
    paths: tuple[str, ...]
    hosts: tuple[str, ...]
    repos: tuple[str, ...]
    config: PolicyConfig
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    _engine: PolicyEngine = field(init=False, repr=False, compare=False)
    _typed_args: tuple[tuple[tuple[str, ...], str], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_engine", PolicyEngine(self.config))
        typed: dict[tuple[str, ...], str] = {}
        for segments, kind in _walk_schema(self.input_schema, self.input_schema):
            typed.setdefault(segments, kind)
        object.__setattr__(self, "_typed_args", tuple(typed.items()))

    @classmethod
    def from_skill(cls, skill: Any, config: PolicyConfig) -> "InvocationPolicy":
        scopes = getattr(skill, "scopes", {}) or {}
        manifest = getattr(skill, "manifest", None)
        input_schema = (
            manifest.get("input_schema") if isinstance(manifest, Mapping) else None
        )
        return cls(
            skill=str(skill.name),
            version=str(getattr(skill, "version", "")),
            effects=frozenset(_effect_value(effect) for effect in skill.effects),
            paths=tuple(scopes.get("paths", ())),
            hosts=tuple(scopes.get("hosts", ())),
            repos=tuple(scopes.get("repos", ())),
            config=config,
            input_schema=input_schema if isinstance(input_schema, Mapping) else {},
        )

    @property
    def workspace_root(self) -> Path:
        return self.config.workspace_root

    def capability(self) -> Capability:
        return Capability(
            tool=self.skill,
            effects=frozenset(self.effects),
            paths=self.paths,
            hosts=self.hosts,
            repos=self.repos,
        )

    # -- effects -----------------------------------------------------------

    def check_effect(self, effect: Effect | str) -> str:
        value = _effect_value(effect)
        if value not in {item.value for item in Effect}:
            raise PolicyDenied(f"unknown effect {value!r}", (f"effect:{value}",))
        if value not in self.effects:
            raise PolicyDenied(
                f"effect {value!r} is not declared by {self.skill}", (f"effect:{value}",)
            )
        return value

    # -- paths -------------------------------------------------------------

    def resolve_path(self, value: str | Path) -> Path:
        if "\x00" in str(value):
            raise PolicyDenied("path contains a NUL byte", ("fs:unresolved",))
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            raise PolicyDenied("path cannot be resolved", ("fs:unresolved",)) from None
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            raise PolicyDenied(
                f"path {value!s} is outside the workspace", ("fs:outside",)
            ) from None
        return resolved

    def _path_verdict(
        self, value: str | Path, *, write: bool
    ) -> tuple[PolicyVerdict, Path]:
        effect = Effect.WRITE if write else Effect.READ
        self.check_effect(effect)
        resolved = self.resolve_path(value)
        verdict = self._engine.evaluate(
            Capability(
                tool=self.skill,
                effects=frozenset({effect}),
                paths=self.paths,
            ),
            target=resolved,
        )
        return verdict, resolved

    def check_path(self, value: str | Path, *, write: bool = False) -> Path:
        verdict, resolved = self._path_verdict(value, write=write)
        if verdict.decision is Decision.DENY:
            raise PolicyDenied(verdict.reason, verdict.blast_radius)
        return resolved

    # -- hosts -------------------------------------------------------------

    def check_host(self, host: str, method: str = "GET") -> str:
        self.check_effect(Effect.NETWORK)
        normalised_method = str(method).upper()
        if normalised_method not in _HTTP_METHODS:
            raise PolicyDenied(
                f"HTTP method {method!r} is not supported", (f"net:method:{method}",)
            )
        if normalised_method not in _SAFE_METHODS and "write" not in self.effects:
            raise PolicyDenied(
                f"method {normalised_method} requires the 'write' effect",
                (f"net:method:{normalised_method}",),
            )
        if not isinstance(host, str):
            raise PolicyDenied("network access requires a host", ("net:unresolved",))
        candidate = host.strip().lower().rstrip(".")
        if not candidate or not host_allowed(candidate, self.hosts):
            raise PolicyDenied(
                f"host {candidate!r} is not in the declared allowlist",
                (f"net:{candidate}",),
            )
        # A deployment can narrow the manifest further; it can never widen it.
        if self.config.allowed_hosts and not host_allowed(
            candidate, self.config.allowed_hosts
        ):
            raise PolicyDenied(
                f"host {candidate!r} is blocked by deployment policy",
                (f"net:{candidate}",),
            )
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            pass
        else:
            raise PolicyDenied(
                f"IP literal {candidate!r} is not an allowlisted hostname",
                (f"net:{candidate}",),
            )
        return candidate

    def check_url(self, url: str, method: str = "GET") -> str:
        if not isinstance(url, str) or not url:
            raise PolicyDenied("network access requires a URL", ("net:unresolved",))
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError:
            raise PolicyDenied("URL is not parseable", ("net:unresolved",)) from None
        if parsed.scheme not in {"http", "https"}:
            raise PolicyDenied(
                f"scheme {parsed.scheme!r} is not allowed", (f"net:scheme:{parsed.scheme}",)
            )
        if parsed.username is not None or parsed.password is not None:
            raise PolicyDenied(
                "URLs with embedded credentials are not allowed", ("net:credentials",)
            )
        if port not in (None, 80, 443):
            raise PolicyDenied(
                f"port {port} is not allowed", (f"net:port:{port}",)
            )
        if not parsed.hostname:
            raise PolicyDenied("URL has no host", ("net:unresolved",))
        return self.check_host(parsed.hostname, method)

    # -- repos -------------------------------------------------------------

    def check_repo(self, repo: str) -> str:
        if not self.repos:
            raise PolicyDenied(
                f"{self.skill} declares no repo scope", ("repo:unscoped",)
            )
        if not repo_allowed(repo, self.repos):
            raise PolicyDenied(
                f"repo {repo!r} is not in the declared allowlist",
                (f"repo:{repo}",),
            )
        return repo

    # -- argument-level scope ---------------------------------------------

    def evaluate_arguments(self, arguments: Mapping[str, Any]) -> PolicyVerdict:
        """Check every scope-typed concrete target in ``arguments``.

        Only arguments the manifest's ``input_schema`` annotates with a
        ``kelp-*`` format are interpreted; the runtime never guesses a target
        from an argument's name. A typed target with no matching declared
        scope is denied, so a read path cannot slip past an empty ``paths``
        list. Raises :class:`PolicyDenied` on the first escape; returns the
        worst non-denying verdict for the audit record.
        """

        worst = PolicyVerdict(Decision.ALLOW, "arguments are inside the declared scope")
        if not isinstance(arguments, Mapping) or not self._typed_args:
            return worst

        for segments, kind in self._typed_args:
            name = segments[-1] if segments else ""
            for value in _values_at(arguments, segments):
                for item in _strings(name, value):
                    if kind == "repo":
                        self.check_repo(item)
                        worst = _worst(
                            worst, PolicyVerdict(Decision.ALLOW, f"repo {item!r} allowed")
                        )
                    elif kind == "host":
                        host = self.check_url_or_host(item, "GET")
                        worst = _worst(
                            worst, PolicyVerdict(Decision.ALLOW, f"host {host!r} allowed")
                        )
                    else:
                        verdict, _ = self._path_verdict(
                            item, write=kind == "write_path"
                        )
                        if verdict.decision is Decision.DENY:
                            raise PolicyDenied(verdict.reason, verdict.blast_radius)
                        worst = _worst(worst, verdict)
        return worst

    def check_url_or_host(self, value: str, method: str = "GET") -> str:
        if "://" in value:
            return self.check_url(value, method)
        return self.check_host(value, method)


# ---------------------------------------------------------------------------
# Egress chokepoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EgressResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    truncated: bool = False

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a 302 must not move egress outside the allowlist."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise PolicyDenied(
            "redirects are not followed by the skill egress chokepoint",
            (f"net:{urllib.parse.urlsplit(newurl).hostname}", "egress:redirect"),
        )


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    body: bytes | None,
    timeout: float,
    max_bytes: int,
) -> EgressResponse:
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers or {}),
        method=method,
    )
    try:
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:  # a response, not a transport failure
        raw = exc.read(max_bytes + 1)
        return EgressResponse(
            status=int(exc.code),
            headers={str(k): str(v) for k, v in exc.headers.items()},
            body=raw[:max_bytes],
            url=url,
            truncated=len(raw) > max_bytes,
        )
    try:
        raw = response.read(max_bytes + 1)
        return EgressResponse(
            status=int(response.status),
            headers={str(k): str(v) for k, v in response.headers.items()},
            body=raw[:max_bytes],
            url=response.geturl(),
            truncated=len(raw) > max_bytes,
        )
    finally:
        response.close()


def _url_host_hint(url: Any) -> str:
    try:
        return urllib.parse.urlsplit(str(url)).hostname or "unresolved"
    except ValueError:
        return "unresolved"


class EgressClient:
    """The only sanctioned network path for a skill.

    Policy is checked before the transport is touched. ``transport`` exists for
    tests and offline fixtures; production uses :mod:`urllib.request` with
    redirects disabled.
    """

    def __init__(
        self,
        policy: InvocationPolicy,
        *,
        audit: AuditLog | None = None,
        turn_id: str | None = None,
        transport: Callable[
            [str, str, Mapping[str, str] | None, bytes | None, float],
            EgressResponse,
        ]
        | None = None,
        max_bytes: int = DEFAULT_MAX_EGRESS_BYTES,
        timeout: float = 10.0,
    ):
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._policy = policy
        self._audit_log = audit
        self._turn_id = turn_id
        self._transport = transport
        self._max_bytes = int(max_bytes)
        self._timeout = float(timeout)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> EgressResponse:
        normalised = str(method).upper()
        try:
            host = self._policy.check_url(url, normalised)
        except PolicyDenied:
            self._audit(
                "skill_egress",
                _url_host_hint(url),
                normalised,
                "deny",
                "policy_denied",
            )
            raise
        effective_timeout = self._timeout if timeout is None else float(timeout)
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")
        self._audit("skill_egress", host, normalised, "allow", "started")
        token = _EGRESS_ALLOWED.set(True)
        try:
            if self._transport is not None:
                response = self._transport(
                    normalised, url, headers, body, effective_timeout
                )
            else:
                response = await asyncio.to_thread(
                    _send,
                    normalised,
                    url,
                    headers,
                    body,
                    effective_timeout,
                    self._max_bytes,
                )
        except PolicyDenied:
            self._audit("skill_egress", host, normalised, "deny", "policy_denied")
            raise
        except Exception:
            self._audit("skill_egress", host, normalised, "allow", "egress_error")
            raise
        finally:
            _EGRESS_ALLOWED.reset(token)
        self._audit(
            "skill_egress",
            host,
            normalised,
            "allow",
            "completed",
            status=response.status,
        )
        return response

    async def get(self, url: str, **kwargs: Any) -> EgressResponse:
        return await self.request("GET", url, **kwargs)

    async def post(
        self, url: str, *, body: bytes | None = None, **kwargs: Any
    ) -> EgressResponse:
        return await self.request("POST", url, body=body, **kwargs)

    def _audit(
        self,
        event: str,
        host: str,
        method: str,
        decision: str,
        outcome: str,
        *,
        status: int | None = None,
    ) -> None:
        if self._audit_log is None:
            return
        extra: dict[str, Any] = {"host": host, "method": method}
        if status is not None:
            extra["status"] = status
        try:
            self._audit_log.record(
                event,
                turn_id=self._turn_id,
                tool=self._policy.skill,
                decision=decision,
                outcome=outcome,
                extra=extra,
            )
        except Exception:  # noqa: BLE001 - audit is accounting, not traffic
            return


# ---------------------------------------------------------------------------
# Handler-facing context
# ---------------------------------------------------------------------------


class GuardedSkillContext:
    """A :class:`~skills.context.SkillContext` plus policy chokepoints.

    It deliberately *wraps* rather than subclasses ``SkillContext`` so a caller
    can still pass a plain context to a handler directly in tests; the loader
    passes this one for registry invocations.
    """

    __slots__ = (
        "skill",
        "workspace_root",
        "_inner",
        "policy",
        "_audit",
        "_turn_id",
    )

    def __init__(
        self,
        inner: Any,
        policy: InvocationPolicy,
        *,
        audit: AuditLog | None = None,
        turn_id: str | None = None,
    ):
        self._inner = inner
        self.skill = inner.skill
        self.workspace_root = inner.workspace_root
        self.policy = policy
        self._audit = audit
        self._turn_id = turn_id

    # -- passthrough -------------------------------------------------------

    def secret(self, name: str) -> str:
        return self._inner.secret(name)

    def has_secret(self, name: str) -> bool:
        return self._inner.has_secret(name)

    def scrub(self, text: str) -> str:
        return self._inner.scrub(text)

    def __repr__(self) -> str:
        return repr(self._inner)

    __str__ = __repr__

    # -- effects -----------------------------------------------------------

    def require_effect(self, effect: Effect | str) -> str:
        return self.policy.check_effect(effect)

    # -- paths -------------------------------------------------------------

    def read_path(self, value: str | Path) -> Path:
        return self.policy.check_path(value, write=False)

    def write_path(self, value: str | Path) -> Path:
        return self.policy.check_path(value, write=True)

    def open_read(self, value: str | Path, mode: str = "rb") -> Any:
        return self.read_path(value).open(mode)

    def open_write(self, value: str | Path, mode: str = "wb") -> Any:
        return self.write_path(value).open(mode)

    # -- repos -------------------------------------------------------------

    def check_repo(self, repo: str) -> str:
        return self.policy.check_repo(repo)

    # -- network -----------------------------------------------------------

    def egress(
        self,
        *,
        transport: Callable[
            [str, str, Mapping[str, str] | None, bytes | None, float],
            EgressResponse,
        ]
        | None = None,
        timeout: float = 10.0,
        max_bytes: int = DEFAULT_MAX_EGRESS_BYTES,
    ) -> EgressClient:
        return EgressClient(
            self.policy,
            audit=self._audit,
            turn_id=self._turn_id,
            transport=transport,
            timeout=timeout,
            max_bytes=max_bytes,
        )


# Installing at import is intentional: the guard must exist before a handler
# runs, and the loader imports this module. It is transparent outside an
# invocation.
install_network_guard()
