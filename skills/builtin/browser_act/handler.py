"""Bounded CDP adapter for a public showcase in a separate browser profile.

The Playwright driver owns the CDP transport. Its fixed private endpoint is
operator configuration, never model input. Browser requests are checked against
the invocation policy as well as the narrower document/resource allowlists.
"""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from chat.browser_privacy import assert_page_observable, is_demo_browser
from skills.errors import SkillHandlerError
from skills.policy import GuardedSkillContext

REPO = "https://github.com/Anshieee/kelp"
README = REPO + "/blob/main/README.md"
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                          ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"))
_SECRET_NAME = re.compile(r"token|key|secret|password|credential|authorization|cookie", re.I)
_SECRET_TEXT = re.compile(r"(?im)^.*(?:password|secret|token|api[_ -]?key|authorization|cookie)\s*[:=].*$|[A-Za-z0-9_+/=-]{32,}")

# A visible pointer follows real Playwright mouse events in the same VNC window.
# CDP mouse events alone don't move the desktop's hardware cursor.
_POINTER = """() => {
  if (document.getElementById('kelp-demo-pointer')) return;
  const p = document.createElement('div'); p.id = 'kelp-demo-pointer';
  p.setAttribute('aria-hidden', 'true');
  p.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;' +
    'width:28px;height:34px;background:#f97316;clip-path:polygon(0 0,100% 70%,55% 70%,40% 100%);' +
    'transform:translate(20px,20px);' +
    'transition:transform 0.12s linear;';
  document.documentElement.appendChild(p);
  addEventListener('mousemove', e => {p.style.transform = `translate(${e.clientX}px,${e.clientY}px)`;}, true);
  addEventListener('mousedown', () => {p.style.background = '#22c55e';}, true);
  addEventListener('mouseup', () => {p.style.background = '#f97316';}, true);
}"""
_TEXT = """({heading}) => {
  const article = document.querySelector('article.markdown-body, article .markdown-body, .markdown-body');
  if (!article) return '';
  const root = heading ? article.querySelector('h1,h2,h3') : article;
  if (!root) return '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const text = []; let size = 0;
  while (walker.nextNode() && size < 10000) {
    const node = walker.currentNode, parent = node.parentElement;
    if (!parent || parent.closest('input,textarea,select,button,form,script,style,pre,code,[hidden],[data-private]') ||
        !parent.getClientRects().length || getComputedStyle(parent).visibility !== 'visible') continue;
    text.push(node.textContent); size += node.textContent.length;
  }
  return text.join(' ').trim().slice(0,10000);
}"""


def _fail(code: str) -> SkillHandlerError:
    return SkillHandlerError("policy_denied", code.replace("_", " ") + ".",
                             retryable=False, user_fixable=False, details={"reason": code})


def configured_endpoint() -> str:
    """Only an explicit, literal RFC1918/loopback/ULA address. No DNS or redirects."""
    raw = os.environ.get("KELP_DEMO_CDP_URL", "")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        if host == "localhost":
            host = "127.0.0.1"
        address = ipaddress.ip_address(host or "")
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port is None or not 1024 <= parsed.port <= 65535
                or not any(address in network for network in _PRIVATE_NETWORKS)):
            raise ValueError
        host = f"[{address}]" if address.version == 6 else str(address)
        return f"http://{host}:{parsed.port}"
    except ValueError:
        raise _fail("demo_cdp_requires_configured_private_endpoint") from None


def _document_allowed(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.netloc == "github.com" and not parsed.query
            and parsed.path.rstrip("/") in ("/Anshieee/kelp", "/Anshieee/kelp/blob/main/README.md"))


