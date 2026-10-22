"""Regression: invalid JSON in the image Additional Parameters left Save spinning with no message.

open-webui issue #31381, fixed by PR #31382: in Admin Settings > Images, saving with text that is
no JSON object in the Additional Parameters of the OpenAI engine or of AUTOMATIC1111 kept the
Save button spinning and showed nothing. Save now reports "Invalid JSON format for Parameters"
and stays usable, so the parameters can be corrected and saved.

open-webui issue #32835: on a fresh install the engine is OpenAI with no key, and Save refused
every change with "OpenAI API Key is required." even with image generation off, so image editing
could not be set up on another engine. The key is now asked for only while generation is on.
The Gemini key of image editing had the same flaw: with Gemini as the edit engine and no key, the
browser refused every save even with Image Edit off. It is now required only while Image Edit is on.

Discriminates: passes on the dev a5bc78300 build; with the check for the two parameter fields
missing from the save handler the message never shows and Save stays disabled. The #32835
tests pass with the fix and fail on dev 63eed95c8 (the generation key error shows and image
editing stays off; the empty Gemini edit key blocks the save and the new base URL is not stored).
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Locator, Page, expect

from harness.image_engines import (
    GEMINI_API_KEY,
    IMAGES_CONFIG,
    save_image_settings,
    serve_automatic1111,
)

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

PARAMS_PLACEHOLDER = "Enter additional parameters in JSON format"
OPENAI_PARAMS = {"quality": "hd"}
AUTOMATIC1111_PARAMS = {"cfg_scale": 7}


def open_images_tab(page: Page) -> Locator:
    page.goto("/admin/settings/images")
    settings = page.get_by_role("dialog")
    expect(settings.get_by_role("switch", name="Image Generation")).to_be_visible(timeout=20_000)
    return settings


def save_refused_then_fixed(page: Page, settings: Locator, fixed: dict) -> None:
    params = settings.get_by_placeholder(PARAMS_PLACEHOLDER)
    save = settings.get_by_role("button", name="Save", exact=True)
    # the tab rewrites the field once its settings load, so fill only after that
    expect(params).to_have_value("{}")
    params.fill("{quality: hd")
    save.click()
    expect(
        page.get_by_text("Invalid JSON format for Parameters"),
        "#31381: no message for the invalid parameters",
    ).to_be_visible()
    expect(save, "#31381: Save stayed disabled after the error").to_be_enabled()
    params.fill(json.dumps(fixed))
    save.click()
    expect(page.get_by_text("Settings saved successfully!").first).to_be_visible()


def saved_settings(admin) -> dict:
    with admin.client() as client:
        return client.get(IMAGES_CONFIG[0]).json()


def test_invalid_openai_parameters_are_reported_and_save_works_once_fixed(
    page_for, make_user, admin, preserve
):
    preserve(IMAGES_CONFIG)
    with admin.client() as client:
        save_image_settings(
            client,
            ENABLE_IMAGE_GENERATION=True,
            IMAGE_GENERATION_ENGINE="openai",
            IMAGE_GENERATION_MODEL="gpt-image-1",
            IMAGES_OPENAI_API_KEY="sk-image-test",
            IMAGES_OPENAI_API_BASE_URL="http://127.0.0.1:9/v1",
        )
    page = page_for(make_user(role="admin"))
    settings = open_images_tab(page)

    save_refused_then_fixed(page, settings, OPENAI_PARAMS)

    assert saved_settings(admin)["IMAGES_OPENAI_API_PARAMS"] == OPENAI_PARAMS


def test_invalid_automatic1111_parameters_are_reported_and_save_works_once_fixed(
    page_for, make_user, admin, preserve, listener
):
    preserve(IMAGES_CONFIG)
    with admin.client() as client:
        save_image_settings(client, **serve_automatic1111(listener))
    page = page_for(make_user(role="admin"))
    settings = open_images_tab(page)

    save_refused_then_fixed(page, settings, AUTOMATIC1111_PARAMS)

    assert saved_settings(admin)["AUTOMATIC1111_PARAMS"] == AUTOMATIC1111_PARAMS


def test_image_edit_saves_without_a_generation_key_while_generation_is_off(
    page_for, make_user, admin, preserve
):
    preserve(IMAGES_CONFIG)
    with admin.client() as client:
        save_image_settings(
            client,
            ENABLE_IMAGE_GENERATION=False,
            IMAGE_GENERATION_ENGINE="openai",
            IMAGE_GENERATION_MODEL="gpt-image-1",
            IMAGES_OPENAI_API_KEY="",
            ENABLE_IMAGE_EDIT=False,
            IMAGE_EDIT_ENGINE="gemini",
            IMAGES_EDIT_GEMINI_API_KEY=GEMINI_API_KEY,
        )
    page = page_for(make_user(role="admin"))
    settings = open_images_tab(page)
    save = settings.get_by_role("button", name="Save", exact=True)

    image_edit = settings.get_by_role("switch", name="Image Edit")
    image_edit.click()
    expect(image_edit).to_be_checked()
    save.click()
    expect(
        page.get_by_text("Settings saved successfully!").first,
        "#32835: saving with image generation off still asked for its API key",
    ).to_be_visible()
    stored = saved_settings(admin)
    assert stored["ENABLE_IMAGE_EDIT"] is True, (
        f"image editing was not saved: {stored['ENABLE_IMAGE_EDIT']}"
    )

    image_generation = settings.get_by_role("switch", name="Image Generation")
    image_generation.click()
    expect(image_generation).to_be_checked()
    save.click()
    expect(page.get_by_text("OpenAI API Key is required.")).to_be_visible()
    stored = saved_settings(admin)
    assert stored["ENABLE_IMAGE_GENERATION"] is False, (
        "image generation was saved on without an API key"
    )


def test_image_settings_save_without_an_edit_key_while_image_edit_is_off(
    page_for, make_user, admin, preserve
):
    preserve(IMAGES_CONFIG)
    with admin.client() as client:
        save_image_settings(
            client,
            ENABLE_IMAGE_GENERATION=False,
            IMAGE_GENERATION_ENGINE="openai",
            IMAGES_OPENAI_API_KEY="sk-image-test",
            ENABLE_IMAGE_EDIT=False,
            IMAGE_EDIT_ENGINE="gemini",
            IMAGES_EDIT_GEMINI_API_KEY="",
            IMAGES_EDIT_GEMINI_API_BASE_URL="http://127.0.0.1:9/v1beta",
        )
    page = page_for(make_user(role="admin"))
    settings = open_images_tab(page)
    save = settings.get_by_role("button", name="Save", exact=True)

    # the Gemini edit fields come last on the tab
    edit_base_url = settings.get_by_placeholder("API Base URL").last
    edit_base_url.fill("http://127.0.0.1:9/v1beta/edited")
    save.click()
    expect(
        page.get_by_text("Settings saved successfully!").first,
        "#32835: saving with Image Edit off still asked for the Gemini edit key",
    ).to_be_visible()
    stored = saved_settings(admin)
    assert stored["IMAGES_EDIT_GEMINI_API_BASE_URL"] == "http://127.0.0.1:9/v1beta/edited", (
        f"the edit base URL was not saved: {stored['IMAGES_EDIT_GEMINI_API_BASE_URL']}"
    )

    image_edit = settings.get_by_role("switch", name="Image Edit")
    image_edit.click()
    expect(image_edit).to_be_checked()
    save.click()
    edit_key = settings.get_by_placeholder("API Key").last
    assert edit_key.evaluate("field => field.validity.valueMissing"), (
        "Image Edit on with Gemini and no key was not refused"
    )
    stored = saved_settings(admin)
    assert stored["ENABLE_IMAGE_EDIT"] is False, "image editing was saved on without a Gemini key"
