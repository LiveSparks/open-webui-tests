"""The people, groups and apps picker behind "Add Access" and "Add Member".

A person is picked with their checkbox, which is named after them; their picture and name next
to it form a button that opens their profile card. A group's whole entry is a button holding an
unnamed checkbox. `pick_member(picker, name)` clicks whichever of the two the name belongs to.
"""

from __future__ import annotations

import re

from playwright.sync_api import Locator


def entry(picker: Locator, name: str | re.Pattern) -> Locator:
    person = picker.get_by_role("checkbox", name=name)
    group = picker.get_by_role("button", name=name).filter(has=picker.page.get_by_role("checkbox"))
    return person.or_(group).last


def pick_member(picker: Locator, name: str | re.Pattern) -> None:
    entry(picker, name).click()
