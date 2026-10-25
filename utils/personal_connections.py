"""Driving Settings > Connections, where a user adds their own direct connections."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

from utils.chat_ui import chat_input
from utils.user_menu import open_settings


def open_personal_connections(page: Page) -> Locator:
    expect(chat_input(page)).to_be_visible()
    open_settings(page)
    page.get_by_role("tab", name="Connections").click()
    tab = page.locator("#tab-connections")
    expect(tab.get_by_role("button", name="Add Connection")).to_be_visible()
    return tab


def add_connection_form(page: Page) -> Locator:
    form = page.get_by_role("dialog").filter(has=page.get_by_role("heading", name="Add Connection"))
    expect(form).to_be_visible()
    return form
