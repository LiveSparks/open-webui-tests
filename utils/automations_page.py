"""The automations list, located the way a person sees it: each automation is a row named by its
title, showing its schedule and holding the Active switch and the Automation Menu. The row's former
`Open automation` label is gone from the markup; the title is a link inside the row now.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page


def automation_row(page: Page, name: str) -> Locator:
    return page.get_by_role("main").locator("div[role='presentation']").filter(has_text=name)
