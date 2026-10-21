"""A chat's row in the sidebar and the menu on it, located the way a person sees them.

The row's link is named by the chat's title, followed by when the chat was last updated: the
short age shown on screen ("1m") while the row is not hovered or focused, or, since dc45e3960, the
spoken age ("a few seconds ago") at all times. `chat_row(scope, title)` matches the title at the
start of that name, whatever follows it; `scope` is the sidebar, a folder or the page.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page, expect

# the short age on screen, which the row hides once its menu trigger shows
_SHORT_AGE = re.compile(r"^\d+[mhdwy]$")


def chat_row(scope: Locator | Page, title: str) -> Locator:
    return scope.get_by_role("button", name=re.compile(rf"^{re.escape(title)}(?: |$)"))


def chat_menu(sidebar: Locator, title: str) -> Locator:
    """Open the menu on the row of `title`, whose trigger shows while the row has focus."""
    row = sidebar.locator("#sidebar-chat-group").filter(has=chat_row(sidebar.page, title))
    row.hover()
    chat_row(row, title).first.focus()
    expect(row.get_by_text(_SHORT_AGE)).to_have_count(0)
    row.get_by_role("button", name="Chat Menu").first.click()
    return sidebar.page.get_by_role("menu")
