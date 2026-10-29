"""KEL-22: the privacy review, the egress ledger, and the exec boundary.

These tests bind the written review (``docs/PRIVACY.md``) to the code:

* every recipient the code can reach is named in the review, and every
  recipient the review names is a known id — checked in both directions;
* the retention rows the review points at are the real
  ``chat.retention.RETENTION_ROWS`` class names;
* each content path writes one content-free ``egress`` ledger row (model turn,
  skill egress, provider health probe), including denials;
* the ledger cannot be handed content: unknown recipients, free-text purposes,
  and non-integer sizes are refused;
* the exec decision holds: no command runs in this build, the card makes no
  execution promise, and the flag cannot be flipped without wiring the gate.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

import chat.api as chat_api
import chat.server as chat_server
from chat import cards
from chat.retention import RETENTION_ROWS
from kernel.config import KernelConfig
from kernel.provider import ChatRequest, TextDelta, TokenFactoryClient
from openshell.audit import AuditLog
from openshell.egress_ledger import (
    EGRESS_EVENT,
    HOST_RE,
    PURPOSE_RE,
    RECIPIENTS,
    UNRESOLVED_HOST,
    EgressLedger,
    recipient_for_host,
)
from openshell.errors import PolicyDenied
from skills.policy import EgressClient, EgressResponse
from tests.chatkit import make_service

ROOT = Path(__file__).resolve().parents[1]
PRIVACY_DOC = ROOT / "docs" / "PRIVACY.md"

#: Field names a ledger row is allowed to carry. There is deliberately no
#: "body", "query", "path", "headers", "prompt", or "content".
ALLOWED_EXTRA_KEYS = frozenset(
    {"recipient", "purpose", "request_bytes", "response_bytes", "outcome", "host",
     "method", "status"}
)

#: Packages that can open a socket or declare a network host. The completeness
#: test globs every module under them, so a new file is covered without editing
#: this list — that is the point of the glob.
EGRESS_PACKAGES = ("chat", "kernel", "skills", "openshell", "payments")

API_KEY = "test-key-not-a-real-secret"
TRANSCRIPT = "my private notes about the nebius gpu cluster"


# ---------------------------------------------------------------------------
# Document / code completeness
# ---------------------------------------------------------------------------


def _doc() -> str:
    return PRIVACY_DOC.read_text(encoding="utf-8")


def test_privacy_review_exists_and_points_at_the_retention_table() -> None:
    doc = _doc()
    assert "# Privacy review" in doc
    assert "docs/DEPLOY.md" in doc
    assert "§8.1" in doc
    assert "chat/retention.py" in doc


def test_privacy_review_names_every_recipient_in_code() -> None:
    doc = _doc()
    for recipient in RECIPIENTS:
        assert f"| `{recipient}` |" in doc, f"recipient {recipient!r} missing from PRIVACY.md"


def test_privacy_review_names_no_recipient_the_code_does_not_know() -> None:
    documented = set(re.findall(r"(?m)^\| `([a-z0-9_-]+)` \|", _doc()))
    assert documented, "the recipient table is missing"
    assert documented == set(RECIPIENTS)


def _documented_hosts() -> tuple[set[str], set[str]]:
    """Return ``(documented_hosts, recipient_ids)`` from the §2 table."""

    section = _doc().split("## 2. Every outbound recipient", 1)[1].split("### 2.1", 1)[0]
    rows = re.findall(r"(?m)^\| `([a-z0-9_-]+)` \| (.+?) \|", section)
    assert rows, "the recipient table is missing"
    documented: set[str] = set()
    for _recipient, host_cell in rows:
        documented |= set(
            re.findall(r"`([a-z0-9][a-z0-9.-]+\.[a-z]{2,})`", host_cell)
        )
    return documented, {recipient for recipient, _ in rows}


def test_privacy_review_hosts_match_the_code_both_ways() -> None:
    """The §2 host column is the same set as ``RECIPIENTS``, not a paraphrase."""

    known_hosts = {host for hosts in RECIPIENTS.values() for host in hosts}
    documented, _ids = _documented_hosts()
    assert documented == known_hosts, {
        "documented_not_in_code": sorted(documented - known_hosts),
        "code_not_documented": sorted(known_hosts - documented),
    }


def _egress_source_files() -> list[Path]:
    files: list[Path] = []
    for package in EGRESS_PACKAGES:
        files.extend(sorted((ROOT / package).rglob("*.py")))
    return files


def test_every_hardcoded_endpoint_is_a_known_recipient() -> None:
    """Glob every module that can reach the network, not a hand-kept list."""

    known_hosts = {host for hosts in RECIPIENTS.values() for host in hosts}
    files = _egress_source_files()
    assert files, "the egress source glob matched nothing; the check would be vacuous"
    # The glob must actually cover the trees the review scopes, not just one file.
    covered = {path.relative_to(ROOT).parts[0] for path in files}
    assert covered == set(EGRESS_PACKAGES), sorted(set(EGRESS_PACKAGES) - covered)

    offenders: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for host in re.findall(r"https://([a-z0-9.-]+)", text):
            if host not in known_hosts:
                offenders.append(f"{relative}: {host}")
    assert offenders == []


def test_every_declared_manifest_host_is_a_known_recipient() -> None:
    known_hosts = {host for hosts in RECIPIENTS.values() for host in hosts}
    declared: list[str] = []
    for manifest in sorted((ROOT / "skills" / "builtin").glob("*/manifest.json")):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        hosts = (data.get("permissions") or {}).get("scopes", {}).get("hosts") or []
        declared.extend(str(host) for host in hosts)
    assert declared, "no skill declares a network host; the check would be vacuous"
    assert set(declared) <= known_hosts, set(declared) - known_hosts


def test_privacy_review_retention_rows_come_from_the_real_policy() -> None:
    doc = _doc()
    for row in RETENTION_ROWS:
        assert f"**{row['class']}**" in doc, f"retention row {row['class']!r} missing"


def test_privacy_review_relative_links_resolve() -> None:
    missing: list[str] = []
    for target in re.findall(r"\]\(([^)#\s]+)\)", _doc()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if not (PRIVACY_DOC.parent / target).resolve().exists():
            missing.append(target)
    assert missing == []


def test_payments_are_not_wired_into_the_application() -> None:
    """The review says Stripe and Razorpay are not constructed by build_app."""

    source = (ROOT / "chat" / "server.py").read_text(encoding="utf-8")
    assert "HttpStripeGateway" not in source
    assert "HttpRazorpayTransport" not in source
    assert "RazorpayCheckout(" not in source


# ---------------------------------------------------------------------------
# The ledger as an account
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> tuple[AuditLog, EgressLedger]:
    audit = AuditLog(tmp_path / "audit.jsonl", secret_values=(API_KEY,))
    return audit, EgressLedger(audit)


def test_recipient_resolution_is_exact_not_suffix() -> None:
    assert recipient_for_host("api.tavily.com") == "tavily"
    assert recipient_for_host("API.Tavily.com.") == "tavily"
    assert recipient_for_host("api.tavily.com.evil.test") == "unlisted"
    assert recipient_for_host("evil-tavily.com") == "unlisted"
    assert recipient_for_host(None) == "unlisted"


def test_ledger_refuses_unknown_recipients_and_free_text(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)
    with pytest.raises(ValueError):
        ledger.record(recipient="google-analytics", purpose="x", request_bytes=1)
    with pytest.raises(ValueError):
        ledger.record(recipient="tavily", purpose="the user asked about", request_bytes=1)
    with pytest.raises(ValueError):
        ledger.record(recipient="tavily", purpose="web.search", request_bytes=-1)
    with pytest.raises(ValueError):
        ledger.record(recipient="tavily", purpose="web.search", request_bytes=True)


def test_ledger_normalises_a_valid_host(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)
    ledger.record(
        recipient="tavily", purpose="web.search", request_bytes=1, host=" API.Tavily.com. "
    )
    assert ledger.entries()[0]["extra"]["host"] == "api.tavily.com"


def test_ledger_refuses_a_host_that_carries_text(tmp_path: Path) -> None:
    """A host is a destination, not a place to smuggle content (KEL-22 B1)."""

    _audit, ledger = _ledger(tmp_path)
    hostile = f"api.tavily.com/search?q={TRANSCRIPT}"
    ledger.record(
        recipient="tavily", purpose="web.search", request_bytes=1, host=hostile
    )
    ledger.record(
        recipient="github", purpose="github.read", request_bytes=1,
        host=f"the user asked about {TRANSCRIPT}",
    )
    rows = ledger.entries()
    assert len(rows) == 2
    assert [row["extra"]["host"] for row in rows] == [UNRESOLVED_HOST] * 2
    rendered = json.dumps(rows)
    assert TRANSCRIPT not in rendered
    assert "tavily.com/search" not in rendered


def test_record_host_refuses_text_and_stays_fail_visible(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)
    ledger.record_host(
        f"https://api.tavily.com/?q={TRANSCRIPT}",
        purpose="web.search",
        request_bytes=0,
    )
    extra = ledger.entries()[0]["extra"]
    assert extra["recipient"] == "unlisted"
    assert extra["host"] == UNRESOLVED_HOST
    assert TRANSCRIPT not in json.dumps(ledger.entries())


def test_host_grammar_admits_names_and_ip_literals_only() -> None:
    assert HOST_RE.match("api.tavily.com")
    assert HOST_RE.match("api-gateway.internal")
    assert HOST_RE.match("127.0.0.1")
    assert HOST_RE.match("[::1]")
    assert not HOST_RE.match("api.tavily.com/search?q=secret")
    assert not HOST_RE.match("api.tavily.com:443")
    assert not HOST_RE.match("user@api.tavily.com")
    assert not HOST_RE.match("the user asked about the cluster")


def test_ledger_without_an_audit_log_is_a_no_op(tmp_path: Path) -> None:
    ledger = EgressLedger()
    assert ledger.record(recipient="tavily", purpose="web.search", request_bytes=3) is None
    assert ledger.entries() == []


def test_ledger_records_counts_and_nothing_else(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)
    ledger.record(
        recipient="tavily",
        purpose="web.search",
        request_bytes=42,
        response_bytes=7,
        method="POST",
        host="api.tavily.com",
        status=200,
    )
    rows = ledger.entries()
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == EGRESS_EVENT
    assert set(row["extra"]) <= ALLOWED_EXTRA_KEYS
    assert row["extra"]["recipient"] == "tavily"
    assert row["extra"]["request_bytes"] == 42
    assert row["extra"]["response_bytes"] == 7


def test_purpose_grammar_rejects_content_shaped_labels() -> None:
    assert PURPOSE_RE.match("web.search")
    assert PURPOSE_RE.match("chat.completions")
    assert PURPOSE_RE.match("provider.health")
    assert not PURPOSE_RE.match("what did the user ask")
    assert not PURPOSE_RE.match("https://api.tavily.com/search?q=secret")


# ---------------------------------------------------------------------------
# One ledger row per egress path
# ---------------------------------------------------------------------------


def _sse(*chunks: Any) -> str:
    body = "\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks)
    return body + "\n\ndata: [DONE]\n\n"


def _drain(client: TokenFactoryClient, message: str) -> list[Any]:
    request = ChatRequest(
        model="nvidia/Nemotron-3_5-Super-VL",
        messages=[{"role": "user", "content": message}],
    )

    async def run() -> list[Any]:
        async with client:
            return [event async for event in client.stream(request)]

    return asyncio.run(run())


def test_model_turn_writes_one_content_free_ledger_row(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        # The transcript really is in the request: the ledger must not copy it.
        assert TRANSCRIPT in request.content.decode("utf-8")
        return httpx.Response(
            200,
            text=_sse(
                {"choices": [{"delta": {"content": "the cluster is healthy"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ),
        )

    client = TokenFactoryClient(
        KernelConfig(api_key=API_KEY),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ledger=ledger,
    )
    events = _drain(client, TRANSCRIPT)
    assert any(isinstance(event, TextDelta) for event in events)

    rows = ledger.entries()
    assert len(rows) == 1
    extra = rows[0]["extra"]
    assert extra["recipient"] == "tokenfactory"
    assert extra["purpose"] == "chat.completions"
    assert extra["outcome"] == "completed"
    assert extra["request_bytes"] > 0
    assert extra["response_bytes"] > 0
    rendered = json.dumps(rows)
    assert TRANSCRIPT not in rendered
    assert API_KEY not in rendered
    assert "the cluster is healthy" not in rendered


def test_model_turn_with_a_provider_error_still_writes_one_row(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="{}")

    client = TokenFactoryClient(
        KernelConfig(api_key=API_KEY),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ledger=ledger,
    )
    _drain(client, TRANSCRIPT)

    rows = ledger.entries()
    assert len(rows) == 1
    assert rows[0]["extra"]["outcome"] == "provider_error"
    assert rows[0]["extra"]["response_bytes"] == 0
    assert TRANSCRIPT not in json.dumps(rows)


def test_model_turn_with_no_key_never_touches_the_ledger(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)
    client = TokenFactoryClient(
        KernelConfig(api_key=None),
        client=httpx.AsyncClient(transport=httpx.MockTransport(_unreachable)),
        ledger=ledger,
    )
    _drain(client, TRANSCRIPT)
    assert ledger.entries() == []


def test_an_overridden_base_url_is_recorded_as_unlisted(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))

    client = TokenFactoryClient(
        KernelConfig(api_key=API_KEY, base_url="https://gateway.example.test/v1"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ledger=ledger,
    )
    _drain(client, TRANSCRIPT)
    rows = ledger.entries()
    assert rows[0]["extra"]["recipient"] == "unlisted"
    assert rows[0]["extra"]["host"] == "gateway.example.test"


def _unreachable(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError("no request should be attempted")


class _FakePolicy:
    """Just enough of ``InvocationPolicy`` for the egress chokepoint."""

    def __init__(self, skill: str, hosts: tuple[str, ...]):
        self.skill = skill
        self._hosts = hosts

    def check_url(self, url: str, method: str = "GET") -> str:
        host = urlsplit(url).hostname or ""
        if host not in self._hosts:
            raise PolicyDenied(f"host {host!r} not in the declared allowlist", (f"net:{host}",))
        return host


def test_skill_egress_writes_a_recipient_row_without_content(tmp_path: Path) -> None:
    audit, ledger = _ledger(tmp_path)
    body = json.dumps({"query": TRANSCRIPT}).encode("utf-8")
    payload = b'{"results": []}'
    client = EgressClient(
        _FakePolicy("web.search", ("api.tavily.com",)),
        audit=audit,
        ledger=ledger,
        transport=lambda method, url, headers, data, timeout: EgressResponse(
            status=200, headers={}, body=payload, url=url
        ),
    )

    asyncio.run(client.post("https://api.tavily.com/search", body=body))

    rows = ledger.entries()
    assert len(rows) == 1
    extra = rows[0]["extra"]
    assert extra["recipient"] == "tavily"
    assert extra["purpose"] == "web.search"
    assert extra["request_bytes"] == len(body)
    assert extra["response_bytes"] == len(payload)
    assert extra["status"] == 200
    rendered = json.dumps(rows)
    assert TRANSCRIPT not in rendered
    assert "results" not in rendered


def test_denied_skill_egress_still_writes_a_zero_byte_row(tmp_path: Path) -> None:
    audit, ledger = _ledger(tmp_path)
    client = EgressClient(
        _FakePolicy("github.read", ("api.github.com",)),
        audit=audit,
        ledger=ledger,
        transport=lambda *args: pytest.fail("a denied host must not be called"),
    )

    with pytest.raises(PolicyDenied):
        asyncio.run(client.get("https://api.tavily.com/search"))

    rows = ledger.entries()
    assert len(rows) == 1
    assert rows[0]["extra"]["recipient"] == "tavily"
    assert rows[0]["extra"]["outcome"] == "policy_denied"
    assert rows[0]["extra"]["request_bytes"] == 0
    assert rows[0]["extra"]["response_bytes"] == 0


def test_transport_failure_writes_a_zero_response_row(tmp_path: Path) -> None:
    audit, ledger = _ledger(tmp_path)

    def boom(*args: Any) -> EgressResponse:
        raise OSError("connection reset")

    client = EgressClient(
        _FakePolicy("github.read", ("api.github.com",)),
        audit=audit,
        ledger=ledger,
        transport=boom,
    )

    with pytest.raises(OSError):
        asyncio.run(client.get("https://api.github.com/repos/Anshieee/nebius-nvidia"))

    rows = ledger.entries()
    assert len(rows) == 1
    assert rows[0]["extra"]["recipient"] == "github"
    assert rows[0]["extra"]["outcome"] == "egress_error"
    assert rows[0]["extra"]["response_bytes"] == 0


def test_provider_health_probe_writes_a_content_free_row(tmp_path: Path) -> None:
    _audit, ledger = _ledger(tmp_path)
    chat_api._record_probe_egress(
        ledger, "https://api.tokenfactory.nebius.com/v1/models", 512, "completed"
    )
    rows = ledger.entries()
    assert len(rows) == 1
    extra = rows[0]["extra"]
    assert extra["recipient"] == "tokenfactory"
    assert extra["purpose"] == "provider.health"
    assert extra["method"] == "get"
    assert extra["request_bytes"] == 0
    assert extra["response_bytes"] == 512


class _StubProbeResponse:
    def __init__(self, status: int, content: bytes = b"") -> None:
        self.status_code = status
        self.content = content


class _StubProbeClient:
    """Stands in for ``httpx.AsyncClient``; the probe builds its own client."""

    def __init__(self, response: _StubProbeResponse) -> None:
        self._response = response
        self.requested: list[str] = []

    async def __aenter__(self) -> "_StubProbeClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False

    async def get(self, url: str, headers: Any = None) -> _StubProbeResponse:
        self.requested.append(url)
        return self._response


def _stub_probe(
    monkeypatch: pytest.MonkeyPatch, status: int, content: bytes = b""
) -> _StubProbeClient:
    client = _StubProbeClient(_StubProbeResponse(status, content))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    return client


def test_provider_probe_rejection_is_provider_error_with_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 is not a completed probe (KEL-22 B2)."""

    _audit, ledger = _ledger(tmp_path)
    client = _stub_probe(monkeypatch, 401, b'{"error":"unauthorized"}')

    with pytest.raises(OSError) as excinfo:
        asyncio.run(
            chat_api._http_provider_probe(
                "https://api.tokenfactory.nebius.com/v1", API_KEY, ledger
            )
        )
    assert "401" in str(excinfo.value)
    assert client.requested == ["https://api.tokenfactory.nebius.com/v1/models"]

    rows = ledger.entries()
    assert len(rows) == 1
    extra = rows[0]["extra"]
    assert extra["recipient"] == "tokenfactory"
    assert extra["purpose"] == "provider.health"
    assert extra["outcome"] == "provider_error"
    assert extra["status"] == 401
    assert extra["response_bytes"] == len(b'{"error":"unauthorized"}')
    rendered = json.dumps(rows)
    assert "unauthorized" not in rendered
    assert API_KEY not in rendered


