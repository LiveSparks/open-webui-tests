"""Regression: Ctrl+Shift+Enter sent the typed message when "Ctrl+Enter to Send" was on.

Issue open-webui/open-webui#31468, fix 41ca360cc (PR open-webui/open-webui#31864). With Enter Key
Behavior set to Ctrl+Enter to Send, the chat input counted Ctrl+Shift+Enter as Ctrl+Enter and
submitted the message as well as running Generate Message Pair, so the model was asked for a
reply the user did not want. The chord now only adds the message and a placeholder reply, and
Ctrl+Enter still sends.

With two models chosen, the pair gets a placeholder reply from each of them, side by side, also
after a reload (feature 83dcc2dbe, PR open-webui/open-webui#32309); that test is a journey.

Discriminates: passes on the dev b859124f9 build, fails on that build with 41ca360cc reverted
(the model receives the typed message after Ctrl+Shift+Enter). The two-model test passes on the
dev 5032e834e build and fails on that build with 83dcc2dbe reverted (one placeholder reply).
"""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Page, expect

from harness import upstream as reply
from harness.python_tools import EVERYONE_READS
from harness.upstream import MOCK_MODEL_ID
from utils.chat_ui import chat_input, conversation, expect_reply, replies

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


@pytest.fixture
def ctrl_enter_page(page_for, make_user) -> Page:
    account = make_user()
    with account.client() as client:
        client.post(
            "/api/v1/users/user/settings/update", json={"ui": {"ctrlEnterToSend": True}}
        ).raise_for_status()
    page = page_for(account)
    expect(chat_input(page)).to_be_visible()
    chat_input(page).click()
    return page


def asked_about(upstream, prompt: str) -> list[dict]:
    return [request for request in upstream.chat_requests() if reply.answering(prompt)(request)]


def test_ctrl_shift_enter_adds_the_pair_without_asking_the_model(ctrl_enter_page, upstream):
    page = ctrl_enter_page
    upstream.queue(reply.text("An unwanted answer", match=reply.answering("pair me")))
    page.keyboard.type("pair me")

    page.keyboard.press("Control+Shift+Enter")

    expect(conversation(page).get_by_text("pair me")).to_have_count(1)
    expect(conversation(page).get_by_text("[RESPONSE")).to_be_visible()
    expect(chat_input(page)).not_to_contain_text("pair me")
    page.wait_for_timeout(1000)  # bounded: a request sent by the chord would have arrived by now
    assert asked_about(upstream, "pair me") == []
    expect(conversation(page).get_by_text("An unwanted answer")).to_have_count(0)


def test_ctrl_enter_still_sends_the_message(ctrl_enter_page, upstream):
    page = ctrl_enter_page
    upstream.queue(reply.text("Sent and answered", match=reply.answering("send me")))
    page.keyboard.type("send me")

    page.keyboard.press("Control+Enter")

    expect_reply(page, "Sent and answered")
    assert len(asked_about(upstream, "send me")) == 1


def test_plain_enter_does_not_send_with_the_setting_on(ctrl_enter_page, upstream):
    page = ctrl_enter_page
    page.keyboard.type("first line")

    page.keyboard.press("Enter")
    page.keyboard.type("second line")

    expect(chat_input(page)).to_contain_text("second line")
    page.wait_for_timeout(1000)  # bounded: an Enter that sent would have reached the provider
    assert asked_about(upstream, "first line") == []


@pytest.fixture
def two_models_page(page_for, make_user, admin) -> Page:
    """A fresh account whose chats start on the scripted model and a second model beside it."""
    model_id = f"second-{uuid.uuid4().hex[:8]}"
    form = {
        "id": model_id,
        "base_model_id": MOCK_MODEL_ID,
        "name": model_id,
        "meta": {},
        "params": {},
        "access_grants": [EVERYONE_READS],
    }
    with admin.client() as client:
        created = client.post("/api/v1/models/create", json=form)
        assert created.status_code == 200, created.text
        account = make_user()
        with account.client() as account_client:
            account_client.post(
                "/api/v1/users/user/settings/update",
                json={"ui": {"models": [MOCK_MODEL_ID, model_id]}},
            ).raise_for_status()
        page = page_for(account)
        expect(chat_input(page)).to_be_visible()
        chat_input(page).click()
        yield page
        client.post("/api/v1/models/model/delete", json={"id": model_id})


@pytest.mark.journey
def test_the_pair_adds_a_placeholder_reply_for_every_selected_model(two_models_page, upstream):
    page = two_models_page
    page.keyboard.type("pair us both")

    page.keyboard.press("Control+Shift+Enter")

    expect(conversation(page).get_by_text("pair us both")).to_have_count(1)
    expect(replies(page)).to_have_count(2)
    expect(conversation(page).get_by_text("[RESPONSE")).to_have_count(2)
    page.wait_for_timeout(1000)  # bounded: a request sent by the chord would have arrived by now
    assert asked_about(upstream, "pair us both") == []

    expect(page).to_have_url(re.compile(r"/c/[\w-]+$"))
    page.reload()
    expect(conversation(page).get_by_text("[RESPONSE")).to_have_count(2)
