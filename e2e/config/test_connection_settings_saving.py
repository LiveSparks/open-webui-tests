"""Connection settings that were saved wrong: a deleted direct connection, the Add Connection form.

* open-webui/open-webui#31383, fix `0864f8b2d` (PR open-webui/open-webui#31384): deleting one
  of your own direct connections in Settings only changed the list on screen; nothing was saved
  unless you also pressed Save, so the connection came back on the next reload.
* open-webui/open-webui#30957, fix `301aa8c5b` (PR open-webui/open-webui#30958): after a
  connection was added, the Add Connection form cleared its URL, key and a few fields but kept
  the headers, the enabled switch, the connection and API types, the provider and the API
  version, so the next connection added was silently saved with the first one's.
* open-webui/open-webui#31377, fix `e924fa7b5` (PR open-webui/open-webui#31378): after the
  connection dialog refused headers that are not a JSON object, its Save button stayed disabled
  with a spinner until the page was reloaded, so the corrected headers could not be saved.

Discriminates: passes on the efe63bd34 build (the headers test on dev a5bc78300); with `0864f8b2d`
reverted the delete test fails (the connection is still saved), with `301aa8c5b` reverted both add
tests fail (the second connection carries the first one's headers, provider and API type, or its
disabled switch), with `e924fa7b5` reverted the headers test fails (Save stays disabled after the
error).
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

from harness.second_provider import OPENAI_CONFIG
from utils.personal_connections import add_connection_form, open_personal_connections
from utils.tooltips import tooltip_button

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

CONNECTIONS_CONFIG = ("/api/v1/configs/connections", "/api/v1/configs/connections")
# refused at once, so the model lists the app fetches after a save fail fast
FIRST_URL, SECOND_URL = "http://127.0.0.1:9/first", "http://127.0.0.1:9/second"


@pytest.fixture
def direct_connections_on(admin, preserve) -> None:
    preserve(CONNECTIONS_CONFIG)
    with admin.client() as client:
        current = client.get(CONNECTIONS_CONFIG[0]).json()
        enabled = {**current, "ENABLE_DIRECT_CONNECTIONS": True}
        client.post(CONNECTIONS_CONFIG[1], json=enabled).raise_for_status()


def is_settings_save(response) -> bool:
    return "/user/settings/update" in response.url


def saved_direct_connections(account) -> dict:
    with account.client() as client:
        settings = client.get("/api/v1/users/user/settings")
    settings.raise_for_status()
    return settings.json()["ui"]["directConnections"]


def saved_urls_settle_on(account, expected: list[str]) -> list[str]:
    """The saved connection URLs, once they read `expected` or after five seconds."""
    deadline = time.monotonic() + 5
    while True:
        urls = saved_direct_connections(account)["OPENAI_API_BASE_URLS"]
        if urls == expected or time.monotonic() > deadline:
            return urls
        time.sleep(0.2)


# --------------------------------------------------------------------------- delete


def test_deleting_a_direct_connection_is_saved_at_once(page_for, make_user, direct_connections_on):
    account = make_user()
    connections = {
        "OPENAI_API_BASE_URLS": [FIRST_URL, SECOND_URL],
        "OPENAI_API_KEYS": ["", ""],
        "OPENAI_API_CONFIGS": {"0": {"enable": True}, "1": {"enable": True}},
    }
    with account.client() as client:
        client.post(
            "/api/v1/users/user/settings/update", json={"ui": {"directConnections": connections}}
        ).raise_for_status()
    page = page_for(account)
    tab = open_personal_connections(page)

    tab.get_by_role("button", name="Open modal to configure connection").nth(1).click()
    editing = page.get_by_role("dialog").filter(
        has=page.get_by_role("heading", name="Edit Connection")
    )
    editing.get_by_role("button", name="Delete").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete").last.click()
    expect(tab.get_by_placeholder("API Base URL")).to_have_count(1)

    assert saved_urls_settle_on(account, [FIRST_URL]) == [FIRST_URL], "the deletion was not saved"
    page.reload()
    tab = open_personal_connections(page)
    expect(tab.get_by_placeholder("API Base URL")).to_have_count(1)
    expect(tab.get_by_placeholder("API Base URL")).to_have_value(FIRST_URL)


# --------------------------------------------------------------------------- add form


def test_the_next_direct_connection_does_not_inherit_a_switched_off_one(
    page_for, make_user, direct_connections_on
):
    account = make_user()
    page = page_for(account)
    tab = open_personal_connections(page)

    tab.get_by_role("button", name="Add Connection").click()
    form = add_connection_form(page)
    form.get_by_role("combobox", name="URL").fill(FIRST_URL)
    form.get_by_role("switch", name="Toggle whether current connection is active.").click()
    with page.expect_response(is_settings_save):
        form.get_by_role("button", name="Save").click()
    expect(form).to_be_hidden()

    tab.get_by_role("button", name="Add Connection").click()
    form = add_connection_form(page)
    form.get_by_role("combobox", name="URL").fill(SECOND_URL)
    with page.expect_response(is_settings_save):
        form.get_by_role("button", name="Save").click()

    saved = saved_direct_connections(account)
    assert saved["OPENAI_API_BASE_URLS"] == [FIRST_URL, SECOND_URL]
    assert saved["OPENAI_API_CONFIGS"]["0"]["enable"] is False
    assert saved["OPENAI_API_CONFIGS"]["1"]["enable"] is True, "the second one came in switched off"


def test_the_next_admin_connection_does_not_inherit_the_advanced_settings(
    page_for, make_user, admin, preserve
):
    preserve(OPENAI_CONFIG)
    page = page_for(make_user(role="admin"), path="/admin/settings/connections")
    settings = page.get_by_role("dialog")
    expect(settings.get_by_role("tab", selected=True)).to_be_visible()

    tooltip_button(settings, "Add Connection").click()
    form = add_connection_form(page)
    form.get_by_role("combobox", name="URL").fill(FIRST_URL)
    form.get_by_role("button", name="API Type").click()
    form.get_by_role("button", name="Advanced").click()
    form.get_by_placeholder("Enter additional headers in JSON format").fill('{"X-Team": "first"}')
    form.get_by_role("combobox", name="Provider").select_option(label="LiteLLM")
    with page.expect_response(lambda response: "/openai/config/update" in response.url):
        form.get_by_role("button", name="Save").click()
    expect(form).to_be_hidden()

    tooltip_button(settings, "Add Connection").click()
    form = add_connection_form(page)
    form.get_by_role("combobox", name="URL").fill(SECOND_URL)
    with page.expect_response(lambda response: "/openai/config/update" in response.url):
        form.get_by_role("button", name="Save").click()

    with admin.client() as client:
        config = client.get(OPENAI_CONFIG[0]).json()
    urls = config["OPENAI_API_BASE_URLS"]
    first = config["OPENAI_API_CONFIGS"][str(urls.index(FIRST_URL))]
    second = config["OPENAI_API_CONFIGS"][str(urls.index(SECOND_URL))]
    assert first.get("headers") == {"X-Team": "first"}
    assert first.get("provider") == "litellm"
    assert first.get("api_type") == "responses"
    carried = {key: second.get(key) for key in ("headers", "provider", "api_type")}
    assert carried == {"headers": None, "provider": None, "api_type": None}, (
        f"the second connection was saved with the first one's settings: {carried}"
    )


# --------------------------------------------------------------------------- invalid headers


def refuse_then_save_headers(page, form, url: str, is_save) -> None:
    """Fill the form with headers that are no JSON object, then fix them and save."""
    form.get_by_role("combobox", name="URL").fill(url)
    form.get_by_role("button", name="Advanced").click()
    headers = form.get_by_placeholder("Enter additional headers in JSON format")
    headers.fill("[1, 2]")
    save = form.get_by_role("button", name="Save")
    save.click()
    expect(page.get_by_text("Headers must be a valid JSON object")).to_be_visible()
    expect(save, "#31377: Save stayed disabled after the headers error").to_be_enabled()
    headers.fill('{"X-Team": "crew"}')
    with page.expect_response(is_save):
        save.click()
    expect(form).to_be_hidden()


def test_an_admin_connection_saves_once_its_headers_are_fixed(page_for, make_user, admin, preserve):
    preserve(OPENAI_CONFIG)
    page = page_for(make_user(role="admin"), path="/admin/settings/connections")
    settings = page.get_by_role("dialog")
    expect(settings.get_by_role("tab", selected=True)).to_be_visible()

    tooltip_button(settings, "Add Connection").click()
    refuse_then_save_headers(
        page,
        add_connection_form(page),
        FIRST_URL,
        lambda response: "/openai/config/update" in response.url,
    )

    with admin.client() as client:
        config = client.get(OPENAI_CONFIG[0]).json()
    index = config["OPENAI_API_BASE_URLS"].index(FIRST_URL)
    assert config["OPENAI_API_CONFIGS"][str(index)]["headers"] == {"X-Team": "crew"}
