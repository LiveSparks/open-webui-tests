"""The people, groups and apps picker behind "Add Access" and "Add Member".

A person's entry is a button holding a checkbox and their picture, and the picture is itself a
button named after them that opens their profile card, so a lookup by name finds both.
`pick_member(picker, name)` clicks the entry, the button that holds the checkbox.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator


def entry(picker: Locator, name: str | re.Pattern) -> Locator:
    checkbox = picker.page.get_by_role("checkbox")
    return picker.get_by_role("button", name=name).filter(has=checkbox).last


def pick_member(picker: Locator, name: str | re.Pattern) -> None:
    entry(picker, name).click()
