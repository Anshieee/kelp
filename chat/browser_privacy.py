"""Shared credential guard and dedicated demo-profile boundary."""
from __future__ import annotations

from pathlib import PurePath
from typing import Any
from weakref import ReferenceType, WeakKeyDictionary, ref

DEMO_PROFILE_NAME = "kelp-demo-browser"
_PRIVATE_PAGES: WeakKeyDictionary[Any, ReferenceType[Any]] = WeakKeyDictionary()


def register_private_page(page: Any, session: Any) -> None:
    _PRIVATE_PAGES[page] = ref(session)


def assert_page_observable(page: Any) -> None:
    private = _PRIVATE_PAGES.get(page)
    if private is not None:
        session = private()
        if session is not None:
            session.assert_observable()
        # Released credential pages still aren't demo pages.
        raise RuntimeError("credential_browser_not_demo")


async def is_demo_browser(browser: Any) -> bool:
    """Read process metadata, never page content. Fail closed if unavailable."""
    if browser is None:
        raise RuntimeError("browser_profile_unverified")
    cdp = await browser.new_browser_cdp_session()
    try:
        arguments = (await cdp.send("Browser.getBrowserCommandLine"))["arguments"]
        profiles = [arg.split("=", 1)[1] for arg in arguments
                    if arg.startswith("--user-data-dir=")]
        return len(profiles) == 1 and PurePath(profiles[0]).name == DEMO_PROFILE_NAME
    finally:
        await cdp.detach()
