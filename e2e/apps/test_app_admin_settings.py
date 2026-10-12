"""Journey: an admin adds, configures, disables and uninstalls an app in Settings > Apps.

The admin enters the app's URL and API key, presses Continue, reviews the requested scopes,
goes back and presses Connect. The app then shows in the list with its handle and an Enable
switch that is on. Wrong keys and unreachable URLs show the reason in the form.

Discriminates: passes on dev 1786d6cdb; in a frontend copy, never calling Connect after the
app is created leaves its switch off and turns test_an_admin_connects_an_app_from_its_url_and_key
red.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]


def _open_apps(page: Page) -> None:
    page.goto("/admin/settings/apps")
    expect(page.get_by_text("Connected apps")).to_be_visible()


def _add_app(page: Page, url: str, api_key: str) -> None:
    page.get_by_role("button", name="Add app").click()
    page.locator("#app-url").fill(url)
    page.locator("#app-api-key").fill(api_key)
    page.get_by_role("button", name="Continue").click()


def _back_from_scopes(page: Page) -> None:
    """The scope review closes with Continue (Back before e9635596d)."""
    review = page.get_by_role("region", name="App scopes").locator("xpath=..")
    review.get_by_role("button", name=re.compile("^(Continue|Back)$")).click()


def _row(page: Page, name: str):
    return page.get_by_role("switch", name=f"Enable {name}")


def test_an_admin_connects_an_app_from_its_url_and_key(page_for, apps_admin, reference_app):
    page = page_for(apps_admin)
    _open_apps(page)
    _add_app(page, reference_app.url, reference_app.api_key)
    region = page.get_by_role("region", name="App scopes")
    expect(region).to_be_visible()
    for label in (
        "Read app mentions in joined channels",
        "Read messages and history in joined channels",
        "Send messages and edit own messages in joined channels",
        "Read joined channel details",
    ):
        expect(region.get_by_text(label).first).to_be_visible()
    _back_from_scopes(page)
    expect(page.locator("#app-name")).to_have_value("Reference App")
    expect(page.get_by_text("4 selected")).to_be_visible()
    page.get_by_role("button", name="Connect").click()
    expect(page.get_by_text("App settings updated")).to_be_visible()
    expect(_row(page, "Reference App").first).to_be_checked()


def test_a_wrong_key_shows_why_continue_failed(page_for, apps_admin, reference_app):
    page = page_for(apps_admin)
    _open_apps(page)
    _add_app(page, reference_app.url, "not-the-key")
    expect(page.get_by_role("alert")).to_contain_text("rejected the API key")
    expect(page.locator("#app-url")).to_be_visible()


def test_an_unreachable_url_shows_why_continue_failed(page_for, apps_admin):
    page = page_for(apps_admin)
    _open_apps(page)
    _add_app(page, "http://localhost:9/events", "k" * 32)
    expect(page.get_by_role("alert")).to_contain_text("HTTPS")


def test_an_admin_grants_only_some_scopes(page_for, apps_admin, reference_app):
    page = page_for(apps_admin)
    _open_apps(page)
    _add_app(page, reference_app.url, reference_app.api_key)
    region = page.get_by_role("region", name="App scopes")
    region.get_by_role("switch").nth(3).click()
    _back_from_scopes(page)
    expect(page.get_by_text("3 selected")).to_be_visible()
    page.locator("#app-handle").fill("scoped-app")
    page.get_by_role("button", name="Connect").click()
    expect(page.get_by_text("App settings updated")).to_be_visible()
    with apps_admin.client() as client:
        app = next(a for a in client.get("/api/v1/apps").json() if a["slug"] == "scoped-app")
    assert "channels:read" not in app["data"]["granted_scopes"]
    assert len(app["data"]["granted_scopes"]) == 3


def test_disabling_an_app_flips_its_switch_off(page_for, apps_admin, installed_app):
    with apps_admin.client() as client:
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"name": "Switch Target"})
    page = page_for(apps_admin)
    _open_apps(page)
    switch = _row(page, "Switch Target")
    expect(switch).to_be_checked()
    switch.click()
    expect(switch).not_to_be_checked()
    with apps_admin.client() as client:
        assert client.get(f"/api/v1/apps/{installed_app['id']}").json()["is_active"] is False


def test_turning_a_disabled_app_back_on_opens_connect(page_for, apps_admin, installed_app):
    with apps_admin.client() as client:
        client.patch(
            f"/api/v1/apps/{installed_app['id']}", json={"name": "Dormant App", "is_active": False}
        )
    page = page_for(apps_admin)
    _open_apps(page)
    _row(page, "Dormant App").click()
    expect(page.get_by_role("heading", name="Configure app")).to_be_visible()
    page.get_by_role("button", name="Connect").click()
    expect(page.get_by_text("App settings updated")).to_be_visible()
    expect(_row(page, "Dormant App")).to_be_checked()


def test_uninstalling_an_app_asks_first_and_marks_it_uninstalled(
    page_for, apps_admin, installed_app
):
    with apps_admin.client() as client:
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"name": "Leaving App"})
    page = page_for(apps_admin)
    _open_apps(page)
    page.get_by_role("button", name="Configure Leaving App").click()
    page.get_by_role("button", name="Uninstall").click()
    expect(page.get_by_text("Credentials and channel memberships will be removed.")).to_be_visible()
    page.get_by_role("button", name="Confirm").click()
    expect(_row(page, "Leaving App")).not_to_be_checked()
    with apps_admin.client() as client:
        details = client.get(f"/api/v1/apps/{installed_app['id']}").json()
    assert details["configured"] is False


def test_a_manual_install_of_a_post_only_app(page_for, apps_admin, apps_instance):
    from harness.apps import serving_reference_app

    with serving_reference_app(manifest=False, name="Manual App") as app:
        page = page_for(apps_admin)
        _open_apps(page)
        page.get_by_role("button", name="Add app").click()
        page.locator("#app-url").fill(app.url)
        page.locator("#app-api-key").fill(app.api_key)
        page.get_by_role("button", name="Enter details manually").click()
        page.locator("#app-name").fill("Manual App")
        # the handle becomes the manifest id, which the app signs into its connection proof
        page.locator("#app-handle").fill(app.manifest_id)
        page.locator("#app-public-key").fill(app.public_key)
        page.get_by_role("button", name="Review").click()
        region = page.get_by_role("region", name="App scopes")
        for switch in region.get_by_role("switch").all():
            switch.click()
        _back_from_scopes(page)
        page.get_by_role("button", name="Connect").click()
        expect(page.get_by_text("App settings updated")).to_be_visible()
        expect(_row(page, "Manual App")).to_be_checked()


def test_the_apps_tab_is_offered_while_apps_are_on(page_for, apps_admin):
    page = page_for(apps_admin)
    page.goto("/admin/settings/general")
    tab = page.locator("#settings-tabs-container").get_by_role("tab", name="Apps", exact=True)
    tab.click()
    expect(page.get_by_text("Connected apps")).to_be_visible()


def test_the_apps_tab_is_hidden_while_apps_are_off(page_for, apps_admin, apps_instance, preserve):
    preserve("admin_config", on=apps_instance)
    with apps_admin.client() as client:
        config = client.get("/api/v1/auths/admin/config").json()
        client.post("/api/v1/auths/admin/config", json={**config, "ENABLE_APPS": False})
    page = page_for(apps_admin)
    page.goto("/admin/settings/general")
    tabs = page.locator("#settings-tabs-container")
    expect(tabs.get_by_role("tab").first).to_be_visible()
    expect(tabs.get_by_role("tab", name="Apps", exact=True)).to_have_count(0)
