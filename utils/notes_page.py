"""The notes list, located the way a person sees it: each note is a row named by its title.

A row holds the note's title link, its relative time and, on hover, the Note Menu and Delete
buttons. `note_row(page, title)` is one note's row, for clicking it open or reaching its menu;
`note_rows(page)` is every row in the list's order. The former row carried the label `Open note`,
now gone from the markup.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page


def note_rows(page: Page) -> Locator:
    return page.get_by_role("main").locator("div[role='presentation']")


def note_row(page: Page, title: str) -> Locator:
    return note_rows(page).filter(has_text=title)
