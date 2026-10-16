"""Journey: Settings > Usage counts the account's own chats, and Settings > About shows the version.

A fresh account's Usage tab says there is no usage yet. After one chat it counts that chat, one
message each way and the model it went to, while a second account still has none. The About tab
shows the release, and See what's new opens the release notes with an entry for the newest
release in the changelog. Dev raises the version before its notes are written (as a6b1c0b35 did
for 0.12.0), so the running release may have no entry yet.

Discriminates: passes on dev 176d31d1d; in a frontend copy, the Usage tab treating every
account as having no usage turns the usage test red, and the About tab showing no release turns
the about test red.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from harness import slow_machine
from harness import upstream as reply
from harness.upstream import MOCK_MODEL_ID
from utils.chat_ui import expect_reply, send

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

RELEASE = re.compile(r"v\d+\.\d+\.\d+")
NO_USAGE = "No usage data found"
# a cold app load opening the dialog takes over 5s at the throttled CPU, with nothing racing it
SETTINGS_OPEN_TIMEOUT_MS = slow_machine.scaled(5_000)


def settings_tab(page: Page, tab_id: str, heading: str) -> Locator:
    page.goto(f"/?settings={tab_id}")
    settings = page.get_by_role("dialog")
    expect(settings.get_by_role("heading", name=heading, exact=True)).to_be_visible(
        timeout=SETTINGS_OPEN_TIMEOUT_MS
    )
    return settings


def usage_row(tab: Locator, label: str) -> Locator:
    return tab.get_by_text(label, exact=True).locator("xpath=..")


def test_usage_counts_the_accounts_own_chat(page_for, make_user, upstream):
    page = page_for(make_user())
    expect(settings_tab(page, "usage", "Usage").get_by_text(NO_USAGE)).to_be_visible()

    question = "How many gulls are on the rock?"
    upstream.queue(reply.text("Seven gulls.", match=reply.answering(question)))
    page.goto("/")
    send(page, question)
    expect_reply(page, "Seven gulls.")

    usage = settings_tab(page, "usage", "Usage")
    expect(usage_row(usage, "Total chats")).to_have_text(re.compile(r"^Total chats\s*1$"))
    expect(usage_row(usage, "User messages")).to_have_text(re.compile(r"^User messages\s*1 ·"))
    expect(usage_row(usage, "Assistant messages")).to_have_text(
        re.compile(r"^Assistant messages\s*1 ·")
    )
    expect(usage.get_by_text(MOCK_MODEL_ID, exact=True).first).to_be_visible()
    expect(usage.get_by_text(NO_USAGE)).to_have_count(0)

    other = page_for(make_user())
    expect(settings_tab(other, "usage", "Usage").get_by_text(NO_USAGE)).to_be_visible()


def test_about_shows_the_release_and_its_notes(page_for, make_user):
    account = make_user()
    with account.client() as client:
        newest = next(iter(client.get("/api/changelog").json()))
    page = page_for(account)
    about = settings_tab(page, "about", "About")
    expect(about.get_by_text(RELEASE).first).to_be_visible()

    about.get_by_role("button", name="See what's new").click()

    notes = page.get_by_role("dialog").filter(has_text="What's New in")
    expect(notes).to_be_visible()
    expect(notes.get_by_role("heading", name=f"v{newest}", exact=True)).to_be_visible()
