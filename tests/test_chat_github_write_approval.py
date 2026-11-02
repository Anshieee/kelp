"""End-to-end chat approval boundary for the scoped GitHub writer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from chat import ChatService, ChatStore
from chat.errors import ChatError
from chat.server import _tool_specs
from kernel.provider import StreamEnd, ToolCallReady, UsageReport
from openshell.audit import AuditLog
from skills import SkillRegistry
from skills.builtin.github_write import handler

from .chatkit import StubProvider, wait_until

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "approval-test-github-token"
ARGS: dict[str, Any] = {
    "repo": "Anshieee/kelp",
    "base": "main",
    "branch": "agent/approved-change",
    "title": "Approved change",
    "body": "Opened only after approval.",
    "files": [{"path": "README.md", "content": "Approved content.\n"}],
}
SHA = "1" * 40


def _transport(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": SHA}})
        if path.endswith("/git/refs") and request.method == "POST":
            return httpx.Response(201, json={"ref": "refs/heads/agent/approved-change"})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "2" * 40})
        if path.endswith(f"/git/commits/{SHA}"):
            return httpx.Response(200, json={"tree": {"sha": "3" * 40}})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "3" * 40})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "4" * 40})
        if path.endswith("/git/refs/heads/agent/approved-change"):
            return httpx.Response(200, json={"object": {"sha": "4" * 40}})
        if path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={"number": 73, "html_url": "https://github.com/Anshieee/kelp/pull/73"},
            )
        return httpx.Response(500)

    monkeypatch.setattr(handler, "_TRANSPORT_FACTORY", lambda: httpx.MockTransport(respond))
    return requests


def _setup(tmp_path: Path, script: list[Any], *, ttl: float = 600.0) -> tuple[
    ChatService, ChatStore, StubProvider, AuditLog
]:
    registry = SkillRegistry.load(ROOT / "skills" / "builtin")
    store = ChatStore(tmp_path / "chat.db")
    provider = StubProvider([script, [StreamEnd("stop")]])
    audit = AuditLog(tmp_path / "audit.jsonl", secret_values=(TOKEN,))
    service = ChatService(
        store=store,
        provider=provider,
        registry=registry,
        tools=_tool_specs(registry),
        secrets={"GITHUB_TOKEN": TOKEN},
        audit=audit,
        approval_ttl_seconds=ttl,
    )
    return service, store, provider, audit


def _script(args: dict[str, Any] = ARGS) -> list[Any]:
    return [
        ToolCallReady(0, "call_github_write", "github.write", dict(args)),
        UsageReport(10, 5, 15),
        StreamEnd("tool_calls"),
    ]


def test_model_is_offered_github_write_and_pending_approval_has_no_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _transport(monkeypatch)
    service, store, provider, audit = _setup(tmp_path, _script())
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload, _ = await service.send(thread["id"], text="Open a PR", client_turn_id="ct-gw-pending")
            cards = await wait_until(lambda: store.list_approvals(status="pending"))
            card = cards[0]
            assert requests == []
            provider_request = provider.requests[0]
            assert any(
                message.get("role") == "user" and message.get("content") == "Open a PR"
                for message in provider_request.messages
            )
            offered = provider_request.tools
            assert any(item["function"]["name"] == "github.write" for item in offered)
            assert "After you approve" in card["rationale"]
            assert "Anshieee/kelp" in card["rationale"]
            assert card["command"] is None
            assert card["effects"] == ["write", "network"]
            assert card["scopes"]["hosts"] == ["api.github.com"]
            assert card["scopes"]["repos"] == ["Anshieee/kelp"]
            assert card["arguments_digest"].startswith("sha256:")
            service.decide(card["id"], "approve")
            await service.wait_for_turn(payload["turn_id"])
            assert sum(
                request.method == "POST" and request.url.path.endswith("/git/refs")
                for request in requests
            ) == 1
            assert sum(
                request.method == "POST" and request.url.path.endswith("/git/commits")
                for request in requests
            ) == 1
            assert sum(
                request.method == "POST" and request.url.path.endswith("/pulls")
                for request in requests
            ) == 1
            assert len(requests) == 8
            assert all(request.url.host == "api.github.com" for request in requests)
            settled = store.get_approval(card["id"])
            assert settled["status"] == "consumed"

            # A consumed approval is single-use and cannot replay the writer.
            with pytest.raises(ChatError) as replay:
                service.decide(card["id"], "approve")
            assert replay.value.code == "already_used"
            assert len(requests) == 8

        asyncio.run(scenario())
        assert any(row.get("event") == "skill_egress" for row in audit.entries())
    finally:
        store.close()


@pytest.mark.parametrize("outcome", ["deny", "expire", "cancel"])
def test_denial_expiry_and_cancel_never_reach_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    requests = _transport(monkeypatch)
    service, store, _provider, _audit = _setup(
        tmp_path,
        _script(),
        ttl=0.03 if outcome == "expire" else 600.0,
    )
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload, _ = await service.send(thread["id"], text="Open a PR", client_turn_id=f"ct-gw-{outcome}")
            card = (await wait_until(lambda: store.list_approvals(status="pending")))[0]
            if outcome == "deny":
                service.decide(card["id"], "deny")
            elif outcome == "cancel":
                await service.stop(payload["turn_id"])
            await service.wait_for_turn(payload["turn_id"])
            assert requests == []
            final = store.get_approval(card["id"])
            assert final["status"] == {"deny": "denied", "expire": "expired", "cancel": "expired"}[outcome]

        asyncio.run(scenario())
    finally:
        store.close()


def test_approval_waiter_rejects_a_changed_argument_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = _transport(monkeypatch)
    service, store, _provider, _audit = _setup(tmp_path, _script())
    try:
        thread = service.create_thread()

        async def scenario() -> None:
            payload, _ = await service.send(thread["id"], text="Open a PR", client_turn_id="ct-gw-mismatch")
            card = (await wait_until(lambda: store.list_approvals(status="pending")))[0]
            # Simulate a stale/corrupt persisted approval: the decision is for
            # another argument set, so the waiter must fail closed.
            service._record_resolution(  # noqa: SLF001
                None, card["id"], "approved", executed=False,
                allowed_from=("pending",),
            )
            altered = dict(card)
            altered["arguments_digest"] = "sha256:" + "0" * 64
            store.save_approval(altered)
            waiter = service._waiters[card["id"]]  # noqa: SLF001
            waiter.set()
            await service.wait_for_turn(payload["turn_id"])
            assert requests == []
            assert store.get_approval(card["id"])["status"] == "expired"

        asyncio.run(scenario())
    finally:
        store.close()
