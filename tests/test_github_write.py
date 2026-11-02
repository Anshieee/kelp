"""Offline contract and approval tests for ``github.write``."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest

from skills import SkillRegistry
from skills.builtin.github_write import handler

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-secret-token-never-return"
BASE_SHA = "1" * 40
BLOB_SHA = "2" * 40
TREE_SHA = "3" * 40
COMMIT_SHA = "4" * 40


def registry() -> SkillRegistry:
    return SkillRegistry.load(ROOT / "skills" / "builtin")


def arguments() -> dict[str, Any]:
    return {
        "repo": "Anshieee/kelp",
        "base": "main",
        "branch": "agent/add-readme-note",
        "title": "Add a README note",
        "body": "A small approved update.",
        "files": [{"path": "README.md", "content": "A note.\n"}],
    }


def invoke(
    target: SkillRegistry,
    args: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return asyncio.run(target.invoke("github.write", args, **kwargs))


def install_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
    requests: list[httpx.Request],
) -> None:
    def response_for(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": BASE_SHA}})
        if request.method == "POST" and path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/agent/add-readme-note"})
        if request.method == "POST" and path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": BLOB_SHA})
        if request.method == "GET" and path.endswith(f"/git/commits/{BASE_SHA}"):
            return httpx.Response(200, json={"tree": {"sha": TREE_SHA}})
        if request.method == "POST" and path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": TREE_SHA})
        if request.method == "POST" and path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": COMMIT_SHA})
        if request.method == "PATCH" and path.endswith("/git/refs/heads/agent/add-readme-note"):
            return httpx.Response(200, json={"object": {"sha": COMMIT_SHA}})
        if request.method == "POST" and path.endswith("/pulls"):
            return httpx.Response(
                201,
                json={"number": 42, "html_url": "https://github.com/Anshieee/kelp/pull/42"},
            )
        return httpx.Response(500, json={"message": "unexpected request"})

    monkeypatch.setattr(handler, "_TRANSPORT_FACTORY", lambda: httpx.MockTransport(response_for))


def test_manifest_loads_with_scoped_write_and_approval() -> None:
    skill = registry().get("github.write")

    assert skill.effects == ("write", "network")
    assert skill.requires_approval is True
    assert skill.scopes["hosts"] == ("api.github.com",)
    assert skill.scopes["repos"] == ("Anshieee/kelp",)
    assert skill.declared_secrets == ("GITHUB_TOKEN",)


def test_approval_required_and_denied_paths_do_not_touch_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    install_fake_transport(monkeypatch, requests)
    target = registry()
    args = arguments()
    secrets = {"GITHUB_TOKEN": TOKEN}

    no_hook = invoke(target, args, secrets=secrets)
    denied = invoke(target, args, approval=lambda _request: _deny(), secrets=secrets)

    assert no_hook["error"]["code"] == "approval_required"
    assert denied["error"]["code"] == "approval_denied"
    assert requests == []


async def _deny() -> bool:
    return False


def test_approved_call_creates_branch_commit_and_pull_request_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    install_fake_transport(monkeypatch, requests)

    def tripwire(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network socket use is forbidden in this offline test")

    monkeypatch.setattr(socket.socket, "connect", tripwire)
    monkeypatch.setattr(socket.socket, "connect_ex", tripwire)
    result = invoke(
        registry(),
        arguments(),
        approval=lambda _request: _approve(),
        secrets={"GITHUB_TOKEN": TOKEN},
    )

    assert result["ok"] is True
    assert result["output"] == {
        "repo": "Anshieee/kelp",
        "branch": "agent/add-readme-note",
        "commit_sha": COMMIT_SHA,
        "pull_request": {
            "number": 42,
            "url": "https://github.com/Anshieee/kelp/pull/42",
            "state": "open",
        },
    }
    assert [request.method for request in requests] == [
        "GET", "POST", "POST", "GET", "POST", "POST", "PATCH", "POST"
    ]
    assert all(request.url.host == "api.github.com" for request in requests)
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in requests)
    assert TOKEN not in json.dumps(result)


async def _approve() -> bool:
    return True


def test_invalid_arguments_are_rejected_before_approval() -> None:
    called = False

    async def approval(_request: Any) -> bool:
        nonlocal called
        called = True
        return True

    args = arguments()
    args["unexpected"] = "value"
    result = invoke(registry(), args, approval=approval, secrets={"GITHUB_TOKEN": TOKEN})

    assert result["error"]["code"] == "invalid_arguments"
    assert called is False


def test_missing_secret_is_typed_after_approval() -> None:
    result = invoke(registry(), arguments(), approval=lambda _request: _approve(), secrets={})

    assert result["error"]["code"] == "missing_secret"
    assert "GITHUB_TOKEN" in result["error"]["message"]
