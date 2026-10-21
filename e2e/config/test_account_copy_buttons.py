"""Copying the token or API key in the account settings also saved the account form, #30955.

Fix commit `bbfa876af` (open-webui/open-webui#30956). The two copy buttons sat inside the account
form without a button type, so a click submitted the form: any unsaved change to the name or
profile was saved with it. They are plain buttons now.

Discriminates: passes on dev efe63bd34; with bbfa876af reverted each copy click saves the edited
name; saving with the Save button passes on both.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Locator, Page, expect

from harness.actors import Actor
from utils.setting_rows import setting_button

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

ADMIN_CONFIG = "/api/v1/auths/admin/config"


@pytest.fixture
def account_owner(admin, make_user, preserve) -> Actor:
    """An admin with an API key, so the account page offers both copy buttons."""
    preserve("admin_config")
    with admin.client() as client:
        current = client.get(ADMIN_CONFIG).json()
        saved = client.post(ADMIN_CONFIG, json={**current, "ENABLE_API_KEYS": True})
        assert saved.status_code == 200, saved.text
    owner = make_user(role="admin")
    with owner.client() as client:
        generated = client.post("/api/v1/auths/api_key")
    assert generated.status_code == 200, generated.text
    return owner


def _account_settings(page: Page) -> Locator:
    page.get_by_role("navigation", name="Chat history").get_by_label("User menu").click()
    page.get_by_role("button", name="Settings").click()
    page.get_by_role("tab", name="Account").click()
    return page.get_by_role("dialog")


def _stored_name(owner: Actor) -> str:
    with owner.client() as client:
        return client.get("/api/v1/auths/").json()["name"]


@pytest.mark.parametrize("copy_button", ["Copy Token", "Copy API Key"])
def test_a_copy_button_does_not_save_an_edited_name(page_for, account_owner, copy_button):
    page = page_for(account_owner)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    settings = _account_settings(page)
    settings.get_by_role("textbox", name="Name").fill(f"Unsaved {uuid.uuid4().hex[:6]}")
    secrets = settings.locator("section").filter(has_text="API keys")
    setting_button(secrets, "Secrets").click()

    profile_saves = []
    page.on(
        "request",
        lambda request: "/auths/update/profile" in request.url and profile_saves.append(request),
    )
    settings.get_by_role("button", name=copy_button).click()
    page.wait_for_timeout(1000)

    assert profile_saves == [], f"{copy_button} saved the account form (#30955)"
    assert _stored_name(account_owner) == account_owner.name


# ---------------------------------------------------------------- nearby


def test_the_save_button_still_saves_the_edited_name(page_for, account_owner):
    page = page_for(account_owner)
    settings = _account_settings(page)
    edited = f"Saved {uuid.uuid4().hex[:6]}"
    settings.get_by_role("textbox", name="Name").fill(edited)

    with page.expect_response(lambda response: "/auths/update/profile" in response.url):
        settings.get_by_role("button", name="Save", exact=True).click()

    expect(page.get_by_text("Settings saved successfully!").first).to_be_visible()
    assert _stored_name(account_owner) == edited
