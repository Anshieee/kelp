"""The opt-in Local runtime profile composed by ``chat.server.build_app``.

No test here binds a socket, starts a scheduler, or contacts a provider. Each
one calls ``build_app(..., local_only=True)`` with a disposable state root and
an injected or intercepted provider, so the assertions are about composition
and side effects only. The process-level HTTP/SSE acceptance belongs to the
later supervisor wiring; the hosted default is covered by ``test_server.py``
and ``test_payment_server_routes.py``.
"""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import chat.server as server
from chat.server import ConfigError, ServerConfig, build_app
from kernel.providers import OpenAICompatibleClient

from .chatkit import StubProvider, tool_script

AUTH = "local-owner-bearer-0123456789"
CSRF = "local-csrf-token-0123456789"
API_KEY = "local-provider-key-0123456789"
SESSION = "local-session-secret-0123456789"
BASE_URL = "http://127.0.0.1:9/v1"


class _NoopProvider:
    """A provider that must never be asked to stream in these tests."""

    async def stream(self, request: Any) -> Any:
        raise AssertionError("no turn should run")
        yield  # pragma: no cover - makes this an async generator


class _EmptyRegistry:
    """The smallest hosted registry double: no skills, no tools."""

    def names(self) -> tuple[str, ...]:
        return ()


def _env(root: Path, **overrides: str) -> dict[str, str]:
    env = {
        "KELP_PROVIDER_API_KEY": API_KEY,
        "KELP_PROVIDER_BASE_URL": BASE_URL,
        "KELP_AUTH_TOKEN": AUTH,
        "KELP_CSRF_TOKEN": CSRF,
        "KELP_SESSION_SECRET": SESSION,
        "KELP_DB_PATH": str(root / "db" / "chat.db"),
        "KELP_ARTIFACT_DIR": str(root / "artifacts"),
        "KELP_AUDIT_DIR": str(root / "audit"),
        "KELP_USAGE_DIR": str(root / "usage"),
        "KELP_WORKSPACE_ROOT": str(root / "workspace"),
        "KELP_HOST": "127.0.0.1",
        "KELP_PORT": "0",
        "KELP_SCHEDULER_ENABLED": "0",
        "KELP_CREDENTIALS_ENABLED": "0",
    }
    env.update(overrides)
    return env


def _assert_no_state(config: ServerConfig) -> None:
    for path in (
        config.db_path.parent,
        config.db_path,
        config.db_path.with_name(config.db_path.name + ".lock"),
        config.artifact_dir,
        config.audit_dir,
        config.usage_dir,
        config.workspace_root,
    ):
        assert not path.exists(), f"local validation created {path}"


def test_local_only_requires_the_explicit_composed_environ_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(tmp_path)
    config = ServerConfig.from_env(env)
    # An ambient mapping must not stand in for the caller's composed mapping.
    monkeypatch.setenv("KELP_DB_PATH", str(tmp_path / "ambient" / "chat.db"))

    with pytest.raises(ConfigError) as raised:
        build_app(config, provider=_NoopProvider(), local_only=True)

    assert raised.value.code == "invalid_config"
    _assert_no_state(config)


def test_local_only_rejects_a_nonowner_nonloopback_or_nonzero_port_profile_before_state(
    tmp_path: Path,
) -> None:
    base = ServerConfig.from_env(_env(tmp_path))
    cases = [
        replace(base, host="0.0.0.0"),
        replace(base, port=8765),
        replace(base, auth_token=""),
        replace(base, kernel=replace(base.kernel, tenant_id="tenant-b")),
        replace(
            base,
            kernel=replace(
                base.kernel,
                tenant_secrets_file=str(tmp_path / "tenant-secrets.json"),
            ),
        ),
    ]

    for config in cases:
        with pytest.raises(ConfigError) as raised:
            build_app(
                config,
                provider=_NoopProvider(),
                environ=_env(tmp_path),
                local_only=True,
            )
        assert raised.value.code == "invalid_config", config
        _assert_no_state(config)


