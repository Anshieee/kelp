"""KEL-12 M2-B: liveness and readiness contract (§15.6)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from chat import ArtifactRegistry, ChatAPI, ChatService, ChatStore, Request
from chat.api import HealthProbe
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


class FakeProvider:
    """A provider double that must never be asked to touch the network."""

    def __init__(self) -> None:
        self.calls = 0

    def probe(self) -> bool:
        self.calls += 1
        return True


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
