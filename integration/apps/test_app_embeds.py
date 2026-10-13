"""Journey: apps post cards (embeds) and link buttons, and Open WebUI checks their shape.

An app message can carry up to ten cards with a title, link, description, color accent, author,
footer, thumbnail and fields, next to or instead of its text, and link buttons that open a URL
without ever calling the app. Cards are stored with the message, come back to the app when it
reads the channel, are replaced as a whole by an edit, and can be part of a private button answer.
Open WebUI enforces the limits itself, whatever the app sends: only HTTP(S) URLs without
credentials, Discord's text limits, at most 6000 card characters per message, and no cards on an
acknowledgement or a download.

Discriminates: passes on dev c7193760b with oix 0.8. Mutation `create_app_message` storing actions
only turns test_a_card_is_stored_with_the_message red; dropping the scheme and host check on card
URLs turns the javascript, ftp, no-host and data cases of test_an_unsafe_card_url_is_refused red
(credentials and spaces are refused by checks of their own).
"""

from __future__ import annotations

import time

import pytest

from harness.apps import (
    connect,
    install_raw_app,
    invite,
    mention_text,
    new_channel,
    post,
    serving_raw_app,
    wait_for_message,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]

CARD = {
    "title": "Advisory reported",
    "description": "Review **the** reported issue.",
    "url": "https://example.com/advisories/1",
    "color": 0xDA3633,
    "author": {"name": "Security Desk"},
    "footer": {"text": "Filed by the reference app"},
    "thumbnail": {"url": "https://example.com/thumbnail.png"},
    "fields": [
        {"name": "Severity", "value": "High", "inline": True},
        {"name": "Package", "value": "oix", "inline": True},
        {"name": "Summary", "value": "A field across the whole card", "inline": False},
    ],
}
SWAPPED_CARD = {
    "title": "Advisory resolved",
    "description": "Nothing left to do.",
    "url": None,
    "color": None,
    "author": None,
    "footer": None,
    "thumbnail": None,
    "fields": [],
}
LINK = {
    "action_id": None,
    "label": "View Full Report",
    "url": "https://example.com/advisories/1",
    "context": {},
}


@pytest.fixture
def channel(apps_admin, installed_app) -> str:
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        assert invite(client, channel_id, installed_app["id"]).status_code == 200
    return channel_id


@pytest.fixture
def raw(apps_admin, channel):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            assert connect(client, raw.installed["id"]).status_code == 200
            raw.wait_until_ready()
            invite(client, channel, raw.installed["id"])
        yield raw


def _app_data(client, channel_id, message_id) -> dict:
    data = client.get(f"/api/v1/channels/{channel_id}/messages/{message_id}/data")
    assert data.status_code == 200, data.text
    return data.json()["app"]


def _first_app_message(client, channel_id, app_id) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        listed = client.get(f"/api/v1/channels/{channel_id}/messages").json()
        found = [message for message in listed if message["user"]["id"] == app_id]
        if found:
            return found[0]
        time.sleep(0.2)
    raise AssertionError(f"the app posted nothing in {channel_id}")


