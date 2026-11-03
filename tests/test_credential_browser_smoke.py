"""Opt-in real Chromium checks; run after `playwright install chromium`."""
import asyncio
import os

import pytest

from chat.credential_browser import PrivateBrowserSession

pytestmark = pytest.mark.skipif(os.getenv('KELP_BROWSER_SMOKE') != '1', reason='opt-in Chromium integration')
FIELDS = [{'id': 'password', 'label': 'Password', 'kind': 'password', 'selector': '#password'}]
HTML = '<form method="post" action="/login"><input id="password" type="password"><button id="submit" type="submit">Sign in</button></form>'


async def scenario(change):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=['--enable-automation'])
        page = await browser.new_page()
        await page.route('https://login.example.test/**', lambda route: route.fulfill(body=HTML, content_type='text/html'))
        await page.goto('https://login.example.test/')
        session = PrivateBrowserSession(page)
        if change == 'cross_site':
            await page.locator('form').evaluate("f => f.action='https://evil.example.test/'")
            with pytest.raises(Exception):
                await session.begin(FIELDS)
        elif change == 'hidden':
            await page.locator('#password').evaluate("x => x.style.display='none'")
            with pytest.raises(Exception):
                await session.begin(FIELDS)
        else:
            record = await session.begin(FIELDS)
            assert record['origin'] == 'https://login.example.test'
            with pytest.raises(RuntimeError):
                await session.screenshot()
            if change == 'drift':
                await page.locator('form').evaluate("f => f.action='/changed'")
                with pytest.raises(RuntimeError, match='form_changed'):
                    await session.current_binding()
            else:
                assert await session.fill({'password': 'synthetic-secret'}) == 'filled'
                assert await page.locator('#password').input_value() == 'synthetic-secret'
                await session._release()
                assert await page.locator('#password').input_value() == ''
        await browser.close()


@pytest.mark.parametrize('change', ['normal', 'drift', 'cross_site', 'hidden'])
def test_real_browser_origin_visibility_and_form_binding(change):
    asyncio.run(scenario(change))