def _discovered_endpoint(websocket: str, endpoint: str) -> str:
    target, configured = urlsplit(websocket), urlsplit(endpoint)
    if (target.scheme != "ws" or target.hostname != configured.hostname
            or target.port != configured.port or target.username is not None
            or target.password is not None or target.query or target.fragment
            or not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", target.path)):
        raise _fail("demo_cdp_discovery_refused")
    return websocket


def _public_text(text: str, context: GuardedSkillContext, limit: int) -> str:
    text = context.scrub(text)
    for name, value in os.environ.items():
        if _SECRET_NAME.search(name) and len(value) >= 4:
            text = text.replace(value, "[redacted]")
    return _SECRET_TEXT.sub("[redacted]", text)[:limit]


async def _guard(page: Any, *, blank: bool = False) -> None:
    assert_page_observable(page)
    if not ((blank and page.url == "about:blank") or _document_allowed(page.url)):
        raise _fail("browser_page_outside_showcase_scope")


async def _move(page: Any, x: int, y: int) -> None:
    await _guard(page)
    size = await page.evaluate("({width:innerWidth,height:innerHeight})")
    if not 0 <= x < size["width"] or not 0 <= y < size["height"]:
        raise _fail("pointer_outside_viewport")
    await _guard(page)
    await page.evaluate(_POINTER)
    await _guard(page)
    await page.mouse.move(x, y, steps=30)
    await page.wait_for_timeout(180)


async def _act(browser: Any, arguments: Mapping[str, Any], context: GuardedSkillContext) -> dict[str, Any]:
    if not await is_demo_browser(browser):
        raise _fail("dedicated_demo_profile_required")
    if len(browser.contexts) != 1:
        raise _fail("ambiguous_demo_browser_context")
    browser_context = browser.contexts[0]
    if browser_context.service_workers:
        raise _fail("demo_service_workers_forbidden")
    index = arguments.get("page", 0)
    if index >= len(browser_context.pages):
        raise _fail("demo_page_not_found")
    page = browser_context.pages[index]
    action = arguments["action"]
    await _guard(page, blank=action == "open")
    if page.url != "about:blank":
        context.policy.check_url(page.url)

    async def scoped_request(route: Any) -> None:
        request = route.request
        parsed = urlsplit(request.url)
        allowed = (request.method == "GET" and (
            _document_allowed(request.url) or
            (not request.is_navigation_request() and parsed.scheme == "https"
             and parsed.netloc == "github.githubassets.com"
             and request.resource_type in {"script", "stylesheet", "image", "font"})))
        if allowed:
            try:
                context.policy.check_url(request.url)
            except Exception:
                allowed = False
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    # Context-wide interception covers redirects, iframes and popups too.
    await browser_context.route_web_socket("**/*", lambda websocket: websocket.close())
    await browser_context.route("**/*", scoped_request)
    try:
        result: dict[str, Any] = {"session": "demo", "page": index, "action": action, "status": "done"}
        if action == "open":
            context.policy.check_url(arguments["url"])
            await _guard(page, blank=True)
            if page.url != arguments["url"]:
                await page.goto(arguments["url"], wait_until="domcontentloaded", timeout=15000)
        elif action == "move_pointer":
            await _move(page, arguments["x"], arguments["y"])
            result["pointer"] = "visible in demo browser"
        elif action == "click_readme":
            # A repeated click is a no-op once on README. No arbitrary buttons.
            if urlsplit(page.url).path != urlsplit(README).path:
                links = page.locator('a[href="/Anshieee/kelp/blob/main/README.md"]')
                link = None
                for number in range(await links.count()):
                    if await links.nth(number).is_visible():
                        link = links.nth(number)
                        break
                if link is None:
                    raise _fail("showcase_readme_link_not_found")
                await _guard(page)
                await link.scroll_into_view_if_needed(timeout=5000)
                box = await link.bounding_box()
                if box is None:
                    raise _fail("showcase_readme_link_not_visible")
                await _move(page, int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                await _guard(page)
                await link.click(timeout=10000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            result["pointer"] = "visible in demo browser"
        else:
            await _guard(page)
            heading = action == "read_heading"
            text = await page.evaluate(_TEXT, {"heading": heading})
            await _guard(page)
            if not text:
                raise _fail("showcase_readme_text_not_found")
            result["heading" if heading else "text"] = _public_text(text, context, 240 if heading else 1200)
        await _guard(page)
        context.policy.check_url(page.url)
        return result
    finally:
        await browser_context.unroute("**/*", scoped_request)


async def run(arguments: Mapping[str, Any], context: GuardedSkillContext) -> dict[str, Any]:
    endpoint = configured_endpoint()
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            # Validate discovery too: a local HTTP endpoint must not redirect
            # Playwright to a public websocket address.
            client = await playwright.request.new_context()
            try:
                response = await client.get(endpoint + "/json/version", timeout=5000, max_redirects=0)
                if response.status != 200:
                    raise _fail("demo_cdp_discovery_refused")
                websocket = _discovered_endpoint((await response.json())["webSocketDebuggerUrl"], endpoint)
            finally:
                await client.dispose()
            browser = await playwright.chromium.connect_over_cdp(websocket, timeout=5000)
            # Stop the driver on exit, never close the owner's Chromium window.
            return await _act(browser, arguments, context)
    except SkillHandlerError:
        raise
    except Exception:
        # Playwright exception bodies can include DOM text, URLs and inputs.
        raise _fail("demo_browser_action_refused") from None
