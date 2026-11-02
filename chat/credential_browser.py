"""Private browser bridge and optional CLI worker, with no model-facing secrets.

Attach to an existing local CDP browser; this module does not provision VMs.
All agent observation for this page must go through this session's guard.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .credentials import fields_spec, origin

# Read only metadata, never input values. Refuse frames, hidden/disabled fields,
# GET submissions, cross-site form actions, and ambiguous selectors.
_INSPECT = """({fields, submit}) => {
  const find = selector => {
    const xs = document.querySelectorAll(selector);
    if (xs.length !== 1) throw Error('ambiguous');
    const x = xs[0], style = getComputedStyle(x);
    if (!x.getClientRects().length || style.visibility !== 'visible' ||
        x.disabled || x.readOnly) throw Error('unavailable');
    return x;
  };
  const inputs = fields.map(f => find(f.selector));
  const form = inputs[0].form;
  if (!form || !inputs.every(x => x.form === form)) throw Error('form');
  const button = submit ? find(submit) : null;
  if (button && (button.form !== form || button.type !== 'submit')) throw Error('submit');
  const action = new URL(button?.getAttribute('formaction') || form.action, location.href);
  const method = (button?.getAttribute('formmethod') || form.method).toLowerCase();
  if (action.origin !== location.origin || action.protocol !== 'https:' || method !== 'post')
    throw Error('destination');
  const inputsMeta = inputs.map((x, i) => {
    if (!['INPUT', 'TEXTAREA'].includes(x.tagName)) throw Error('field');
    if (fields[i].kind === 'password' && x.type !== 'password') throw Error('password');
    if (fields[i].kind !== 'password' && !['text','password','email','tel','number','textarea'].includes(x.type))
      throw Error('type');
    return {selector: fields[i].selector, type: x.type, name: x.name, autocomplete: x.autocomplete};
  });
  return {url: location.href, origin: location.origin, document: performance.timeOrigin,
          action: action.href, method, fields: inputsMeta, submit,
          submit_label: button ? (button.innerText || button.value || button.getAttribute('aria-label') || 'Submit').slice(0,80) : ''};
}"""


class PrivateBrowserSession:
    """Guarded page. Model adapters must call assert_observable before reads.

    This is a cooperative Python guard, not isolation against a malicious worker
    or a caller retaining the raw Playwright page. Frontends may show the private
    remote browser to the owner; the agent stays paused until explicit release.
    """

    def __init__(self, page: Any):
        self._page = page
        self.session_id = 'browser_' + uuid.uuid4().hex
        self._blocked = False
        self._fields: list[dict[str, str]] = []
        self._submit: str | None = None
        self._binding = ''
        self._origin = ''

    def assert_observable(self) -> None:
        if self._blocked:
            raise RuntimeError('private_credential_handoff_active')

    async def _inspect(self) -> dict[str, Any]:
        meta = await self._page.evaluate(_INSPECT, {'fields': self._fields, 'submit': self._submit})
        site = origin(meta['origin'])
        digest = hashlib.sha256(json.dumps(meta, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'origin': site, 'binding': digest}

    async def begin(self, fields: list[dict[str, str]], submit: str | None = None) -> dict[str, Any]:
        self.assert_observable()
        self._blocked = True
        self._fields = fields_spec(fields)
        if submit is not None and (not isinstance(submit, str) or not 1 <= len(submit) <= 256):
            raise ValueError('invalid_submit_selector')
        self._submit = submit
        meta = await self._inspect()
        self._binding, self._origin = meta['binding'], meta['origin']
        label = ''
        if submit:
            label = str(await self._page.locator(submit).evaluate("x => (x.innerText || x.value || x.getAttribute('aria-label') || 'Submit').slice(0,80)"))
        return {'session_id': self.session_id, **meta, 'fields': self._fields,
                'submission': {'enabled': submit is not None, 'label': label}}

    async def current_binding(self) -> dict[str, str]:
        meta = await self._inspect()
        if meta['binding'] != self._binding or meta['origin'] != self._origin:
            raise RuntimeError('credential_form_changed')
        return {'session_id': self.session_id, **meta}

    async def fill(self, values: dict[str, str]) -> str:
        if not self._blocked or set(values) != {f['id'] for f in self._fields}:
            raise RuntimeError('invalid_credential_delivery')
        try:
            for f in self._fields:
                await self.current_binding()
                await self._page.locator(f['selector']).fill(values[f['id']])
            await self.current_binding()
            if self._submit:
                await self._page.locator(self._submit).click()
                return 'submitted'
            return 'filled'
        finally:
            # Drop references, including on failure. Python cannot guarantee
            # zeroization of immutable strings or the browser's process memory.
            values.clear()

    async def screenshot(self) -> bytes:
        self.assert_observable()
        return bytes(await self._page.screenshot())

    async def _release(self) -> None:
        # Never let an agent resume on a still-filled credential field.
        # A fresh document after navigation is safe to resume only with owner
        # consent. For a same-page handoff, clear all tracked inputs first.
        if not self._blocked:
            return
        current_site = origin(str(await self._page.evaluate('location.origin')))
        if current_site == self._origin:
            for f in self._fields:
                locator = self._page.locator(f['selector'])
                if await locator.count() == 1:
                    await locator.fill('')
        self._fields = []
        self._blocked = False


class CredentialWorker:
    def __init__(self, server: str, token: str, *, client: httpx.AsyncClient | None = None):
        p = urlsplit(server)
        if (not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('', '/')
                or (p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in {'127.0.0.1', 'localhost', '::1'}))):
            raise ValueError('worker_requires_https_or_loopback')
        if len(token) < 32:
            raise ValueError('worker_token_required')
        self._base = server.rstrip('/') + '/api/credential-worker'
        self._token = token
        self._owned = client is None
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=15)

    async def call(self, method: str, path: str = '', data: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self._client.request(method, self._base + path,
                                              headers={'Authorization': 'Bearer ' + self._token}, json=data)
        if response.status_code not in (200, 201):
            raise RuntimeError('credential_service_rejected')
        return response.json()

    async def request(self, session: PrivateBrowserSession, fields: list[dict[str, str]],
                      submit: str | None = None) -> dict[str, Any]:
        result = await self.call('POST', data=await session.begin(fields, submit))
        self._token = result['worker_token']
        return result['request']

    async def deliver(self, session: PrivateBrowserSession, request_id: str) -> dict[str, Any]:
        # Validate the *current* browser before asking for the one-use payload.
        binding = await session.current_binding()
        delivery = await self.call('POST', f'/{request_id}/claim', binding)
        try:
            outcome = await session.fill(delivery['values'])
        except Exception:
            await self.call('POST', f'/{request_id}/finish', {'outcome': 'failed'})
            raise RuntimeError('credential_fill_failed') from None
        finally:
            delivery.clear()
        return (await self.call('POST', f'/{request_id}/finish', {'outcome': outcome}))['request']

    async def resume(self, session: PrivateBrowserSession, request_id: str) -> None:
        record = (await self.call('GET', '/' + request_id))['request']
        if record['status'] != 'released' or record['session_id'] != session.session_id:
            raise RuntimeError('owner_release_required')
        await session._release()

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()


async def _run(args: argparse.Namespace) -> int:
    # Optional dependency; importing the server never requires a browser install.
    from playwright.async_api import async_playwright
    target = urlsplit(args.cdp)
    if target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost', '::1'} or target.username or target.password:
        raise ValueError('cdp_must_be_local')
    fields = fields_spec(json.loads(Path(args.fields).read_text()))
    worker = CredentialWorker(args.server, os.environ.get('KELP_BROWSER_WORKER_TOKEN', ''))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(args.cdp)
            pages = [page for context in browser.contexts for page in context.pages]
            if args.page < 0 or args.page >= len(pages):
                raise ValueError('page_index_out_of_range')
            session = PrivateBrowserSession(pages[args.page])
            record = await worker.request(session, fields, args.submit_selector)
            print(json.dumps({'id': record['id'], 'origin': record['origin'], 'status': 'pending'}), flush=True)
            # No screenshots, DOM snapshots, trace recordings, credentials or
            # exception bodies are emitted by this dedicated worker process.
            while True:
                await asyncio.sleep(1)
                record = (await worker.call('GET', '/' + record['id']))['request']
                if record['status'] == 'submitted':
                    record = await worker.deliver(session, record['id'])
                    print(json.dumps({'id': record['id'], 'status': record['status'], 'outcome': record['outcome']}), flush=True)
                if record['status'] == 'released':
                    await worker.resume(session, record['id'])
                    return 0
                # Terminal outcomes remain private until owner release too.
    finally:
        await worker.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='Private credential worker for an existing cloud browser')
    parser.add_argument('--server', required=True)
    parser.add_argument('--cdp', required=True)
    parser.add_argument('--page', type=int, required=True, help='Explicit page index in the attached browser')
    parser.add_argument('--fields', required=True, help='Path to a JSON field-description file; never secret values')
    parser.add_argument('--submit-selector')
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except (Exception, KeyboardInterrupt):
        print('credential_worker_stopped', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
