"""Regression: Settings labels showed raw keys such as "settings.personal.general.title" on reload.

Issue open-webui/open-webui#30348, fix fecbeac7d (PR open-webui/open-webui#30354). On the first
visit the language detector stores the language the browser reports, and on every later load
that stored language became the only fallback. A language without a bundle of its own (the
browser reporting "en-IN") then left nothing to fall back to, and every label whose key differs
from its English text showed the key. English is now always the last fallback.

Discriminates: passes on the dev efe63bd34 build, fails on that build with fecbeac7d reverted
(the Settings tabs read "settings.personal.*.title").
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


def reload_with_stored_locale(page: Page, locale: str) -> None:
    """What the detector leaves after a first visit from a browser reporting `locale`."""
    expect(page.get_by_role("navigation", name="Chat history")).to_be_attached()
    page.evaluate("locale => localStorage.setItem('locale', locale)", locale)
    page.reload()


def open_settings(page: Page, user_menu: str = "User menu", settings: str = "Settings"):
    page.get_by_role("button", name=user_menu).first.click()
    page.get_by_role("menu").get_by_role("menuitem", name=settings).click()
    return page.get_by_role("dialog")


@pytest.mark.parametrize("locale", ["en-IN", "xx"])
def test_a_stored_language_without_a_bundle_still_shows_the_settings_labels(
    page_for, make_user, locale
):
    page = page_for(make_user())
    reload_with_stored_locale(page, locale)

    settings = open_settings(page)

    expect(settings.get_by_role("tab", name="General", exact=True)).to_be_visible()
    expect(settings.get_by_role("tab", name="Interface", exact=True)).to_be_visible()
    expect(settings.get_by_text("settings.personal.")).to_have_count(0)


@pytest.mark.parametrize("locale", ["en-US", "en-GB", "en"])
def test_a_stored_english_language_shows_the_settings_labels(page_for, make_user, locale):
    page = page_for(make_user())
    reload_with_stored_locale(page, locale)

    settings = open_settings(page)

    expect(settings.get_by_role("tab", name="General", exact=True)).to_be_visible()
    expect(settings.get_by_text("settings.personal.")).to_have_count(0)


def test_a_stored_language_with_a_bundle_keeps_its_own_labels(page_for, make_user):
    page = page_for(make_user())
    reload_with_stored_locale(page, "de-DE")

    settings = open_settings(page, "Benutzermenü", "Einstellungen")

    expect(settings.get_by_role("tab", name="Allgemein", exact=True)).to_be_visible()
    expect(settings.get_by_text("settings.personal.")).to_have_count(0)