def test_local_runtime_omits_skill_discovery_schedules_payments_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(
        tmp_path,
        KELP_SKILL_ROOT=str(tmp_path / "skills-that-must-not-be-read"),
    )
    config = ServerConfig.from_env(env)
    provider = _NoopProvider()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the Local profile must not construct this component")

    for name in (
        "CredentialBroker",
        "ProactiveConfig",
        "ProactiveRunner",
        "RazorpayCheckout",
        "RazorpayWallet",
        "ScheduleStore",
        "SchedulerRuntime",
        "CheckoutAPI",
        "WalletAPI",
        "WalletLedger",
        "build_provider",
    ):
        monkeypatch.setattr(server, name, forbidden)
    monkeypatch.setattr(server.SkillRegistry, "load", staticmethod(forbidden))

    app = build_app(config, provider=provider, environ=env, local_only=True)
    try:
        assert app.provider is provider
        assert app.service._registry is None
        assert app.service._tools == []
        assert app.service._budget.max_tool_calls == 0
        assert app.service._tool_executor_factory is None
        assert app.scheduler is None
        assert app.proactive is None
        assert app.wallet_api is None
        assert app.checkout_api is None
        assert app.wallet_ledger is None
        # The state, audit, retention, and recovery surfaces stay.
        assert app.store is not None
        assert app.audit is not None
        assert app.maintenance is not None
        assert app.recovery is not None
        assert app.lock.held
    finally:
        app.close()
    assert app.lock.held is False


def test_local_profile_reads_entitlements_and_rate_limits_from_the_explicit_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _env(
        tmp_path,
        KELP_ENTITLEMENT_PLAN="local-plan",
        KELP_ENTITLEMENT_PLANS_JSON=json.dumps(
            {
                "local-plan": {
                    "window": "day",
                    "max_turns": 2,
                    "allowed_task_types": ["chat"],
                }
            }
        ),
        KELP_RATE_LIMIT_ENABLED="0",
    )
    config = ServerConfig.from_env(env)
    # If either default constructor read ambient config instead of ``source``,
    # the invalid plan JSON would abort boot and the limiter would be enabled.
    monkeypatch.setenv("KELP_ENTITLEMENT_PLANS_JSON", "not json")
    monkeypatch.setenv("KELP_RATE_LIMIT_ENABLED", "1")

    app = build_app(config, provider=_NoopProvider(), environ=env, local_only=True)
    try:
        assert app.service.entitlements.default_plan_id == "local-plan"
        assert app.api.limiter.enabled is False
    finally:
        app.close()


def test_local_profile_redacts_provider_and_frame_secrets_without_skill_credentials(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    app = build_app(
        ServerConfig.from_env(env),
        provider=_NoopProvider(),
        environ=env,
        local_only=True,
    )
    try:
        for secret in (API_KEY, AUTH, CSRF, SESSION):
            assert app.audit.scrub(secret) == "[redacted]"
            assert secret in app.service._secret_values
        # Redaction-only: nothing in this set is offered as a skill credential.
        assert app.service._secrets == {}
    finally:
        app.close()


def test_local_provider_factory_seals_the_transport_and_hosted_keeps_the_module_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_build_provider(config: Any, *, factory: Any = None, **kwargs: Any) -> Any:
        captured["factory"] = factory
        captured["kwargs"] = kwargs
        return _NoopProvider()

    monkeypatch.setattr(server, "build_provider", fake_build_provider)
    env = _env(tmp_path / "local")
    app = build_app(ServerConfig.from_env(env), environ=env, local_only=True)
    try:
        factory = captured["factory"]
        assert isinstance(factory, functools.partial)
        assert factory.func is OpenAICompatibleClient
        assert factory.keywords == {"trust_env": False}
        assert captured["kwargs"]["environ"] is env
    finally:
        app.close()

    captured.clear()
    monkeypatch.setattr(
        server.SkillRegistry,
        "load",
        staticmethod(lambda *_args, **_kwargs: _EmptyRegistry()),
    )
    hosted_env = _env(tmp_path / "hosted")
    hosted = build_app(ServerConfig.from_env(hosted_env), environ=hosted_env)
    try:
        assert captured["factory"] is server.ProviderClient
    finally:
        hosted.close()


def test_local_zero_tool_budget_blocks_an_unexpected_tool_without_an_executor(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    provider = StubProvider([tool_script("unexpected.tool", {"argument": "value"})])
    app = build_app(
        ServerConfig.from_env(env),
        provider=provider,
        environ=env,
        local_only=True,
    )
    try:
        thread = app.service.create_thread()

        async def scenario() -> None:
            payload, created = await app.service.send(
                thread["id"],
                text="call a tool",
                client_turn_id="ct-local-zero-tools",
            )
            assert created is True
            await app.service.wait_for_turn(payload["turn_id"], timeout=5.0)

            events = app.store.events_after(thread["id"])
            assert not [
                event for event in events if event["type"] == "tool_call_started"
            ]
            finished = [event for event in events if event["type"] == "turn_finished"]
            assert finished[-1]["reason"] == "budget"
            assert finished[-1]["tool_calls"] == 0
            message = app.store.get_message(payload["assistant_message_id"])
            assert message["status"] == "error"
            assert not [
                block for block in message["blocks"] if block.get("type") == "tool_call"
            ]

        asyncio.run(scenario())
    finally:
        app.close()
