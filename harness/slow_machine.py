"""Slow machine for the browser suite, on by default and switched off with `OWUI_TEST_SLOW=0`.

A CI runner is slower than a laptop, so a test that races the page (a blur before the load-time
focus lands) can pass every time locally and fail there. Every Chromium page the suite opens
runs with its CPU throttled through the DevTools protocol (`DEFAULT_RATE`-fold unless
`OWUI_TEST_SLOW=<n>` asks for n-fold), and the scripted provider waits as
much longer before each reply, as it waits for a page that is that much slower.
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, BrowserContext, Page

DEFAULT_RATE = 4


def _rate_from_env() -> float:
    value = os.getenv("OWUI_TEST_SLOW", "").strip().lower()
    if value in ("0", "false", "no", "off"):
        return 0
    if value in ("", "1", "true", "yes", "on"):
        return DEFAULT_RATE
    return float(value)


RATE = _rate_from_env()


def scaled(seconds: float) -> float:
    return seconds * RATE if RATE else seconds


def _throttle(page: Page) -> None:
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": RATE})


def install() -> None:
    """Throttle every page of every browser the suite launches, popups and new tabs included."""
    if not RATE:
        return
    open_context = Browser.new_context
    open_page = BrowserContext.new_page

    def new_context(self: Browser, *args, **kwargs) -> BrowserContext:
        context = open_context(self, *args, **kwargs)
        # pages the context opens on its own, such as a link into a new tab
        context.on("page", _throttle)
        return context

    def new_page(self: BrowserContext, *args, **kwargs) -> Page:
        page = open_page(self, *args, **kwargs)
        # before the caller's first navigation, which the event above may not beat
        _throttle(page)
        return page

    Browser.new_context = new_context
    BrowserContext.new_page = new_page