def test_provider_probe_success_is_completed_with_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _audit, ledger = _ledger(tmp_path)
    _stub_probe(monkeypatch, 200, b'{"data":[]}')

    asyncio.run(
        chat_api._http_provider_probe(
            "https://api.tokenfactory.nebius.com/v1", API_KEY, ledger
        )
    )

    rows = ledger.entries()
    assert len(rows) == 1
    extra = rows[0]["extra"]
    assert extra["outcome"] == "completed"
    assert extra["status"] == 200
    assert extra["response_bytes"] == len(b'{"data":[]}')


def test_egress_ledger_shares_the_audit_retention(tmp_path: Path) -> None:
    """One file, one clock: ledger rows live in the audit trail, not beside it."""

    audit, ledger = _ledger(tmp_path)
    ledger.record(recipient="tavily", purpose="web.search", request_bytes=1)
    assert audit.entries(event=EGRESS_EVENT) == ledger.entries()
    assert (tmp_path / "audit.jsonl").exists()
    assert list(tmp_path.glob("egress*.jsonl")) == []


# ---------------------------------------------------------------------------
# The exec boundary
# ---------------------------------------------------------------------------


def _exec_wiring() -> tuple[bool, bool]:
    """Return ``(constructs_gate, enables_exec)`` from ``build_app``'s module.

    Parsed, not grepped: a comment or a docstring mentioning ``ExecGate`` no
    longer counts as wiring, and a rename cannot hide a real call.
    """

    tree = ast.parse(Path(chat_server.__file__).read_text(encoding="utf-8"))
    constructs_gate = False
    enables_exec = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "ExecGate":
            constructs_gate = True
        for keyword in node.keywords:
            if (
                keyword.arg == "allow_exec"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                enables_exec = True
    return constructs_gate, enables_exec


def test_exec_promise_matches_the_wiring() -> None:
    """Flag and wiring must agree; a copy-only flip fails here."""

    constructs_gate, enables_exec = _exec_wiring()
    if cards.EXEC_AVAILABLE:
        assert constructs_gate, "EXEC_AVAILABLE is True but build_app builds no gate"
        assert enables_exec, "EXEC_AVAILABLE is True but allow_exec is never enabled"
    else:
        assert not constructs_gate, "build_app constructs ExecGate while exec is unavailable"
        assert not enables_exec, "build_app enables allow_exec while exec is unavailable"


def test_build_app_never_constructs_the_exec_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime invariant: constructing the gate is observable, not greppable.

    If ``build_app`` ever builds an ``ExecGate``, this fails at construction
    time — regardless of how the call is spelled or imported.
    """

    if cards.EXEC_AVAILABLE:  # pragma: no cover - the wiring tests cover that side
        pytest.skip("exec is declared available; wiring is covered separately")

    import openshell.exec_gate as exec_gate

    def _refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("build_app constructed ExecGate while exec is unavailable")

    monkeypatch.setattr(exec_gate.ExecGate, "__init__", _refuse)

    env = _env(tmp_path)
    app = chat_server.build_app(
        chat_server.ServerConfig.from_env(env), provider=_NoopProvider(), environ=env
    )
    try:
        tool_names = {
            spec["function"]["name"] for spec in app.service._tools  # noqa: SLF001
        }
        assert "shell.exec" not in tool_names
    finally:
        app.close()


def test_exec_is_not_reachable_from_the_application(tmp_path: Path) -> None:
    env = _env(tmp_path)
    app = chat_server.build_app(
        chat_server.ServerConfig.from_env(env), provider=_NoopProvider(), environ=env
    )
    try:
        tool_names = {
            spec["function"]["name"] for spec in app.service._tools  # noqa: SLF001
        }
        assert "shell.exec" not in tool_names
        for skill in _loaded_skills(app):
            effects = (skill.manifest.get("permissions") or {}).get("effects") or []
            assert "execute" not in effects, skill.name
    finally:
        app.close()


def test_exec_card_makes_no_execution_promise() -> None:
    card = cards.approval_card(
        approval_id="apr_exec",
        source="openshell",
        thread_id="ses_1",
        turn_id=None,
        call_id="call_1",
        tool="shell.exec",
        effects=("execute",),
        command=["echo", "hi"],
        requested_at="2026-10-30T09:00:00+00:00",
        expires_at="2026-10-30T09:10:00+00:00",
    )
    rationale = card["rationale"]
    assert cards.EXEC_UNAVAILABLE_CLAUSE in rationale
    assert "does not run commands" in rationale
    assert "run a command on this machine" in rationale
    assert "It runs once" not in rationale
    assert rationale.endswith(cards.APPROVAL_RATIONALE_SUFFIX)


def test_exec_approval_records_a_decision_and_nothing_runs(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    service, store, _provider = make_service(tmp_path, [], audit=audit)
    try:
        thread = service.create_thread()
        card = service.open_exec_approval(
            thread_id=thread["id"],
            turn_id=None,
            call_id="call_exec",
            tool="shell.exec",
            command=["echo", "hi"],
        )
        decided = service.decide(card["id"], "approve")
        assert decided["executed"] is False
        assert decided["approval"]["status"] == "approved"
        assert store.get_approval(card["id"])["status"] == "approved"
        # Nothing was spawned, so the gate never wrote its execution events.
        assert audit.entries(event="exec_started") == []
        assert audit.entries(event="exec_finished") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "KELP_DB_PATH": str(tmp_path / "chat.db"),
        "KELP_AUDIT_DIR": str(tmp_path / "audit"),
        "KELP_USAGE_DIR": str(tmp_path / "usage"),
        "KELP_ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "KELP_WORKSPACE_ROOT": str(tmp_path),
        "KELP_SKILL_ROOT": str(ROOT / "skills" / "builtin"),
        "KELP_AUTH_TOKEN": "auth-token-for-tests",
        "NEBIUS_API_KEY": API_KEY,
    }


class _NoopProvider:
    async def stream(self, request: Any):  # pragma: no cover - never called
        if False:  # pragma: no cover
            yield None
        return

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        return


def _loaded_skills(app: Any):
    registry = getattr(app.service, "_registry", None)
    if registry is None:  # pragma: no cover - the app always loads one
        return []
    return [registry.get(name) for name in registry.names()]
