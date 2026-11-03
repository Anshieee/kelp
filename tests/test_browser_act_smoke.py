"""Opt-in loopback CDP integration using a synthetic public showcase document.

This proves local Chromium/approval wiring, not the remote VNC/chat rehearsal.
"""
import asyncio
import json
import os
import socket
from unittest.mock import AsyncMock

import pytest

from skills.loader import SkillRegistry
from skills.builtin.browser_act.handler import REPO

pytestmark = pytest.mark.skipif(os.getenv("KELP_BROWSER_SMOKE") != "1", reason="opt-in Chromium integration")
HTML = """<html><body style="background:white;padding:30px">
<a href="/Anshieee/kelp/blob/main/README.md" onclick="event.preventDefault();history.pushState({},'',this.href)">README.md</a>
<article class="markdown-body"><h1>Kelp showcase fixture</h1><p>Public demo text.</p>
<input value="never-read-input"><pre>never-read-code</pre>
<p style="display:none">never-read-hidden</p><p data-private>never-read-private</p></article></body></html>"""


def test_loopback_cdp_approved_actions_and_visible_pointer(tmp_path, monkeypatch):
    async def scenario():
        from playwright.async_api import async_playwright
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        monkeypatch.setenv("KELP_DEMO_CDP_URL", f"http://127.0.0.1:{port}")
        async with async_playwright() as playwright:
            browser_context = await playwright.chromium.launch_persistent_context(
                tmp_path / "kelp-demo-browser", headless=True,
                args=[f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1", "--enable-automation"],
            )
            try:
                page = browser_context.pages[0]
                await page.route(REPO, lambda route: route.fulfill(body=HTML, content_type="text/html"))
                await page.goto(REPO)
                await page.unroute(REPO)
                registry = SkillRegistry.load()
                approval = AsyncMock(return_value=True)
                async def invoke(**arguments):
                    result = await registry.invoke("browser.act", arguments, approval=approval)
                    assert result["ok"], json.dumps(result)
                    return result["output"]
                await invoke(action="open", url=REPO)
                await invoke(action="move_pointer", x=450, y=300)
                pointer = page.locator("#kelp-demo-pointer")
                assert await pointer.is_visible()
                assert await pointer.evaluate("x => x.style.transform") == "translate(450px, 300px)"
                await page.screenshot(path="/tmp/opencode/kelp-demo-browser-pointer.png")
                await invoke(action="click_readme")
                assert page.url.endswith("/blob/main/README.md")
                assert (await invoke(action="read_heading"))["heading"] == "Kelp showcase fixture"
                text = (await invoke(action="read_text"))["text"]
                assert "Public demo text." in text
                assert "never-read" not in text
                assert approval.await_count == 5
                # Stopping each attachment must leave the same window alive.
                assert not page.is_closed()
            finally:
                await browser_context.close()
    asyncio.run(scenario())