def test_a_card_is_stored_with_the_message(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!card")
        message = _first_app_message(client, channel, installed_app["id"])
        stored = _app_data(client, channel, message["id"])
    assert message["content"] == "", "a card needs no text of its own"
    assert message["data"] is True
    assert stored["embeds"] == [CARD]
    assert stored["actions"] == [LINK]


def test_a_mention_can_be_answered_with_a_card(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        posted = post(client, channel, mention_text(installed_app, "card"))
        message = _first_app_message(client, channel, installed_app["id"])
        stored = _app_data(client, channel, message["id"])
    assert message["reply_to_id"] == posted["id"]
    assert stored["embeds"] == [CARD]


def test_an_edit_replaces_the_cards(apps_admin, installed_app, reference_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!recard")
        message = wait_for_message(client, channel, "Card")
        swapped = reference_app.wait_for("recard", app_id=installed_app["id"])
        stored = _app_data(client, channel, message["id"])
    assert swapped["message_id"] == message["id"]
    assert stored["embeds"] == [SWAPPED_CARD]


def test_an_edit_without_cards_clears_them(apps_admin, raw, channel):
    created = raw.request("create_message", channel_id=channel, content="x", embeds=[CARD])
    assert created["status"] == 200, created
    message = created["data"]
    assert message["data"]["app"]["embeds"] == [CARD]
    updated = raw.request(
        "update_message", message_id=message["id"], content="x", etag=str(message["updated_at"])
    )
    assert updated["status"] == 200, updated
    assert updated["data"]["data"]["app"]["embeds"] == []
    with apps_admin.client() as client:
        assert _app_data(client, channel, message["id"])["embeds"] == []


def test_the_app_reads_cards_back_from_the_channel(apps_admin, raw, channel):
    created = raw.request("create_message", channel_id=channel, embeds=[CARD], actions=[LINK])
    assert created["status"] == 200, created
    listed = raw.request("list_messages", channel_id=channel)
    assert listed["status"] == 200, listed
    [message] = [m for m in listed["data"]["messages"] if m["id"] == created["data"]["id"]]
    assert message["embeds"] == [CARD]
    assert message["actions"] == [LINK]


def test_a_button_can_answer_privately_with_a_card(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!tools")
        message = wait_for_message(client, channel, "Tools")
        pressed = client.post(
            f"/api/v1/apps/{installed_app['id']}/interactions",
            json={"message_id": message["id"], "action_id": "card"},
        )
    assert pressed.status_code == 200, pressed.text
    assert pressed.json()["visibility"] == "private"
    assert pressed.json()["embeds"] == [CARD]
    assert "message" not in pressed.json(), "a private answer is not stored"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "ftp://example.com/file",
        "https:///no-host",
        "https://example.com/a b",
        "data:text/html,hi",
    ],
    ids=["javascript", "credentials", "ftp", "no-host", "space", "data"],
)
@pytest.mark.parametrize("where", ["title-link", "thumbnail", "link-button"])
def test_an_unsafe_card_url_is_refused(raw, channel, url, where):
    form = {"channel_id": channel, "content": "x"}
    if where == "title-link":
        form["embeds"] = [{"title": "t", "url": url}]
    elif where == "thumbnail":
        form["embeds"] = [{"title": "t", "thumbnail": {"url": url}}]
    else:
        form["actions"] = [{"label": "Open", "url": url}]
    answer = raw.request("create_message", **form)
    assert answer == {"status": 400, "error": "Invalid app operation arguments"}


@pytest.mark.parametrize(
    "form",
    [
        {"embeds": [{"title": "x" * 257}]},
        {"embeds": [{"description": "x" * 4097}]},
        {"embeds": [{"author": {"name": "x" * 257}}]},
        {"embeds": [{"footer": {"text": "x" * 2049}}]},
        {"embeds": [{"fields": [{"name": "n", "value": "x" * 1025}]}]},
        {"embeds": [{"fields": [{"name": "x" * 257, "value": "v"}]}]},
        {"embeds": [{"fields": [{"name": "", "value": "v"}]}]},
        {"embeds": [{"fields": [{"name": "n", "value": "v"}] * 26}]},
        {"embeds": [{"title": "t"}] * 11},
        {"embeds": [{"description": "x" * 4000}, {"description": "x" * 2001}]},
        {"embeds": [{"color": 0x1000000}]},
        {"embeds": [{"color": -1}]},
        {"embeds": [{"color": "#ff0000"}]},
        {"embeds": [{"title": "t", "image": {"url": "https://example.com/i.png"}}]},
        {"actions": [{"action_id": "go", "label": "Go", "url": "https://example.com/"}]},
        {"actions": [{"label": "Neither"}]},
        {"actions": [{"label": "Open", "url": "https://example.com/", "context": {"a": 1}}]},
    ],
    ids=[
        "title",
        "description",
        "author",
        "footer",
        "field-value",
        "field-name",
        "empty-field-name",
        "26-fields",
        "11-cards",
        "6001-characters",
        "color-too-high",
        "color-negative",
        "color-string",
        "unknown-key",
        "link-and-action",
        "neither-link-nor-action",
        "link-with-context",
    ],
)
def test_a_card_over_the_limits_is_refused(raw, channel, form):
    answer = raw.request("create_message", channel_id=channel, content="x", **form)
    assert answer == {"status": 400, "error": "Invalid app operation arguments"}


def test_cards_right_at_the_limits_are_accepted(raw, channel):
    full = {
        "title": "t" * 256,
        "description": "d" * 4096,
        "fields": [{"name": "n", "value": "v" * 1024}],
        "color": 0xFFFFFF,
    }
    answer = raw.request(
        "create_message",
        channel_id=channel,
        embeds=[full, {"description": "x" * (6000 - 256 - 4096 - 1 - 1024)}],
    )
    assert answer["status"] == 200, answer
    ten = raw.request("create_message", channel_id=channel, embeds=[{"title": "t"}] * 10)
    assert ten["status"] == 200, ten


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "ack", "embeds": [{"title": "t"}]},
        {"type": "download", "filename": "f.md", "content": "x", "embeds": [{"title": "t"}]},
        {"type": "error", "content": "no", "embeds": [{"title": "t"}]},
        {"content": "x", "embeds": [{"title": "t", "url": "javascript:alert(1)"}]},
        {"content": "x", "actions": [{"label": "Open", "url": "https://example.com/"}]},
    ],
    ids=["ack", "download", "error", "unsafe-url", "private-link-button"],
)
def test_a_button_answer_with_misplaced_cards_is_refused(apps_admin, raw, channel, answer):
    created = raw.request(
        "create_message",
        channel_id=channel,
        content="Pick",
        actions=[{"action_id": "go", "label": "Go"}],
    )
    assert created["status"] == 200, created
    raw.event_answer = answer
    with apps_admin.client() as client:
        pressed = client.post(
            f"/api/v1/apps/{raw.installed['id']}/interactions",
            json={"message_id": created["data"]["id"], "action_id": "go"},
        )
    assert pressed.status_code == 502, pressed.text


def test_a_channel_button_answer_can_post_a_card(apps_admin, raw, channel):
    created = raw.request(
        "create_message",
        channel_id=channel,
        content="Pick",
        actions=[{"action_id": "go", "label": "Go"}],
    )
    raw.event_answer = {"visibility": "channel", "content": "Posted", "embeds": [CARD]}
    with apps_admin.client() as client:
        pressed = client.post(
            f"/api/v1/apps/{raw.installed['id']}/interactions",
            json={"message_id": created["data"]["id"], "action_id": "go"},
        )
        assert pressed.status_code == 200, pressed.text
        message = wait_for_message(client, channel, "Posted")
        assert _app_data(client, channel, message["id"])["embeds"] == [CARD]
