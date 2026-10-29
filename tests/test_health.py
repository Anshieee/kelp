"""KEL-12 M2-B: liveness and readiness contract (§15.6)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from chat import ArtifactRegistry, ChatAPI, ChatService, ChatStore, Request
from chat.api import HealthProbe, _http_provider_probe
from chat.server import ServerConfig, build_app
from kernel.usage import JsonlUsageStore
from openshell.audit import AuditLog

from .chatkit import MODEL, StubProvider, text_script

AUTH = {"authorization": "Bearer tok"}
TOKEN = "tok"
COMMIT = "abc1234def5678"
VERSION = "1.2.3"

COMPONENTS = ("store", "artifacts", "audit", "usage", "provider", "registry")


class FakeRegistry:
    def names(self) -> list[str]:
        return ["docs"]


class BrokenRegistry:
    def names(self) -> list[str]:
        raise RuntimeError("registry is not loaded")


class FalsyProbe:
    """A probe object that is falsy but present.

    Selection is by ``is None``, not truthiness, so this hook must win over a
    ``health`` fallback instead of being skipped.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self) -> bool:
        self.calls += 1
        return True


def _health_hook_must_not_run() -> bool:  # pragma: no cover - assertions guard it
    raise AssertionError("health hook must not be selected over a present probe")


class UpstreamProvider:
    """A provider double with a real ``config`` so the HTTP probe path runs."""

    def __init__(
        self,
        base_url: str = "https://upstream.example/v1",
        api_key: str = "upstream-key-123456",
    ) -> None:
        self.config = SimpleNamespace(base_url=base_url, api_key=api_key)

    async def stream(self, request: Any) -> Any:  # pragma: no cover - no turns run
        raise AssertionError("no turn should run")
        yield  # pragma: no cover - keeps this an async generator


def _fake_httpx(
    monkeypatch: pytest.MonkeyPatch, *, status_code: int = 200
) -> tuple[list[tuple[str, dict[str, str]]], dict[str, Any]]:
    """Replace ``httpx.AsyncClient`` with a recorder; return calls and kwargs."""

    import httpx

    calls: list[tuple[str, dict[str, str]]] = []
    client_kwargs: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self) -> None:
            self.status_code = status_code

    class FakeAsyncClient:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            client_kwargs.update(kwargs)

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(
            self, url: str, headers: Mapping[str, str] | None = None
        ) -> FakeResponse:
            calls.append((url, dict(headers or {})))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return calls, client_kwargs


class Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.store = ChatStore(tmp_path / "db" / "chat.db")
        self.artifacts = ArtifactRegistry(self.store, tmp_path / "artifacts")
        self.audit = AuditLog(tmp_path / "audit" / "audit.jsonl")
        self.usage = JsonlUsageStore(tmp_path / "usage" / "usage.jsonl")
        self.registry = FakeRegistry()
        self.provider = StubProvider([text_script("Hello")])
        self.service = ChatService(
            store=self.store,
            provider=self.provider,
            registry=self.registry,
            artifacts=self.artifacts,
            usage_store=self.usage,
            model=MODEL,
        )
        self.probe = self.make_probe()
        self.api = ChatAPI(
            self.service, auth_token=TOKEN, csrf_token="csrf", health=self.probe
        )

    def make_probe(self, **overrides: Any) -> HealthProbe:
        values: dict[str, Any] = {
            "store": self.store,
            "artifacts": self.artifacts,
            "audit": self.audit,
            "usage_store": self.usage,
            "provider": self.provider,
            "registry": self.registry,
            "provider_check": lambda: True,
            "version": VERSION,
            "commit": COMMIT,
        }
        values.update(overrides)
        return HealthProbe(**values)

    def close(self) -> None:
        self.api.close()
        self.store.close()


@pytest.fixture()
def stack(tmp_path: Path) -> Any:
    built = Stack(tmp_path)
    try:
        yield built
    finally:
        built.close()


def call(
    api: ChatAPI,
    path: str,
    *,
    method: str = "GET",
    auth: bool = False,
) -> Any:
    headers = dict(AUTH) if auth else {}
    return api.handle(Request(method, path, {}, headers, b""))


