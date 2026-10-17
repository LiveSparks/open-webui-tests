"""Regression: older unfinished replies kept a loading cursor forever after opening a chat.

Issue open-webui/open-webui#14806, fix 394295ec7 (PR open-webui/open-webui#31895). Opening a
chat only finished the newest reply that never completed, so an older one (from a cancelled
generation, or a turn an earlier version left unfinished) kept spinning and never offered its
Copy and Regenerate buttons. With no generation running, every unfinished reply now shows as
finished on load, except one waiting for the user to answer a tool's question.

Discriminates: passes on the dev b859124f9 build, fails on that build with 394295ec7 reverted
(the older unfinished reply has no Copy or Regenerate buttons).
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Locator, Page, expect

from harness.chat_history import seed_chat
from utils.chat_ui import conversation

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

OLDER = "The first answer was cut off mid"
MIDDLE = "A complete second answer."
NEWEST = "The last answer was cut off mid"

# the whole message row: its buttons sit beside the text, not inside the element holding it
MESSAGE = "div.flex.w-full[id^='message-']"

ASK_USER_CALL = {
    "type": "function_call",
    "id": "fc_plan",
    "call_id": "call_plan",
    "name": "ask_user",
    "status": "pending",
    "arguments": json.dumps(
        {
            "questions": [
                {
                    "id": "plan",
                    "header": "Plan",
                    "question": "Which plan fits you best?",
                    "options": [
                        {"label": "Basic", "description": "The cheapest one"},
                        {"label": "Pro", "description": "The fastest one"},
                    ],
                }
            ]
        }
    ),
}


def open_chat(page_for, make_user, messages: list[dict]) -> Page:
    owner = make_user()
    with owner.client() as client:
        chat_id, _ = seed_chat(client, messages)
    page = page_for(owner, path=f"/c/{chat_id}")
    return page


def reply_with(page: Page, text: str) -> Locator:
    return conversation(page).locator(MESSAGE, has_text=text)


def expect_finished(reply: Locator) -> None:
    expect(reply).to_be_visible()
    reply.hover()
    expect(reply.get_by_role("button", name="Copy")).to_have_count(1)
    expect(reply.get_by_role("button", name="Regenerate").first).to_be_attached()


def test_an_older_unfinished_reply_is_shown_as_finished(page_for, make_user):
    page = open_chat(
        page_for,
        make_user,
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": OLDER, "done": False},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": MIDDLE},
        ],
    )

    expect(reply_with(page, MIDDLE)).to_be_visible()
    expect_finished(reply_with(page, OLDER))


def test_every_unfinished_reply_in_a_longer_chat_is_shown_as_finished(page_for, make_user):
    page = open_chat(
        page_for,
        make_user,
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": OLDER, "done": False},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": MIDDLE},
            {"role": "user", "content": "third question"},
            {"role": "assistant", "content": NEWEST, "done": False},
        ],
    )

    expect_finished(reply_with(page, NEWEST))
    expect_finished(reply_with(page, MIDDLE))
    expect_finished(reply_with(page, OLDER))


def test_a_reply_waiting_for_the_users_answer_stays_pending(page_for, make_user):
    page = open_chat(
        page_for,
        make_user,
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": OLDER, "done": False},
            {"role": "user", "content": "help me choose a plan"},
            {
                "role": "assistant",
                "content": "",
                "done": False,
                "output": [ASK_USER_CALL],
            },
        ],
    )

    expect(page.get_by_text("Which plan fits you best?")).to_be_visible()
    expect_finished(reply_with(page, OLDER))
    waiting = conversation(page).locator(MESSAGE).last
    waiting.hover()
    expect(waiting.get_by_role("button", name="Regenerate")).to_have_count(0)
