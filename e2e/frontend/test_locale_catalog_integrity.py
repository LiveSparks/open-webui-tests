"""Journey: every language the Settings picker offers has a name and a translation of its own.

The picker lists the languages of src/lib/i18n/locales/languages.json and choosing one loads
locales/<code>/translation.json. Both sides are kept by hand and by `npm run i18n:parse`, so they
drift: an entry with no catalog behind it loads nothing and leaves the interface in English, an
entry listed twice or without a title shows a doubled or blank line, and an empty English
catalog shows raw keys such as "settings.personal.general.title" in the Settings labels.

Here a fresh account opens Settings > General in English, reads the picker and picks every
language in turn, which must fetch that language's catalog; German, picked for real, turns the
interface German and stays after a reload. That every catalog directory is listed and that each
is valid JSON stays a data lint in unit/frontend/test_locale_catalog_integrity.py, since a
built page cannot see a catalog the manifest does not name.

Discriminates: passes on the ef67cc3fa build; on a build whose languages.json lists German twice
and a code with no catalog the naming test fails on the doubled entry and the loading test on
the code with nothing to load; on a build with an empty English catalog the English test fails.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

LOAD_TIMEOUT_MS = 10_000


def open_settings(page: Page, user_menu: str = "User menu", settings: str = "Settings") -> Locator:
    page.get_by_role("button", name=user_menu).first.click()
    page.get_by_role("menu").get_by_role("menuitem", name=settings).click()
    return page.get_by_role("dialog")


def language_picker(settings: Locator) -> Locator:
    # found by what it offers, since its own label changes with the language
    picker = settings.locator("select").filter(has=settings.page.locator("option[value='en-US']"))
    expect(picker).to_be_visible()
    return picker


def offered_languages(picker: Locator) -> list[tuple[str, str]]:
    return picker.locator("option").evaluate_all(
        "options => options.map(option => [option.value, option.textContent.trim()])"
    )


def is_a_script_chunk(request) -> bool:
    return request.resource_type == "script" and "/_app/immutable/" in request.url


def test_the_settings_open_in_english_without_raw_keys(page_for, make_user):
    settings = open_settings(page_for(make_user()))

    expect(settings.get_by_role("tab", name="General", exact=True)).to_be_visible()
    expect(settings.get_by_role("combobox", name="Language")).to_be_visible()
    expect(settings.get_by_text("settings.personal.")).to_have_count(0)


def test_every_offered_language_has_one_entry_with_a_name(page_for, make_user):
    offered = offered_languages(language_picker(open_settings(page_for(make_user()))))

    assert len(offered) > 20, f"the picker offers only {offered}"
    unnamed = [(code, title) for code, title in offered if not code or not title]
    assert not unnamed, f"picker entries without a code or a title: {unnamed}"
    codes = [code for code, _ in offered]
    doubled = sorted({code for code in codes if codes.count(code) > 1})
    assert not doubled, f"the picker lists these languages more than once: {doubled}"


def test_every_offered_language_loads_its_own_catalog(page_for, make_user):
    page = page_for(make_user())
    picker = language_picker(open_settings(page))
    codes = dict.fromkeys(code for code, _ in offered_languages(picker) if code != "en-US")
    # French (Canada) falls back to French (France), which would load that catalog first
    codes = sorted(codes, key=lambda code: code != "fr-FR")

    nothing_loaded = []
    for code in codes:
        try:
            with page.expect_request(is_a_script_chunk, timeout=LOAD_TIMEOUT_MS):
                picker.select_option(code)
        except PlaywrightTimeoutError:
            nothing_loaded.append(code)
        expect(page.locator("html")).to_have_attribute("lang", code)

    assert not nothing_loaded, f"choosing these languages loaded no catalog: {nothing_loaded}"


def test_choosing_german_turns_the_interface_german_and_stays(page_for, make_user):
    page = page_for(make_user())
    settings = open_settings(page)

    language_picker(settings).select_option(label="German (Deutsch)")

    expect(settings.get_by_role("tab", name="Allgemein", exact=True)).to_be_visible()
    expect(settings.get_by_role("combobox", name="Sprache")).to_have_value("de-DE")
    page.reload()
    settings = open_settings(page, "Benutzermenü", "Einstellungen")
    expect(settings.get_by_role("tab", name="Allgemein", exact=True)).to_be_visible()
    expect(page.locator("html")).to_have_attribute("lang", "de-DE")
