"""Finding a model in the chat's model selector by name, the way a person searches for one."""

from __future__ import annotations

import re

from playwright.sync_api import Locator, Page

SELECTOR_BUTTON = re.compile("Select a model|Selected model")


def model_search(page: Page) -> Locator:
    """The selector's search box, which announces the option the arrow keys are on."""
    return page.get_by_role("combobox", name="Search In Models")


def model_options(page: Page, name: str) -> Locator:
    """The selector's options for `name`, after searching for it; opens the selector if closed."""
    search = model_search(page)
    if not search.is_visible():
        page.get_by_role("button", name=SELECTOR_BUTTON).click()
    search.fill(name)
    return page.get_by_role("listbox", name="Available models").get_by_role(
        "option", name=f"Select {name} model"
    )


def select_model(page: Page, name: str) -> None:
    model_options(page, name).click()
