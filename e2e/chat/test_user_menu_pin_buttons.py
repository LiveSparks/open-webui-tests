"""Regression: the user menu's pin buttons stayed after the window lost focus and kept a stale icon.

Holding Shift with the user menu open shows a pin button beside Workspace, Notes and the other
entries; it pins the entry to the sidebar or unpins it. Two bugs lived in that menu.

Issue open-webui/open-webui#31840, fix `7e6c8d7b6` (PR open-webui/open-webui#31841): the buttons
hid on the Shift key's release, so when Shift was let go in another window no release ever
arrived and they stayed. They now hide when the window loses focus.

Issue open-webui/open-webui#31842, fix `ecaa1a1bb` (PR open-webui/open-webui#31843): clicking a
pin button saved the pin but left its icon and tooltip as they were, because the template read
the pinned state through a function that Svelte did not re-run. Pin to Sidebar now becomes Unpin
from Sidebar right away, and back, with the menu still open.

Discriminates: passes on the dev b859124f9 build; fails on that build with 7e6c8d7b6 reverted
(the pin buttons are still shown after the window blurs) and with ecaa1a1bb reverted (the button
keeps its icon and tooltip after a click).
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Locator, Page, expect

from utils.user_menu import settings_item

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

PIN = "Pin to Sidebar"
UNPIN = "Unpin from Sidebar"
BLUR_THE_WINDOW = "() => window.dispatchEvent(new Event('blur'))"
TOOLTIP_OF = "(button) => button.parentElement._tippy?.props.content"


@pytest.fixture
def page(page_for, make_user) -> Page:
    page = page_for(make_user(role="admin"))
    page.get_by_role("button", name="User menu").first.click()
    expect(settings_item(page)).to_be_visible()
    return page


def _notes_row(page: Page) -> Locator:
    return page.get_by_role("menu").locator(".user-menu-row").filter(has_text="Notes")


def _pin_buttons(page: Page) -> Locator:
    # the row's main entry is a menuitem link; the pin toggle is the row's only button element
    return page.get_by_role("menu").locator(".user-menu-row").locator("button")


def _tooltip_becomes(button: Locator, expected: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    shown = None
    while time.monotonic() < deadline:
        shown = button.evaluate(TOOLTIP_OF)
        if shown == expected:
            return
        time.sleep(0.1)
    raise AssertionError(f"the pin button's tooltip stayed {shown!r}, not {expected!r}")


def test_pin_buttons_show_only_while_shift_is_held(page):
    expect(_pin_buttons(page)).to_have_count(0)

    page.keyboard.down("Shift")
    expect(_pin_buttons(page).first).to_be_visible()
    page.keyboard.up("Shift")

    expect(_pin_buttons(page)).to_have_count(0)


def test_pin_buttons_hide_when_the_window_loses_focus_while_shift_is_held(page):
    page.keyboard.down("Shift")
    expect(_pin_buttons(page).first).to_be_visible()

    page.evaluate(BLUR_THE_WINDOW)

    expect(_pin_buttons(page)).to_have_count(0)
    expect(page.get_by_role("menu")).to_be_visible()


def test_a_clicked_pin_button_switches_its_icon_and_tooltip_at_once(page):
    page.keyboard.down("Shift")
    button = _notes_row(page).locator("button")
    expect(button).to_be_visible()
    first_tooltip = button.evaluate(TOOLTIP_OF)
    first_icon = button.inner_html()
    assert first_tooltip in (PIN, UNPIN), first_tooltip
    other_tooltip = UNPIN if first_tooltip == PIN else PIN

    button.click()

    _tooltip_becomes(button, other_tooltip)
    assert button.inner_html() != first_icon
    expect(page.get_by_role("menu")).to_be_visible()

    button.click()

    _tooltip_becomes(button, first_tooltip)
    assert button.inner_html() == first_icon
    expect(page.get_by_role("menu")).to_be_visible()
