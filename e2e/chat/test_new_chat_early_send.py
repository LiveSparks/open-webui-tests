"""Regression: a message sent while a freshly loaded new chat was still setting up lost its reply.

Issue open-webui/open-webui#32819. Loading the page starts a new chat whose setup waits for the
function and skill lists. A first message sent before those arrived was wiped from the screen when
the setup finished, so its reply never showed, the page stayed on the empty new chat and only the
sidebar held the finished chat. The fix stops the setup from resetting a chat that got a message
in the meantime.

The test holds the function list from a fresh page load, sends the first message, and lets the
list through while the send is still waiting for its answer, as a slow connection orders them.

Discriminates: fails on the dev ca8f451cc build (the message and its reply vanish and the page
stays on the new chat), passes with the fix d1511abce.
"""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import Page, Route, expect

from harness import upstream as reply
from utils.chat_ui import chat_input, conversation, expect_reply, send

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


def hold(page: Page, pattern: str) -> list[Route]:
    held: list[Route] = []
    page.route(pattern, lambda route: held.append(route))
    return held


def wait_until_held(page: Page, held: list[Route], what: str, timeout_ms: int = 15_000) -> None:
    for _ in range(timeout_ms // 100):
        if held:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"the page never asked for {what}")


def test_a_first_message_sent_during_new_chat_setup_keeps_its_reply(page_for, make_user, upstream):
    page = page_for(make_user())
    question = f"early question {uuid.uuid4().hex[:6]}"
    upstream.queue(reply.text("early answer", match=reply.answering(question)))

    function_lists = hold(page, "**/api/v1/functions/")
    sends = hold(page, "**/api/chat/completions")
    page.reload()
    expect(chat_input(page)).to_be_visible()
    wait_until_held(page, function_lists, "the function list")

    send(page, question)
    wait_until_held(page, sends, "the reply")
    with page.expect_response("**/api/v1/skills/**"):
        for route in function_lists:
            route.continue_()
    for route in sends:
        route.continue_()

    expect(conversation(page)).to_contain_text(question)
    expect_reply(page, "early answer")
    expect(page).to_have_url(re.compile(r"/c/"))

    # a New Chat started afterwards still opens empty
    page.get_by_role("link", name="New Chat").first.click()
    expect(page).not_to_have_url(re.compile(r"/c/"))
    expect(conversation(page).get_by_text(question)).to_have_count(0)
