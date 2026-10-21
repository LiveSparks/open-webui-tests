"""Regression: cancelling an Ollama model download deleted the model it was downloading.

Issue open-webui/open-webui#31823, fix PR open-webui/open-webui#31824. The cancel handler aborted
the download and then sent a delete for the model, which removed the copy that was already on the
server when the admin was updating it by pulling again. It is the same in two places: the Manage
Ollama dialog's pull (Admin > Settings > Connections) and "Download ... from Ollama.com" in the
model selector. The cancel now only stops the download.

The Ollama stand-in already has the model and holds its pull after the first progress line, so the
admin cancels it in flight; its delete really removes the model. Each test waits for the cancel
toast, which the handler showed only after its delete, then reads what the stand-in was sent.

Discriminates: passes on dev b859124f9; in a frontend build with 6eb0a0b77 reverted, both tests
fail (the stand-in is sent a delete and no longer has the model).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness.ollama_provider import OLLAMA_CONFIG, connect_ollama, serve_ollama
from utils.chat_ui import chat_input
from utils.manage_ollama import open_manage_ollama
from utils.model_selector import SELECTOR_BUTTON, model_search
from utils.tooltips import tooltip_button

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

MODEL = "qwen3:0.6b"
CANCELLED = f"{MODEL} download has been canceled"


@pytest.fixture
def pulling(admin, preserve, listener):
    """An Ollama stand-in that has the model and keeps every pull of it in progress."""
    preserve(OLLAMA_CONFIG)
    server = serve_ollama(listener, MODEL)
    server.hold_pulls()
    with admin.client() as client:
        connect_ollama(client, listener)
    yield server
    server.release_pulls()


@pytest.fixture
def operator(make_user):
    """An admin of its own, so the browser session and its model pool are the test's alone."""
    return make_user(role="admin")


def test_cancelling_a_pull_in_the_manage_dialog_keeps_the_model(page_for, operator, pulling):
    page = page_for(operator)
    dialog = open_manage_ollama(page, pulling.listener.base_url)

    dialog.get_by_placeholder("Enter model tag (e.g. mistral:7b)").first.fill(MODEL)
    tooltip_button(dialog, "Pull Model").click()
    expect(dialog.get_by_text("50%")).to_be_visible()
    dialog.get_by_role("button", name="Cancel", exact=True).click()

    expect(page.get_by_text(CANCELLED)).to_be_visible()
    assert pulling.sent("/api/delete") == []
    assert MODEL in pulling.models


def test_cancelling_a_pull_from_the_model_selector_keeps_the_model(page_for, operator, pulling):
    page = page_for(operator)
    expect(chat_input(page)).to_be_visible()
    page.get_by_role("button", name=SELECTOR_BUTTON).click()
    model_search(page).fill(MODEL)

    page.get_by_role("option", name=f'Download "{MODEL}"').click()
    expect(page.get_by_text("50%")).to_be_visible()
    page.get_by_role("button", name=f"Cancel download of {MODEL}").click()

    expect(page.get_by_text(CANCELLED)).to_be_visible()
    assert pulling.sent("/api/delete") == []
    assert MODEL in pulling.models