def payload(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


# -- liveness --------------------------------------------------------------


def test_healthz_is_public_and_never_touches_state(stack: Stack) -> None:
    response = call(stack.api, "/api/healthz")
    assert response.status == 200
    body = payload(response)
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["version"] == VERSION
    assert body["commit"] == COMMIT
    assert body["uptime_seconds"] >= 0
    # Liveness is deliberately empty: no component is touched.
    assert body["checks"] == {}


def test_healthz_ignores_a_bogus_token(stack: Stack) -> None:
    response = stack.api.handle(
        Request("GET", "/api/healthz", {}, {"authorization": "Bearer wrong"}, b"")
    )
    assert response.status == 200


def test_healthz_is_get_only(stack: Stack) -> None:
    response = call(stack.api, "/api/healthz", method="POST")
    assert response.status == 401


def test_health_alias_stays_authenticated(stack: Stack) -> None:
    assert call(stack.api, "/api/health").status == 401
    response = call(stack.api, "/api/health", auth=True)
    assert response.status == 200
    assert payload(response)["ok"] is True


# -- readiness -------------------------------------------------------------


def test_readyz_requires_auth(stack: Stack) -> None:
    response = call(stack.api, "/api/readyz")
    assert response.status == 401
    assert payload(response)["error"]["code"] == "unauthorized"


def test_readyz_reports_every_component(stack: Stack) -> None:
    response = call(stack.api, "/api/readyz", auth=True)
    assert response.status == 200
    body = payload(response)
    assert body["status"] == "ok"
    assert body["ok"] is True
    assert set(body["checks"]) == set(COMPONENTS)
    assert all(check["ok"] for check in body["checks"].values())
    for name in ("store", "artifacts", "audit", "usage", "registry"):
        assert body["checks"][name]["ok"] is True

    # Probing is read-only: no artifact or audit content is left behind.
    assert list((Path(stack.artifacts.root)).iterdir()) == []
    assert stack.audit.entries() == []
    assert stack.usage.records() == []


def _broken(component: str, stack: Stack, tmp_path: Path) -> HealthProbe:
    not_a_directory = tmp_path / "occupied"
    not_a_directory.write_text("x", encoding="utf-8")
    blocked_parent = not_a_directory / "file.jsonl"
    overrides: dict[str, Any] = {}
    if component == "store":
        overrides["store"] = SimpleNamespace(path=stack.artifacts.root)
    elif component == "artifacts":
        overrides["artifacts"] = SimpleNamespace(root=not_a_directory)
    elif component == "audit":
        overrides["audit"] = SimpleNamespace(path=blocked_parent)
    elif component == "usage":
        overrides["usage_store"] = SimpleNamespace(path=blocked_parent)
    elif component == "provider":
        overrides["provider_check"] = lambda: False
    elif component == "registry":
        overrides["registry"] = BrokenRegistry()
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(component)
    return stack.make_probe(**overrides)


@pytest.mark.parametrize("component", COMPONENTS)
def test_readyz_is_503_when_one_component_is_degraded(
    stack: Stack, tmp_path: Path, component: str
) -> None:
    stack.api.health = _broken(component, stack, tmp_path)
    response = call(stack.api, "/api/readyz", auth=True)

    assert response.status == 503
    body = payload(response)
    assert body["ok"] is False
    assert body["status"] == "degraded"
    assert body["checks"][component]["ok"] is False
    for name in COMPONENTS:
        if name != component:
            assert body["checks"][name]["ok"] is True, (name, body["checks"][name])


def test_readyz_recovers_after_a_degraded_component(stack: Stack, tmp_path: Path) -> None:
    healthy = stack.api.health
    stack.api.health = _broken("registry", stack, tmp_path)
    assert call(stack.api, "/api/readyz", auth=True).status == 503
    stack.api.health = healthy
    assert call(stack.api, "/api/readyz", auth=True).status == 200


# -- provider cache --------------------------------------------------------


def test_provider_probe_is_cached_for_at_most_thirty_seconds(stack: Stack) -> None:
    calls: list[float] = []
    now = [100.0]

    def check() -> bool:
        calls.append(now[0])
        return True

    probe = stack.make_probe(
        provider_check=check, clock=lambda: now[0], started_at=100.0
    )

    first, ready = probe.readiness(runner=stack.api.runner)
    assert ready is True
    assert first["checks"]["provider"]["cached"] is False
    assert len(calls) == 1

    now[0] = 129.0
    second, _ = probe.readiness(runner=stack.api.runner)
    assert len(calls) == 1
    assert second["checks"]["provider"]["cached"] is True
    assert second["checks"]["provider"]["age_seconds"] == 29.0

    now[0] = 130.5
    third, _ = probe.readiness(runner=stack.api.runner)
    assert len(calls) == 2
    assert third["checks"]["provider"]["cached"] is False


def test_provider_failure_is_cached_too(stack: Stack) -> None:
    calls: list[int] = []
    now = [0.0]

    def check() -> bool:
        calls.append(1)
        return False

    probe = stack.make_probe(
        provider_check=check, clock=lambda: now[0], started_at=0.0
    )
    payload_one, ready = probe.readiness(runner=stack.api.runner)
    assert ready is False
    assert payload_one["checks"]["provider"]["detail"] == "unavailable"
    now[0] = 10.0
    probe.readiness(runner=stack.api.runner)
    assert len(calls) == 1


def test_provider_age_is_measured_from_when_the_probe_answered(stack: Stack) -> None:
    now = [100.0]

    def check() -> bool:
        now[0] += 5.0  # a slow upstream holds the check for five seconds
        return True

    probe = stack.make_probe(
        provider_check=check, clock=lambda: now[0], started_at=100.0
    )

    first, ready = probe.readiness(runner=stack.api.runner)
    assert ready is True
    assert first["checks"]["provider"]["cached"] is False
    assert first["checks"]["provider"]["age_seconds"] == 0.0
    assert now[0] == 105.0

    # 29 s after the answer, not 34 s after the request that produced it.
    now[0] = 134.0
    second, _ = probe.readiness(runner=stack.api.runner)
    assert second["checks"]["provider"]["cached"] is True
    assert second["checks"]["provider"]["age_seconds"] == 29.0

    # The 30 s window is counted from the answer, so this is a re-probe.
    now[0] = 135.5
    third, _ = probe.readiness(runner=stack.api.runner)
    assert third["checks"]["provider"]["cached"] is False


def test_a_falsy_but_present_probe_is_used(stack: Stack) -> None:
    chosen = FalsyProbe()
    provider = SimpleNamespace(probe=chosen, health=_health_hook_must_not_run)
    probe = stack.make_probe(provider=provider, provider_check=None)

    body, ready = probe.readiness(runner=stack.api.runner)
    assert ready is True
    assert chosen.calls == 1
    assert body["checks"]["provider"] == {
        "ok": True,
        "cached": False,
        "age_seconds": 0.0,
    }


# -- provider transport ----------------------------------------------------


@pytest.mark.parametrize("status_code", [200, 201, 204, 299])
def test_http_provider_probe_accepts_a_2xx(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    calls, client_kwargs = _fake_httpx(monkeypatch, status_code=status_code)

    asyncio.run(_http_provider_probe("https://upstream.example/v1/", "key-123"))

    assert calls == [
        (
            "https://upstream.example/v1/models",
            {"Authorization": "Bearer key-123"},
        )
    ]
    # A redirect is never chased: it would be a different upstream answering.
    assert client_kwargs["follow_redirects"] is False


@pytest.mark.parametrize(
    "status_code", [199, 301, 302, 304, 307, 400, 401, 403, 429, 500, 503]
)
def test_http_provider_probe_rejects_every_non_2xx(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    calls, _ = _fake_httpx(monkeypatch, status_code=status_code)

    with pytest.raises(OSError):
        asyncio.run(_http_provider_probe("https://upstream.example/v1", None))

    assert calls == [("https://upstream.example/v1/models", {})]


def test_provider_check_walks_the_http_path_for_a_configured_upstream(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = _fake_httpx(monkeypatch, status_code=204)
    provider = SimpleNamespace(
        config=SimpleNamespace(
            base_url="https://upstream.example/v1", api_key="key-abc"
        )
    )

    body, ready = stack.make_probe(
        provider=provider, provider_check=None
    ).readiness(runner=stack.api.runner)
    assert ready is True
    assert body["checks"]["provider"] == {
        "ok": True,
        "cached": False,
        "age_seconds": 0.0,
    }
    assert calls == [
        ("https://upstream.example/v1/models", {"Authorization": "Bearer key-abc"})
    ]

    # A redirect is not a healthy upstream: the same wiring yields a 503.
    _fake_httpx(monkeypatch, status_code=302)
    stack.api.health = stack.make_probe(provider=provider, provider_check=None)
    response = call(stack.api, "/api/readyz", auth=True)
    assert response.status == 503
    assert payload(response)["checks"]["provider"]["detail"] == "unavailable"


# -- latency ---------------------------------------------------------------


def test_healthz_and_readyz_meet_their_latency_budgets(stack: Stack) -> None:
    # Warm the cache so the measurement is about steady-state scrapes.
    assert call(stack.api, "/api/readyz", auth=True).status == 200

    def p95(samples: int, path: str, auth: bool) -> float:
        timings: list[float] = []
        for _ in range(samples):
            started = time.perf_counter()
            response = call(stack.api, path, auth=auth)
            timings.append(time.perf_counter() - started)
            assert response.status == 200
        timings.sort()
        return timings[int(round(0.95 * (len(timings) - 1)))]

    assert p95(40, "/api/healthz", False) < 0.050
    assert p95(40, "/api/readyz", True) < 0.250


# -- disclosure ------------------------------------------------------------


def test_bodies_never_leak_tokens_paths_or_exception_text(
    stack: Stack, tmp_path: Path
) -> None:
    secret = "owner-token-super-secret-123456"
    stack.api.auth_token = secret
    auth = {"authorization": f"Bearer {secret}"}

    def get(path: str) -> Any:
        return stack.api.handle(Request("GET", path, {}, auth, b""))

    bodies = [
        call(stack.api, "/api/healthz").body,
        get("/api/readyz").body,
    ]
    # A degraded run must not turn an exception message into a path leak.
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    stack.api.health = stack.make_probe(
        audit=SimpleNamespace(path=blocked / "audit.jsonl")
    )
    degraded = get("/api/readyz")
    assert degraded.status == 503
    assert payload(degraded)["checks"]["audit"]["detail"] == "unavailable"
    bodies.append(degraded.body)

    for body in bodies:
        text = body.decode("utf-8")
        assert secret not in text
        assert str(tmp_path) not in text
        assert "Traceback" not in text
        assert "NEBIUS" not in text


def test_uptime_moves_forward(stack: Stack) -> None:
    first = payload(call(stack.api, "/api/healthz"))["uptime_seconds"]
    time.sleep(0.02)
    second = payload(call(stack.api, "/api/healthz"))["uptime_seconds"]
    assert second > first


# -- production wiring -----------------------------------------------------


def _server_env(root: Path) -> dict[str, str]:
    """The minimal documented environment ``chat.server`` reads."""

    return {
        "NEBIUS_API_KEY": "test-api-key-never-printed-9876",
        "NEBIUS_TOKEN_FACTORY_BASE_URL": "http://127.0.0.1:9/v1",
        "KELP_AUTH_TOKEN": TOKEN,
        "KELP_DB_PATH": str(root / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(root / "artifacts"),
        "KELP_AUDIT_DIR": str(root / "audit"),
        "KELP_USAGE_DIR": str(root / "usage"),
        "KELP_WORKSPACE_ROOT": str(root / "workspace"),
        "KELP_HOST": "127.0.0.1",
        "KELP_PORT": "0",
    }


def test_from_service_skips_only_a_provider_without_an_upstream(stack: Stack) -> None:
    probe = HealthProbe.from_service(stack.service, audit=stack.audit)

    body, ready = probe.readiness(runner=stack.api.runner)
    assert ready is True
    for name in ("store", "artifacts", "audit", "usage", "registry"):
        assert body["checks"][name] == {"ok": True}, name
    # A test-double provider has no config, probe, or health hook, so the one
    # check with nothing to reach reports the documented skip instead of a
    # false alarm.
    assert body["checks"]["provider"]["ok"] is True
    assert body["checks"]["provider"]["detail"] == "not_configured"


def test_build_app_wires_every_check_to_a_real_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = _fake_httpx(monkeypatch, status_code=200)
    env = _server_env(tmp_path)
    app = build_app(
        ServerConfig.from_env(env), provider=UpstreamProvider(), environ=env
    )
    try:
        response = app.api.handle(Request("GET", "/api/readyz", {}, dict(AUTH), b""))
        assert response.status == 200
        body = payload(response)
        assert body["status"] == "ok"
        assert set(body["checks"]) == set(COMPONENTS)
        for name in COMPONENTS:
            check = body["checks"][name]
            assert check["ok"] is True, (name, check)
            assert check.get("detail") != "not_configured", (name, check)
        # The provider check reached the configured upstream, not a stub hook.
        assert body["checks"]["provider"]["cached"] is False
        assert calls == [
            (
                "https://upstream.example/v1/models",
                {"Authorization": "Bearer upstream-key-123456"},
            )
        ]
        # Readiness checks the server's own audit log, which is what a
        # deployment needs: boot recovery writes through the same object.
        assert app.api.health.audit is app.audit
        assert app.api.health.usage_store is not None
    finally:
        app.close()
