"""Per-tenant secret resolution (P4-1).

Every value below is a fake. The tests assert the two properties that matter
more than resolution itself:

* a resolver bound to tenant A has no path to tenant B's value; and
* no new object's ``repr``, log line, audit row, or SQLite byte carries a value.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from chat.logging import configure_logging, log
from kernel.config import KernelConfig
from kernel.providers import (
    ChatRequest,
    ProviderFailure,
    ProviderPricing,
    ProviderSpec,
    build_provider,
)
from kernel.secrets import (
    EnvSecretResolver,
    MissingSecret,
    SecretFileError,
    SecretRef,
    TenantSecretResolver,
)
from openshell.audit import AuditLog

from .chatkit import (
    executor_factory,
    make_service,
    send_and_wait,
    text_script,
    tool_script,
)

FAKE_ENV = "sk-test-fake-env-secret"
FAKE_A = "sk-test-fake-tenant-a-secret"
FAKE_B = "sk-test-fake-tenant-b-secret"
PROVIDER_KEY = "KELP_PROVIDER_API_KEY"
BASE_URL = "https://provider.example.test/v1"


def _write_secret_file(
    tmp_path: Path,
    payload: dict[str, Any],
    *,
    mode: int = 0o600,
) -> Path:
    path = tmp_path / "tenant-secrets.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _spec(api_key_env: str = PROVIDER_KEY) -> ProviderSpec:
    return ProviderSpec(
        name="test-provider",
        base_url=BASE_URL,
        api_key_env=api_key_env,
        default_model="test-model",
        pricing=ProviderPricing(0.0, 0.0),
    )


def _drain(client: Any) -> list[Any]:
    async def run() -> list[Any]:
        async with client:
            return [
                event
                async for event in client.stream(
                    ChatRequest(model="test-model", messages=[])
                )
            ]

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Environment resolver
# ---------------------------------------------------------------------------


def test_env_resolver_reads_an_explicit_mapping() -> None:
    resolver = EnvSecretResolver({PROVIDER_KEY: FAKE_ENV})
    assert resolver.resolve(PROVIDER_KEY) == FAKE_ENV

    with pytest.raises(MissingSecret) as missing:
        resolver.resolve("UNCONFIGURED_NAME")
    assert missing.value.name == "UNCONFIGURED_NAME"
    assert FAKE_ENV not in str(missing.value)


def test_env_resolver_can_read_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROVIDER_KEY, FAKE_ENV)
    assert EnvSecretResolver().resolve(PROVIDER_KEY) == FAKE_ENV

    monkeypatch.setenv(PROVIDER_KEY, "   ")
    with pytest.raises(MissingSecret):
        EnvSecretResolver().resolve(PROVIDER_KEY)


# ---------------------------------------------------------------------------
# Tenant file resolver
# ---------------------------------------------------------------------------


def test_tenant_file_must_be_mode_0600(tmp_path: Path) -> None:
    path = _write_secret_file(
        tmp_path, {"owner": {PROVIDER_KEY: FAKE_A}}, mode=0o644
    )
    with pytest.raises(SecretFileError) as refused:
        TenantSecretResolver(path, tenant_id="owner")
    message = str(refused.value)
    assert "0600" in message
    assert FAKE_A not in message


def test_tenant_file_resolves_and_describes_without_the_value(
    tmp_path: Path,
) -> None:
    path = _write_secret_file(tmp_path, {"owner": {PROVIDER_KEY: FAKE_A}})
    resolver = TenantSecretResolver(path, tenant_id="owner")

    assert resolver.resolve(PROVIDER_KEY) == FAKE_A
    ref = resolver.ref(PROVIDER_KEY)
    assert ref.name == PROVIDER_KEY
    assert ref.version == int(path.stat().st_mtime_ns)
    metadata = ref.as_metadata()
    assert metadata == {
        "name": PROVIDER_KEY,
        "source": "tenant-file",
        "version": ref.version,
    }
    assert "value" not in metadata
    assert FAKE_A not in repr(metadata)

    for rendered in (
        repr(resolver),
        str(resolver),
        repr(ref),
        str(ref),
    ):
        assert FAKE_A not in rendered


def test_tenant_isolation_has_no_cross_tenant_path(tmp_path: Path) -> None:
    path = _write_secret_file(
        tmp_path,
        {
            "owner": {PROVIDER_KEY: FAKE_A},
            "tenant-b": {"TENANT_B_ONLY_KEY": FAKE_B},
        },
    )
    owner = TenantSecretResolver(path, tenant_id="owner")
    other = TenantSecretResolver(path, tenant_id="tenant-b")

    assert owner.resolve(PROVIDER_KEY) == FAKE_A
    assert other.resolve("TENANT_B_ONLY_KEY") == FAKE_B

    with pytest.raises(MissingSecret) as missing:
        owner.resolve("TENANT_B_ONLY_KEY")
    assert missing.value.name == "TENANT_B_ONLY_KEY"
    assert missing.value.tenant_id == "owner"
    assert FAKE_B not in str(missing.value)
    assert FAKE_B not in repr(missing.value)

    # The owner's in-memory map and scrubber set contain only its own value.
    assert owner.secret_values() == (FAKE_A,)
    assert FAKE_B not in owner.secret_values()
    assert FAKE_B not in repr(owner)

    unknown = TenantSecretResolver(path, tenant_id="tenant-c")
    with pytest.raises(MissingSecret):
        unknown.resolve(PROVIDER_KEY)


def test_missing_secret_error_shape_is_typed_and_scoped() -> None:
    error = MissingSecret(PROVIDER_KEY, tenant_id="owner")
    assert isinstance(error, LookupError)
    assert error.name == PROVIDER_KEY
    assert error.tenant_id == "owner"
    assert PROVIDER_KEY in str(error)
    assert "owner" in str(error)
    assert "secret" in str(error)


# ---------------------------------------------------------------------------
# Registry: credentials come from the passed resolver, never os.environ
# ---------------------------------------------------------------------------


def test_provider_spec_resolves_through_the_passed_resolver() -> None:
    captured: dict[str, KernelConfig] = {}

    def factory(config: KernelConfig) -> object:
        captured["config"] = config
        return object()

    build_provider(
        _spec(),
        factory=factory,
        resolver=EnvSecretResolver({PROVIDER_KEY: FAKE_ENV}),
    )
    config = captured["config"]
    assert config.api_key == FAKE_ENV
    assert FAKE_ENV not in repr(config)


def test_provider_spec_resolver_miss_does_not_fall_back_to_environ() -> None:
    """A resolver miss is final even when the caller supplied a mapping."""

    captured: dict[str, KernelConfig] = {}

    def factory(config: KernelConfig) -> object:
        captured["config"] = config
        return object()

    build_provider(
        _spec(),
        factory=factory,
        resolver=EnvSecretResolver({}),
        environ={PROVIDER_KEY: FAKE_ENV},
    )
    assert captured["config"].api_key is None


def test_kernel_config_resolves_through_the_passed_resolver() -> None:
    captured: dict[str, KernelConfig] = {}

    def factory(config: KernelConfig) -> object:
        captured["config"] = config
        return object()

    build_provider(
        KernelConfig(base_url=BASE_URL, api_key=None),
        factory=factory,
        resolver=EnvSecretResolver({PROVIDER_KEY: FAKE_ENV}),
    )
    assert captured["config"].api_key == FAKE_ENV


def test_tenant_resolver_wins_over_the_config_key(tmp_path: Path) -> None:
    """A supplied resolver is authoritative over an ambient config key.

    The server passes ``api_key=None`` when a tenant secrets file is configured,
    but a ``KernelConfig`` built from the environment still carries that key.
    The resolver must win, or the tenant file would be silently bypassed.
    """

    path = _write_secret_file(tmp_path, {"owner": {PROVIDER_KEY: FAKE_A}})
    resolver = TenantSecretResolver(path, tenant_id="owner")
    captured: dict[str, KernelConfig] = {}

    def factory(config: KernelConfig) -> object:
        captured["config"] = config
        return object()

    build_provider(
        KernelConfig(base_url=BASE_URL, api_key=FAKE_ENV),
        factory=factory,
        resolver=resolver,
    )
    assert captured["config"].api_key == FAKE_A
    assert FAKE_ENV not in repr(captured["config"])


def test_resolver_miss_does_not_fall_back_to_the_config_key(
    tmp_path: Path,
) -> None:
    """A resolver that cannot answer fails closed, not to the ambient key."""

    path = _write_secret_file(tmp_path, {"owner": {}})
    resolver = TenantSecretResolver(path, tenant_id="owner")
    captured: dict[str, KernelConfig] = {}

    def factory(config: KernelConfig) -> object:
        captured["config"] = config
        return object()

    build_provider(
        KernelConfig(base_url=BASE_URL, api_key=FAKE_ENV),
        factory=factory,
        resolver=resolver,
    )
    assert captured["config"].api_key is None


def test_missing_key_is_typed_and_makes_no_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a missing key must not reach the network")

    client = build_provider(
        _spec(),
        resolver=EnvSecretResolver({}),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    events = _drain(client)
    assert len(events) == 1
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "missing_api_key"
    assert events[0].retryable is False


def test_registry_never_reads_process_environment_implicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROVIDER_KEY, FAKE_ENV)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the library must not inherit os.environ")

    client = build_provider(
        _spec(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    events = _drain(client)
    assert len(events) == 1
    assert isinstance(events[0], ProviderFailure)
    assert events[0].code == "missing_api_key"


# ---------------------------------------------------------------------------
# Hard security assertions: no value in repr, logs, audit, or SQLite
# ---------------------------------------------------------------------------


def test_no_secret_value_reaches_repr_logs_audit_or_sqlite(tmp_path: Path) -> None:
    path = _write_secret_file(tmp_path, {"owner": {PROVIDER_KEY: FAKE_A}})
    resolver = TenantSecretResolver(path, tenant_id="owner")
    ref = resolver.ref(PROVIDER_KEY)

    # Every new object's repr and the metadata projection carry no value.
    for rendered in (
        repr(resolver),
        str(resolver),
        repr(ref),
        repr(SecretRef(PROVIDER_KEY, "env")),
        repr(EnvSecretResolver({PROVIDER_KEY: FAKE_ENV})),
        repr(MissingSecret(PROVIDER_KEY, tenant_id="owner")),
        repr(KernelConfig(base_url=BASE_URL, tenant_secrets_file=str(path))),
    ):
        assert FAKE_A not in rendered
        assert FAKE_ENV not in rendered
        assert FAKE_B not in rendered

    # Structured logging scrubs the resolver values.
    stream = io.StringIO()
    try:
        configure_logging(
            level="info",
            secret_values=resolver.secret_values(),
            stream=stream,
        )
        log("provider_key_test", reason=f"loaded {FAKE_A}")
    finally:
        configure_logging(
            level="info", secret_values=(), stream=None, enabled=False
        )
    # Positive control: the probe line reached the sink with the value replaced,
    # so the absence check below fails if the scrubber stops covering it.
    logged = json.loads(stream.getvalue())
    assert logged["event"] == "provider_key_test"
    assert logged["reason"] == "loaded [redacted]"
    assert FAKE_A not in stream.getvalue()

    # Audit rows carry the key name and metadata only; even a caller that
    # mistakenly passes a value is scrubbed by the same set.
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, secret_values=resolver.secret_values())
    audit.record(
        "provider_key_loaded",
        extra={"secret_name": ref.name, "secret_ref": ref.as_metadata()},
    )
    audit.record("provider_key_mistake", extra={"api_key": FAKE_A})
    raw_audit = audit_path.read_text(encoding="utf-8")
    # Positive control: the mistaken row was written, with the value replaced.
    assert "provider_key_mistake" in raw_audit
    assert "[redacted]" in raw_audit
    assert FAKE_A not in raw_audit
    assert PROVIDER_KEY in raw_audit
    assert "value" not in raw_audit.splitlines()[0]

    # Durable chat state: route the resolver values through the one store path
    # that could carry them. ChatService writes tool arguments and results into
    # message blocks and events; ``scrub_values`` is what keeps a tenant value
    # out of them. (An unrelated ``create_thread`` cannot fail this check.)
    async def executor(call: object) -> dict:
        return {"ok": True, "output": {"echo": f"leaked {FAKE_A}"}}

    service, chat_store, _provider = make_service(
        tmp_path / "service",
        [tool_script("stub.tool", {"q": FAKE_A}), text_script("done")],
        scrub_values=tuple(resolver.secret_values()),
        tool_executor_factory=executor_factory(executor),
    )
    try:
        thread = service.create_thread()

        async def scenario() -> tuple[str, str]:
            payload = await send_and_wait(service, thread["id"], "go")
            blocks = chat_store.get_message(payload["assistant_message_id"])["blocks"]
            return json.dumps(blocks), json.dumps(chat_store.events_after(thread["id"]))

        blocks_json, events_json = asyncio.run(scenario())
    finally:
        chat_store.close()

    # Positive control: the tool turn was persisted and carries the marker.
    assert "stub.tool" in blocks_json
    assert "[redacted]" in blocks_json
    assert FAKE_A not in blocks_json
    assert FAKE_A not in events_json
    db_files = [c for c in (tmp_path / "service").glob("chat.db*") if c.is_file()]
    assert db_files
    for candidate in db_files:
        assert FAKE_A.encode() not in candidate.read_bytes()
