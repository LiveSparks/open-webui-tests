"""Journey: people see an app's cards and link buttons in a channel.

An app message can carry cards: an author line, a title that links out, a description and
fields written in Markdown, a color accent and a footer. Link buttons open their URL in a new
tab and never call the app. A card swapped by an edit is replaced in place for everyone, a
button can answer privately with a card, and a reply to a card quotes the card's text.

Discriminates: passes on the dev c7193760b build except test_a_reply_to_a_card_quotes_its_text,
which is red there: a reply's quoted message carries only a flag that it has data, so a card-only
message is quoted with no text. In one frontend copy, rendering link buttons like action buttons
turns the link test red, card Markdown with HTML allowed turns the raw HTML test red, and showing
the app block only for messages with buttons turns the edited card test red.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Page, expect

from harness.apps import (
    connect,
    install_raw_app,
    invite,
    new_channel,
    post,
    serving_raw_app,
    wait_for_message,
)
from utils.chat_ui import chat_input

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

ANSWER_TIMEOUT_MS = 20_000


def _channel_with_app(admin, app, **form) -> str:
    with admin.client() as client:
        channel_id = new_channel(client, **form)
        assert invite(client, channel_id, app["id"]).status_code == 200
    return channel_id


def _open_channel(page: Page, channel_id: str) -> None:
    page.goto(f"/channels/{channel_id}")
    expect(chat_input(page)).to_be_visible()


def _card(page: Page, title: str):
    return page.locator(".app-embed").filter(has_text=title)


def _post_card(admin, channel_id: str, app_id: str) -> dict:
    with admin.client() as client:
        post(client, channel_id, "!card")
        listed = []
        for _ in range(100):
            listed = client.get(f"/api/v1/channels/{channel_id}/messages").json()
            found = [message for message in listed if message["user"]["id"] == app_id]
            if found:
                return found[0]
            time.sleep(0.2)
    raise AssertionError(f"the app posted no card: {listed}")


def test_a_card_shows_every_part(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    _post_card(apps_admin, channel_id, installed_app["id"])
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    card = _card(page, "Advisory reported")
    expect(card).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(card).to_contain_text("Security Desk")
    title = card.get_by_role("link", name="Advisory reported")
    expect(title).to_have_attribute("href", "https://example.com/advisories/1")
    expect(title).to_have_attribute("target", "_blank")
    expect(card.locator("strong", has_text="the")).to_be_visible()
    for text in ("Severity", "High", "Package", "oix", "Summary", "A field across the whole card"):
        expect(card.get_by_text(text, exact=True)).to_be_visible()
    expect(card).to_contain_text("Filed by the reference app")
    expect(card.locator("svg rect")).to_have_attribute("fill", "#da3633")


def test_a_link_button_opens_its_url_without_calling_the_app(
    page_for, apps_admin, installed_app, reference_app
):
    channel_id = _channel_with_app(apps_admin, installed_app)
    _post_card(apps_admin, channel_id, installed_app["id"])
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    link = page.get_by_role("link", name="View Full Report")
    expect(link).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(link).to_have_attribute("href", "https://example.com/advisories/1")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(page.get_by_role("button", name="View Full Report")).to_have_count(0)
    page.context.route("https://example.com/**", lambda route: route.fulfill(body="report"))
    with page.context.expect_page() as opened:
        link.click()
    assert opened.value.url == "https://example.com/advisories/1"
    opened.value.close()
    page.wait_for_timeout(1000)
    assert reference_app.records("action.generic", app_id=installed_app["id"]) == []


def test_a_card_description_shows_no_raw_html(page_for, apps_admin, raw_card_app):
    raw, channel_id = raw_card_app
    created = raw.request(
        "create_message",
        channel_id=channel_id,
        embeds=[
            {
                "title": "Untrusted",
                "description": 'before <img src="x" id="injected-image"> after',
                "fields": [{"name": "Field", "value": '<b id="injected-bold">bold?</b>'}],
            }
        ],
    )
    assert created["status"] == 200, created
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    card = _card(page, "Untrusted")
    expect(card).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(card).to_contain_text("before")
    expect(page.locator("#injected-image")).to_have_count(0)
    expect(page.locator("#injected-bold")).to_have_count(0)


def test_an_edited_card_is_replaced_for_everyone(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    watcher = page_for(member)
    _open_channel(watcher, channel_id)
    with apps_admin.client() as client:
        post(client, channel_id, "!recard")
    expect(_card(watcher, "Advisory resolved")).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(_card(watcher, "Advisory reported")).to_have_count(0)
    watcher.reload()
    expect(_card(watcher, "Advisory resolved")).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(_card(watcher, "Advisory reported")).to_have_count(0)


def test_a_button_can_answer_privately_with_a_card(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    with apps_admin.client() as client:
        post(client, channel_id, "!tools")
        wait_for_message(client, channel_id, "Tools")
    presser, watcher = page_for(member), page_for(apps_admin)
    _open_channel(presser, channel_id)
    _open_channel(watcher, channel_id)
    presser.get_by_role("button", name="Card", exact=True).click()
    reply = presser.locator(f"[data-app-reply='{installed_app['id']}']")
    expect(reply.locator(".app-embed")).to_contain_text(
        "Advisory reported", timeout=ANSWER_TIMEOUT_MS
    )
    expect(reply).to_contain_text("Only visible to you")
    expect(watcher.locator(f"[data-app-reply='{installed_app['id']}']")).to_have_count(0)


def test_a_reply_to_a_card_quotes_its_text(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    card_message = _post_card(apps_admin, channel_id, installed_app["id"])
    with apps_admin.client() as client:
        post(client, channel_id, "about that card", reply_to_id=card_message["id"])
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    expect(page.get_by_text("about that card")).to_be_visible()
    quote = page.get_by_role("button").filter(has_text=installed_app["name"]).first
    expect(quote).to_contain_text("Advisory reported", timeout=ANSWER_TIMEOUT_MS)


@pytest.fixture
def raw_card_app(apps_admin):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            assert connect(client, raw.installed["id"]).status_code == 200
            raw.wait_until_ready()
            channel_id = new_channel(client)
            invite(client, channel_id, raw.installed["id"])
        yield raw, channel_id
