"""Browser skill contract, approval, privacy and bounded-action regression tests."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat import cards
from chat.browser_privacy import assert_page_observable
from chat.credential_browser import PrivateBrowserSession
from chat.server import _tool_specs
from openshell.policy import PolicyConfig
from skills.context import SkillContext
from skills.errors import SkillHandlerError, SkillManifestError
from skills.loader import SkillRegistry
from skills.policy import GuardedSkillContext, InvocationPolicy
from skills.builtin.browser_act import handler


@pytest.fixture
def registry():
    return SkillRegistry.load()


@pytest.fixture
def context(registry, tmp_path):
    skill = registry.get("browser.act")
    return GuardedSkillContext(SkillContext(skill.name, tmp_path),
                               InvocationPolicy.from_skill(skill, PolicyConfig(workspace_root=tmp_path)))


@pytest.fixture
def browser():
    page = MagicMock()
    page.url = handler.REPO
    page.evaluate = AsyncMock(return_value={"width": 1280, "height": 900})
    page.mouse.move = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    ctx = SimpleNamespace(pages=[page], route=AsyncMock(), route_web_socket=AsyncMock(),
                          unroute=AsyncMock(), service_workers=[])
    cdp = SimpleNamespace(send=AsyncMock(return_value={"arguments": ["--user-data-dir=/var/lib/kelp-demo-browser"]}),
                          detach=AsyncMock())
    result = SimpleNamespace(contexts=[ctx], new_browser_cdp_session=AsyncMock(return_value=cdp))
    page.context = SimpleNamespace(browser=result)
    return result


def act(browser, context, **arguments):
    return asyncio.run(handler._act(browser, arguments, context))


@pytest.mark.parametrize("url", [
    "", "http://3.107.248.82:9222", "https://127.0.0.1:9222", "http://example.com:9222",
    "http://169.254.169.254:9222", "http://0.0.0.0:9222", "http://127.0.0.1:80",
    "http://user:pass@127.0.0.1:9222", "http://127.0.0.1:9222/?token=hidden",
    "http://127.0.0.1:9222/path", "http://127.0.0.1:9222/#fragment",
    "ws://127.0.0.1:9222/devtools/browser/id", "http://[fe80::1]:9222",
    "http://127.0.0.1:65536", "http://127.0.0.1:invalid",
])
def test_endpoint_fails_closed(monkeypatch, url):
    monkeypatch.setenv("KELP_DEMO_CDP_URL", url)
    with pytest.raises(SkillHandlerError, match="private endpoint"):
        handler.configured_endpoint()


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.2", "172.17.0.1", "192.168.1.2", "[::1]", "[fd00::1]"])
def test_private_literal_endpoint(monkeypatch, host):
    monkeypatch.setenv("KELP_DEMO_CDP_URL", f"http://{host}:9222")
    assert handler.configured_endpoint() == f"http://{host}:9222"


def test_localhost_is_pinned_to_loopback(monkeypatch):
    monkeypatch.setenv("KELP_DEMO_CDP_URL", "http://localhost:9222/")
    assert handler.configured_endpoint() == "http://127.0.0.1:9222"


@pytest.mark.parametrize("url", [
    "ws://3.107.248.82:9222/devtools/browser/id", "ws://127.0.0.1:9333/devtools/browser/id",
    "ws://localhost:9222/devtools/browser/id", "wss://127.0.0.1:9222/devtools/browser/id",
    "ws://user:pass@127.0.0.1:9222/devtools/browser/id", "ws://127.0.0.1:9222/devtools/page/id",
    "ws://127.0.0.1:9222/devtools/browser/id?token=hidden",
])
def test_discovery_cannot_escape_configured_endpoint(url):
    with pytest.raises(SkillHandlerError):
        handler._discovered_endpoint(url, "http://127.0.0.1:9222")


def test_manifest_reaches_model_without_shell(registry):
    specs = {item["function"]["name"]: item["function"] for item in _tool_specs(registry)}
    assert specs["browser.act"]["parameters"] == registry.get("browser.act").manifest["input_schema"]
    assert "shell.exec" not in specs
    assert cards.EXEC_AVAILABLE is False
    skill = registry.get("browser.act")
    assert skill.idempotent and skill.requires_approval
    assert set(skill.effects) == {"read", "network"}
    assert skill.declared_secrets == ()


@pytest.mark.parametrize("arguments", [
    {"action": "type", "text": "private"}, {"action": "open"},
    {"action": "open", "url": "https://github.com/login"},
    {"action": "open", "url": "https://evil.example"},
    {"action": "move_pointer", "x": -1, "y": 10},
    {"action": "move_pointer", "x": 10},
    {"action": "read_heading", "endpoint": "http://127.0.0.1:9333"},
    {"action": "click_readme", "selector": "button"},
    {"action": "read_text", "session": "credentials"},
    {"action": "read_heading", "page": 10},
    {"action": "click_readme", "x": 1, "y": 1},
])
def test_bounded_schema_rejects_before_approval(registry, arguments):
    approval = AsyncMock(return_value=True)
    result = asyncio.run(registry.invoke("browser.act", arguments, approval=approval))
    assert result["error"]["code"] == "invalid_arguments"
    approval.assert_not_awaited()


def test_approval_required_and_denied_never_attach(registry):
    with patch.object(handler, "configured_endpoint") as endpoint:
        result = asyncio.run(registry.invoke("browser.act", {"action": "read_heading"}))
        assert result["error"]["code"] == "approval_required"
        result = asyncio.run(registry.invoke("browser.act", {"action": "read_heading"},
                                            approval=AsyncMock(return_value=False)))
        assert result["error"]["code"] == "approval_denied"
        endpoint.assert_not_called()


def test_card_names_scoped_effect():
    text = cards.approval_rationale(["read", "network"], tool="browser.act",
                                    scopes={"hosts": ["github.com", "github.githubassets.com"]})
    for expected in ("this one scoped action", "visible demo pointer", "README", "never credential sessions", "cannot type", "run shell commands"):
        assert expected in text


def test_wrong_profile_refused_before_page_read(browser, context):
    cdp = asyncio.run(browser.new_browser_cdp_session())
    cdp.send.return_value = {"arguments": ["--user-data-dir=/tmp/credentials"]}
    with pytest.raises(SkillHandlerError, match="dedicated demo profile"):
        act(browser, context, action="read_heading")
    browser.contexts[0].pages[0].evaluate.assert_not_awaited()


@pytest.mark.parametrize("action", ["open", "move_pointer", "click_readme", "read_heading", "read_text"])
def test_private_guard_precedes_all_observation_and_interaction(browser, context, action):
    page = browser.contexts[0].pages[0]
    session = PrivateBrowserSession(page)
    session._blocked = True
    with pytest.raises(RuntimeError, match="private_credential_handoff_active"):
        act(browser, context, action=action, url=handler.REPO, x=100, y=100)
    page.evaluate.assert_not_awaited()
    page.goto.assert_not_awaited()
    page.mouse.move.assert_not_awaited()
    page.locator.assert_not_called()


def test_released_credential_page_still_is_not_demo(browser):
    page = browser.contexts[0].pages[0]
    PrivateBrowserSession(page)
    with pytest.raises(RuntimeError, match="credential_browser_not_demo"):
        assert_page_observable(page)


def test_credentials_cannot_begin_in_demo_profile(browser):
    page = browser.contexts[0].pages[0]
    session = PrivateBrowserSession(page)
    with pytest.raises(RuntimeError, match="credential_handoff_forbidden_in_demo_profile"):
        asyncio.run(session.begin([]))
    page.evaluate.assert_not_awaited()


def test_heading_returns_relevant_text_only(browser, context):
    page = browser.contexts[0].pages[0]
    page.evaluate.return_value = "Kelp assistant"
    output = act(browser, context, action="read_heading")
    assert output == {"session": "demo", "page": 0, "action": "read_heading", "status": "done", "heading": "Kelp assistant"}
    assert set(output) & {"url", "html", "cookies", "screenshot"} == set()


def test_output_scrubs_configured_secrets_before_truncation(browser, context, monkeypatch):
    value = "private-fixture-value-should-not-appear"
    monkeypatch.setenv("KELP_PROVIDER_API_KEY", value)
    page = browser.contexts[0].pages[0]
    page.evaluate.return_value = f"Kelp\napi_key={value}\n" + "x" * 1180 + value
    result = act(browser, context, action="read_text")
    assert value not in json.dumps(result)
    assert "[redacted]" in result["text"]
    assert len(result["text"]) <= 1200


def test_pointer_moves_real_mouse_and_installs_visible_marker(browser, context):
    page = browser.contexts[0].pages[0]
    result = act(browser, context, action="move_pointer", x=400, y=300)
    page.mouse.move.assert_awaited_once_with(400, 300, steps=30)
    assert any("kelp-demo-pointer" in call.args[0] for call in page.evaluate.await_args_list)
    assert result["pointer"] == "visible in demo browser"


def test_pointer_cannot_escape_viewport(browser, context):
    page = browser.contexts[0].pages[0]
    with pytest.raises(SkillHandlerError, match="pointer outside viewport"):
        act(browser, context, action="move_pointer", x=3000, y=300)
    page.mouse.move.assert_not_awaited()


def test_readme_click_is_idempotent(browser, context):
    page = browser.contexts[0].pages[0]
    page.url = handler.README
    act(browser, context, action="click_readme")
    act(browser, context, action="click_readme")
    page.locator.assert_not_called()
    page.goto.assert_not_awaited()


@pytest.mark.parametrize("url", ["https://github.com/login", "https://github.com/other/repo", "https://evil.example", handler.REPO + "?token=hidden"])
def test_selected_page_scope_checked_before_dom(browser, context, url):
    page = browser.contexts[0].pages[0]
    page.url = url
    with pytest.raises(SkillHandlerError, match="outside showcase scope"):
        act(browser, context, action="read_heading")
    page.evaluate.assert_not_awaited()


def test_session_page_index_is_explicit(browser, context):
    with pytest.raises(SkillHandlerError, match="demo page not found"):
        act(browser, context, action="read_text", page=1)


def test_existing_service_worker_refused(browser, context):
    browser.contexts[0].service_workers = [object()]
    with pytest.raises(SkillHandlerError, match="service workers forbidden"):
        act(browser, context, action="read_text")


def test_selected_page_respects_narrower_deployment_policy(browser, context, registry, tmp_path):
    from openshell.errors import PolicyDenied
    context.policy = InvocationPolicy.from_skill(registry.get("browser.act"),
        PolicyConfig(workspace_root=tmp_path, allowed_hosts=("github.githubassets.com",)))
    with pytest.raises(PolicyDenied):
        act(browser, context, action="read_heading")
    browser.contexts[0].pages[0].evaluate.assert_not_awaited()


@pytest.mark.parametrize("url,method,nav,kind,allowed", [
    (handler.REPO, "GET", True, "document", True),
    (handler.README, "GET", True, "document", True),
    ("https://github.com/login", "GET", True, "document", False),
    (handler.REPO, "POST", False, "fetch", False),
    ("https://evil.example", "GET", True, "document", False),
    ("https://github.githubassets.com/assets/app.js", "GET", False, "script", True),
    ("https://github.githubassets.com/assets/app.js", "GET", True, "document", False),
    ("https://github.githubassets.com/assets/app.js", "GET", False, "fetch", False),
])
def test_request_interception_bounds_redirects_forms_assets(browser, context, url, method, nav, kind, allowed):
    route = SimpleNamespace(request=SimpleNamespace(url=url, method=method, resource_type=kind,
                            is_navigation_request=lambda: nav), continue_=AsyncMock(), abort=AsyncMock())
    async def install(pattern, callback):
        await callback(route)
    browser.contexts[0].route.side_effect = install
    browser.contexts[0].pages[0].evaluate.return_value = "Kelp"
    act(browser, context, action="read_heading")
    assert route.continue_.await_count == int(allowed)
    assert route.abort.await_count == int(not allowed)
    browser.contexts[0].unroute.assert_awaited_once()


def test_execute_effect_remains_rejected(registry, tmp_path):
    data = json.loads((Path("skills/builtin/browser_act/manifest.json")).read_text())
    data["permissions"]["effects"].append("execute")
    root = tmp_path / "browser_act"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(SkillManifestError, match="execute.*forbidden"):
        SkillRegistry.load(tmp_path)


@pytest.mark.parametrize("discovery_status", [200, 302])
def test_driver_discovery_and_exception_bodies_stay_private(registry, monkeypatch, discovery_status):
    monkeypatch.setenv("KELP_DEMO_CDP_URL", "http://127.0.0.1:9222")
    websocket = "ws://127.0.0.1:9222/devtools/browser/demo-id"
    response = SimpleNamespace(status=discovery_status,
                               json=AsyncMock(return_value={"webSocketDebuggerUrl": websocket}))
    client = SimpleNamespace(get=AsyncMock(return_value=response), dispose=AsyncMock())
    browser = object()
    api = SimpleNamespace(request=SimpleNamespace(new_context=AsyncMock(return_value=client)),
                          chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=api)
    manager.__aexit__ = AsyncMock(return_value=False)
    async_api = ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: manager
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(handler, "_act", AsyncMock(side_effect=RuntimeError("private DOM/input exception body")))
    result = asyncio.run(registry.invoke("browser.act", {"action": "read_heading"},
                                        approval=AsyncMock(return_value=True)))
    assert result["error"]["code"] == "policy_denied"
    assert "private DOM/input" not in json.dumps(result)
    client.get.assert_awaited_once_with("http://127.0.0.1:9222/json/version", timeout=5000, max_redirects=0)
    client.dispose.assert_awaited_once()
    if discovery_status == 200:
        api.chromium.connect_over_cdp.assert_awaited_once_with(websocket, timeout=5000)
    else:
        api.chromium.connect_over_cdp.assert_not_awaited()
