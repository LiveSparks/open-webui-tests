"""A model's default Web Search was lost when comparing models, open-webui/open-webui#30310.

Fix commit `1b5a8ac5a` (PR open-webui/open-webui#30383). Changing the model selection clears the
feature toggles and then applies the model's default features, but only for a single model, so
adding a second model to the chat switched Web Search off even when both models have it as a
default feature. The same reset ran before the selection had settled, so for a single model the
toggle showed Web Search on while the request went out with it off. In compare mode a default
feature now stays on when every selected model has it, and the request matches the toggle.

Discriminates: passes on the efe63bd34 build, fails on it with `1b5a8ac5a` reverted (the toggle
and the request disagree).
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from harness import upstream as reply
from harness.python_tools import EVERYONE_READS
from harness.upstream import MOCK_MODEL_ID
from harness.web_retrieval import RETRIEVAL_CONFIG, save_web_settings, serve_search_results
from utils.chat_ui import chat_input
from utils.model_selector import model_search

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


@pytest.fixture
def web_search_on(admin, preserve, listener) -> None:
    preserve(RETRIEVAL_CONFIG)
    with admin.client() as client:
        save_web_settings(client, **serve_search_results(listener, []))


@pytest.fixture
def make_model(admin):
    """`make_model(searches_by_default)`: a preset able to search the web, named by its id."""
    created_ids: list[str] = []

    def create(searches_by_default: bool) -> str:
        model_id = f"web-{uuid.uuid4().hex[:8]}"
        meta = {
            "capabilities": {"web_search": True},
            "defaultFeatureIds": ["web_search"] if searches_by_default else [],
        }
        form = {
            "id": model_id,
            "base_model_id": MOCK_MODEL_ID,
            "name": model_id,
            "meta": meta,
            "params": {},
            "access_grants": [EVERYONE_READS],
        }
        with admin.client() as client:
            saved = client.post("/api/v1/models/create", json=form)
        assert saved.status_code == 200, saved.text
        created_ids.append(model_id)
        return model_id

    yield create
    with admin.client() as client:
        for model_id in created_ids:
            client.post("/api/v1/models/model/delete", json={"id": model_id})


def open_on(page_for, make_user, model_id: str) -> Page:
    account = make_user()
    with account.client() as client:
        saved = client.post(
            "/api/v1/users/user/settings/update", json={"ui": {"models": [model_id]}}
        )
    saved.raise_for_status()
    page = page_for(account)
    expect(chat_input(page)).to_be_visible()
    expect(page.get_by_role("button", name=f"Selected model: {model_id}")).to_be_visible()
    return page


def pick_model(page: Page, current_id: str, model_id: str, *, compare: bool = False) -> None:
    page.get_by_role("button", name=f"Selected model: {current_id}").click()
    if compare:
        page.get_by_role("button", name="Compare").click()
    model_search(page).fill(model_id)
    page.get_by_role("option", name=f"Select {model_id} model").click()
    if compare:
        page.keyboard.press("Escape")
        shown = f"Selected model: {current_id} +1"
    else:
        shown = f"Selected model: {model_id}"
    expect(page.get_by_role("button", name=shown)).to_be_visible()


def web_search_shown_on(page: Page) -> bool:
    """What the Web Search toggle in the Integrations menu shows."""
    page.get_by_role("button", name="Integrations", exact=True).last.click()
    toggle = page.get_by_role("menu").get_by_role("button", name="Web Search")
    expect(toggle).to_be_visible()
    shown_on = toggle.get_attribute("aria-pressed") == "true"
    page.keyboard.press("Escape")
    expect(toggle).to_be_hidden()
    return shown_on


def web_search_sent_on(page: Page, upstream, question: str) -> bool:
    """Send `question`; whether the chat request the page made asked for web search."""
    upstream.queue(reply.text("noted", match=reply.answering(question)))

    def is_chat_request(request) -> bool:
        return request.method == "POST" and request.url.endswith("/api/chat/completions")

    with page.expect_request(is_chat_request) as sent:
        chat_input(page).click()
        page.keyboard.type(question)
        page.keyboard.press("Enter")
    features = sent.value.post_data_json.get("features") or {}
    return features.get("web_search") is True


def test_comparing_two_models_that_search_by_default_keeps_web_search_on(
    page_for, make_user, make_model, web_search_on, upstream
):
    first, second = make_model(True), make_model(True)
    page = open_on(page_for, make_user, first)

    pick_model(page, first, second, compare=True)

    assert web_search_shown_on(page), "the toggle went off when the second model was added"
    assert web_search_sent_on(page, upstream, "compare the two")


def test_switching_to_a_model_that_searches_by_default_sends_web_search_on(
    page_for, make_user, make_model, web_search_on, upstream
):
    searching = make_model(True)
    page = open_on(page_for, make_user, MOCK_MODEL_ID)

    pick_model(page, MOCK_MODEL_ID, searching)

    assert web_search_shown_on(page)
    assert web_search_sent_on(page, upstream, "just the one"), "the toggle is on, the request not"


def test_comparing_with_a_model_that_does_not_search_by_default_leaves_it_off(
    page_for, make_user, make_model, web_search_on, upstream
):
    first, second = make_model(True), make_model(False)
    page = open_on(page_for, make_user, first)

    pick_model(page, first, second, compare=True)

    assert not web_search_shown_on(page)
    assert not web_search_sent_on(page, upstream, "only one of us searches"), (
        "the toggle is off, the request asked for web search"
    )


def test_switching_to_a_model_that_does_not_search_by_default_turns_it_off(
    page_for, make_user, make_model, web_search_on, upstream
):
    searching, plain = make_model(True), make_model(False)
    page = open_on(page_for, make_user, searching)

    pick_model(page, searching, plain)

    assert not web_search_shown_on(page)
    assert not web_search_sent_on(page, upstream, "no search please")
