"""Journey: switching the model editor's editing language shows that language's text.

A model holding a German and a French name and description opens in the workspace editor. Picking
French in Editing language fills the name and description fields with the French text, and then
picking German fills them with the German text. The fields used to keep the French text after the
switch to German (open-webui/open-webui#32223); switching from Default to a language was never
affected.

Discriminates: red on dev 0b49167ae (both fields keep the French text after the switch to German),
green with the fields refreshed on every language change.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import expect

from harness.upstream import MOCK_MODEL_ID

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

DEFAULT_DESCRIPTION = "Answers harbour questions."
TRANSLATIONS = {
    "de-DE": {"name": "Hafen Notizen", "description": "Beantwortet Fragen zum Hafen."},
    "fr-FR": {"name": "Notes du port", "description": "Répond aux questions sur le port."},
}


@pytest.fixture
def builder(make_user):
    account = make_user(role="admin")
    yield account
    with account.client() as client:
        for model in client.get("/api/v1/models/list").json().get("items", []):
            if model["user_id"] == account.id:
                client.post("/api/v1/models/model/delete", json={"id": model["id"]})


@pytest.fixture
def translated_model(builder) -> dict:
    model = {"id": f"quay-{uuid.uuid4().hex[:8]}", "name": f"Quay notes {uuid.uuid4().hex[:6]}"}
    with builder.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                **model,
                "base_model_id": MOCK_MODEL_ID,
                "meta": {"description": DEFAULT_DESCRIPTION, "i18n": TRANSLATIONS},
                "params": {},
            },
        )
    assert created.status_code == 200, created.text
    return model


def test_switching_between_two_languages_shows_each_languages_text(
    page_for, builder, translated_model
):
    page = page_for(builder)
    page.goto(f"/workspace/models/edit?id={translated_model['id']}")
    editor = page.get_by_role("main")
    expect(editor.get_by_placeholder("Model Name")).to_have_value(translated_model["name"])
    language = page.get_by_label("Editing language")
    name = editor.get_by_placeholder(translated_model["name"])
    description = editor.get_by_placeholder(DEFAULT_DESCRIPTION)

    language.select_option("fr-FR")
    expect(name).to_have_value(TRANSLATIONS["fr-FR"]["name"])
    expect(description).to_have_value(TRANSLATIONS["fr-FR"]["description"])

    language.select_option("de-DE")
    expect(name).to_have_value(TRANSLATIONS["de-DE"]["name"])
    expect(description).to_have_value(TRANSLATIONS["de-DE"]["description"])
