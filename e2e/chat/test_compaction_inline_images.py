"""Regression: an image stored inside a chat made the context usage jump past the threshold.

Fix 95dd3321a (PR #31915, issue #31913). The chat page estimates the context usage it shows in
the Status panel itself, and counted an image kept in the chat record as a base64 data URI as
text: a few hundred kilobytes of picture read as a hundred thousand tokens, far past the
compaction threshold of a chat that is nearly empty. The encoded data is now left out of the
estimate, here as on the server. Twin of integration/chat/test_compaction_inline_images.py.

Discriminates: passes on the dev b859124f9 build, fails on a build with 95dd3321a reverted (the
panel reads "500% 100k/20k").
"""

from __future__ import annotations

import base64
import re
import uuid

import pytest
from playwright.sync_api import expect

from harness.chat_history import seed_chat
from utils.chat_ui import chat_input, expect_reply

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

CHAT_CONFIG = ("/api/v1/chats/config", "/api/v1/chats/config")
THRESHOLD = 20_000
# about 400 KB of base64, a hundred thousand tokens if counted as text
INLINE_IMAGE = "data:image/png;base64," + base64.b64encode(bytes(300_000)).decode()


@pytest.fixture
def compaction(admin, preserve):
    preserve(CHAT_CONFIG)
    with admin.client() as client:
        current = client.get(CHAT_CONFIG[0]).json()
        updated = client.post(
            CHAT_CONFIG[1],
            json={
                **current,
                "ENABLE_CONTEXT_COMPACTION": True,
                "CONTEXT_COMPACTION_TOKEN_THRESHOLD": THRESHOLD,
                "CONTEXT_COMPACTION_TOKEN_CAP": THRESHOLD,
            },
        )
    assert updated.status_code == 200, updated.text


def test_the_status_panel_does_not_count_an_inline_image_as_text(page_for, make_user, compaction):
    account = make_user()
    image = {"type": "image", "id": str(uuid.uuid4()), "url": INLINE_IMAGE}
    with account.client() as client:
        chat_id, _ = seed_chat(
            client,
            [
                {"role": "user", "content": "what is in this picture?", "files": [image]},
                {"role": "assistant", "content": "a blank square"},
            ],
        )
    page = page_for(account, path=f"/c/{chat_id}")
    expect_reply(page, "a blank square")

    chat_input(page).click()
    page.keyboard.type("/status")
    page.keyboard.press("Enter")

    usage = page.get_by_text("Context usage").locator("xpath=following-sibling::span")
    expect(usage).to_have_text(re.compile(r"^\d% \d+/20k$"))
