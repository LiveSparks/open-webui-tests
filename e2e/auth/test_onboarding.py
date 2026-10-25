"""Journey: the first visitor to a fresh install is welcomed, creates the admin and signs in again.

Before any account exists the auth page opens on a welcome screen instead of the sign-in form.
Get started leads to the admin sign-up form, which has no switch to sign in or sign up, and
nothing is created until it is submitted. Once the admin exists the welcome screen is gone: the
auth page is the plain sign-in page and the owner's credentials get them back in. The single
happy path is also pinned in e2e/imports/test_fresh_install.py.

Discriminates: passes on dev 176d31d1d; in a frontend copy, never showing the welcome screen turns
all three tests red, the admin form keeping the sign-up switch turns the form test red, and the
welcome screen staying up once an account exists turns the form and sign-in tests red.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from utils.chat_ui import chat_input

pytestmark = [
    pytest.mark.journey,
    pytest.mark.slow,
    pytest.mark.requires_browser,
    pytest.mark.requires_source,
]

PAGE_TIMEOUT_MS = 30_000
OWNER_NAME = "Owner"
OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "owner-password-123"


@pytest.fixture
def visitor(browser, fresh_install):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080}, base_url=fresh_install.base_url
    )
    yield context.new_page()
    context.close()


def _open_admin_form(page: Page) -> None:
    page.goto("/")
    page.get_by_role("button", name="Get started").click()
    expect(page.get_by_role("heading", name="Welcome to your AI home.")).to_be_hidden()
    expect(page.get_by_label("Email")).to_be_in_viewport()


def _create_admin(page: Page) -> None:
    page.get_by_label("Name").fill(OWNER_NAME)
    page.get_by_label("Email").fill(OWNER_EMAIL)
    page.get_by_label("Password", exact=True).fill(OWNER_PASSWORD)
    page.get_by_role("button", name="Create Admin Account").click()
    expect(chat_input(page)).to_be_visible(timeout=PAGE_TIMEOUT_MS)


def test_the_first_visit_shows_the_welcome_screen_before_any_form(visitor):
    visitor.goto("/")

    expect(visitor.get_by_role("heading", name="Welcome to your AI home.")).to_be_visible(
        timeout=PAGE_TIMEOUT_MS
    )
    expect(visitor.get_by_role("button", name="Get started")).to_be_visible()
    # the form is in the page but pushed below the welcome screen
    expect(visitor.get_by_label("Email")).not_to_be_in_viewport()


def test_the_admin_form_has_no_sign_in_switch_and_creates_nothing_until_submitted(
    visitor, fresh_install
):
    _open_admin_form(visitor)

    expect(visitor.get_by_text("Get started with Open WebUI")).to_be_visible()
    expect(visitor.get_by_text("does not make any external connections")).to_be_visible()
    expect(visitor.get_by_role("button", name="Create Admin Account")).to_be_visible()
    expect(visitor.get_by_role("button", name="Sign in", exact=True)).to_have_count(0)
    expect(visitor.get_by_role("button", name="Sign up")).to_have_count(0)
    visitor.reload()
    expect(visitor.get_by_role("button", name="Get started")).to_be_visible(timeout=PAGE_TIMEOUT_MS)
    with fresh_install.client() as client:
        assert client.get("/api/config").json().get("onboarding") is True


def test_the_owner_signs_back_in_on_a_plain_sign_in_page(visitor, fresh_install):
    _open_admin_form(visitor)
    _create_admin(visitor)
    # the changelog modal covers an admin's first page load
    with fresh_install.client(visitor.evaluate("localStorage.token")) as client:
        dismissed = client.post(
            "/api/v1/users/user/settings/update", json={"ui": {"showChangelog": False}}
        )
    dismissed.raise_for_status()
    visitor.reload()
    expect(chat_input(visitor)).to_be_visible(timeout=PAGE_TIMEOUT_MS)
    visitor.get_by_role("navigation", name="Chat history").get_by_label("User menu").click()
    visitor.get_by_role("menuitem", name="Sign Out").click()

    expect(visitor).to_have_url(re.compile(r"/auth"))
    expect(visitor.get_by_text("Sign in to Open WebUI")).to_be_visible(timeout=PAGE_TIMEOUT_MS)
    expect(visitor.get_by_role("heading", name="Welcome to your AI home.")).to_be_hidden()
    visitor.get_by_label("Email").fill(OWNER_EMAIL)
    visitor.get_by_label("Password", exact=True).fill(OWNER_PASSWORD)
    visitor.get_by_role("button", name="Sign in", exact=True).click()
    expect(chat_input(visitor)).to_be_visible(timeout=PAGE_TIMEOUT_MS)
    with fresh_install.client() as client:
        assert "onboarding" not in client.get("/api/config").json()
