"""Finding a settings row's button the way assistive technology does: by the row's label.

Settings rows tie their label to the row's button, so the button is named after the row (Change
Password, Export Chats, Temperature) while it still shows its action (Show, Export, Default).
"""

from __future__ import annotations

from playwright.sync_api import Locator


def setting_button(scope: Locator, label: str) -> Locator:
    """The button of the settings row in `scope` whose label reads `label`."""
    return scope.get_by_role("button", name=label, exact=True)
