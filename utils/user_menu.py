"""The sidebar's user menu, driven through one locator so a relabel is repaired in one place.

The trigger sits at the foot of the sidebar in both its forms: the collapsed rail on a wide screen
and the open drawer on a phone or once the sidebar is expanded. The button carries the account's
profile image, labelled `Open User Profile Menu`, which names it in every form; the collapsed
rail's button additionally labels itself `User menu`, the expanded one by the account's own name.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page


def user_menu_trigger(page: Page) -> Locator:
    """The user-menu button at the foot of the sidebar, whichever form the sidebar is in."""
    return page.get_by_role("navigation", name="Chat history").get_by_label(
        "Open User Profile Menu"
    )


def settings_item(page: Page) -> Locator:
    """The Settings entry inside the open user menu, a menuitem rather than a plain button now."""
    return page.get_by_role("menu").get_by_role("menuitem", name="Settings")


def open_settings(page: Page) -> None:
    """Open the user menu and click Settings, in the expanded sidebar or the collapsed rail."""
    user_menu_trigger(page).click()
    settings_item(page).click()
