"""A folder's row in the sidebar, located the way a person sees it.

The row holds the expand toggle (its first button), the folder's name as a link that opens the
folder (a double click renames it) and, on hover, the "More" menu button (its last button).
`folder_link(scope, name)` is the name to click; `folder_row(scope, name)` is the whole row, for
hovering, dragging and its buttons. `scope` is the sidebar or the page. The link's name carries
an unread count after the folder's name when there is one, so `name` matches that start.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page


def _named(name: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(name)}( \d+\+?)?$")


def folder_link(scope: Locator | Page, name: str) -> Locator:
    return scope.get_by_role("link", name=_named(name))


def folder_row(scope: Locator | Page, name: str) -> Locator:
    page = scope if isinstance(scope, Page) else scope.page
    link = page.get_by_role("link", name=_named(name))
    return scope.locator("[id^='folder-'][id$='-button']").filter(has=link)
