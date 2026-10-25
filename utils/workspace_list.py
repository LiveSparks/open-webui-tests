"""Workspace and knowledge lists, located the way a person sees them: each item is a row named by
its title. A row holds the item's link, its details and, on hover, the item's menu and any switch.
The row was a button before; it is the presentation element around the link now.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page


def item_rows(page: Page) -> Locator:
    """Every item row in the list: the presentation rows that hold an item link."""
    main = page.get_by_role("main")
    return main.locator("div[role='presentation']").filter(has=main.get_by_role("link"))


def item_row(page: Page, name: str) -> Locator:
    """The row of the item titled `name`."""
    return page.get_by_role("main").locator("div[role='presentation']").filter(has_text=name)
