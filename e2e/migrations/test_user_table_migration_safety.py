"""Regression: accounts saved before the user-table migration lost their settings or SSO link.

Issue #26403 (fix c416c6cad): the migration `b10670c03dd5` wrote each saved `user.settings`
serialized a second time into its new JSON column on SQLite, so the account came back with a
string the app cannot read. The same migration moves the old single SSO link `user.oauth_sub`
into the `user.oauth` object, which the provider-and-sub lookup reads. The server boots on a
database built as a 0.6.x install left it (`harness.legacy_accounts`): the account that saved
Widescreen Mode on still sees it on in its Interface settings, and the account linked through
`oauth_sub` signs in with "Continue with SSO" and lands in its own account.

Twin of unit/migrations/test_user_table_migration_safety.py.
Discriminates: passes on dev ef67cc3fa; with the migration's `.values({...: parsed})` back to
`json.dumps(parsed)` in a copy of it both tests fail (neither account can sign in), and with the
`oauth_sub` conversion dropped the SSO test fails (the sign-in page stays up).
"""

from __future__ import annotations

import json
from typing import Callable, Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from harness.instance import resolve_backend, resolve_frontend_build
from harness.legacy_accounts import LEGACY_PASSWORD, legacy_email, legacy_id, seed_legacy_accounts
from harness.oidc_provider import shared_provider, sso_env
from harness.prepared_data import RunningBackend, serving
from utils.chat_ui import chat_input
from utils.user_menu import open_settings

pytestmark = [
    pytest.mark.regression,
    pytest.mark.slow,
    pytest.mark.requires_browser,
    pytest.mark.requires_source,
]


@pytest.fixture(scope="module")
def idp():
    return shared_provider()


@pytest.fixture(scope="module")
def upgraded(idp, tmp_path_factory) -> Iterator[RunningBackend]:
    backend = resolve_backend()
    build = resolve_frontend_build(backend) if backend else None
    if build is None:
        pytest.skip("no built frontend (set OPEN_WEBUI_BUILD_DIR)")
    data_dir = tmp_path_factory.mktemp("legacy-browser")
    seed_legacy_accounts(data_dir)
    settings = {**sso_env(idp), "FRONTEND_BUILD_DIR": str(build)}
    with serving(data_dir, settings) as running:
        yield running


@pytest.fixture
def new_page(browser, upgraded) -> Iterator[Callable[[], Page]]:
    """`new_page()` opens a page on the upgraded server in a browser of its own."""
    contexts = []

    def open_page() -> Page:
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080}, base_url=upgraded.base_url
        )
        contexts.append(context)
        return context.new_page()

    yield open_page
    for context in contexts:
        context.close()


@pytest.fixture
def signed_in_page(new_page, upgraded) -> Callable[[str], Page]:
    """`signed_in_page(who)` signs a legacy account in with its password and opens the app."""

    def open_page(who: str) -> Page:
        signed_in = httpx.post(
            f"{upgraded.base_url}/api/v1/auths/signin",
            json={"email": legacy_email(who), "password": LEGACY_PASSWORD},
            timeout=60.0,
        )
        assert signed_in.status_code == 200, (
            f"{who} cannot sign in after the upgrade: {signed_in.text}"
        )
        page = new_page()
        token = json.dumps(signed_in.json()["token"])
        page.add_init_script(f"try {{ localStorage.setItem('token', {token}); }} catch (e) {{}}")
        page.goto("/")
        return page

    return open_page


def test_a_setting_saved_before_the_upgrade_is_still_on(signed_in_page):
    page = signed_in_page("alice")
    open_settings(page)
    page.get_by_role("tab", name="Interface").click()

    switch = page.get_by_role("switch", name="Widescreen Mode")

    expect(switch).to_have_attribute("aria-checked", "true", timeout=30_000)


def test_an_old_sso_link_signs_its_owner_in_from_the_sign_in_page(new_page, upgraded, idp):
    idp.sign_in_as(sub="legacy-dana-sub", email=legacy_email("dana"), name="Dana")
    page = new_page()
    page.goto("/auth")

    page.get_by_role("button", name="Continue with SSO").click()

    expect(chat_input(page)).to_be_visible(timeout=30_000)
    token = page.evaluate("localStorage.getItem('token')")
    with upgraded.client(token) as client:
        assert client.get("/api/v1/auths/").json()["id"] == legacy_id("dana"), (
            "the SSO sign-in landed on another account"
        )
