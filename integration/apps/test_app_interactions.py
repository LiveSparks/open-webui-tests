"""Journey: mentioning an app, pressing its buttons and acknowledging work it finishes later.

A mention or a button press is an interaction: Open WebUI sends the app the actor and a signed
execution token, the app rechecks it, and answers within 2.5 seconds. A private answer reaches
only the person who asked, over their socket (`events:app`); a channel answer becomes an app
message everyone sees; an acknowledgement says nothing at all. Work that takes longer is
acknowledged and published later through the message API, after the app rechecks the asker's
access with the same execution token.

Discriminates: mutation, `app_channel_reply_handler` emitting the private reply to the whole
channel, turns test_a_private_answer_reaches_only_the_person_who_asked red;
`authorize_app_execution` skipping the channel access check turns
test_background_work_loses_access_when_the_asker_leaves red.
"""

from __future__ import annotations

import time

import pytest

from harness.apps import (
    channel_messages,
    installed,
    invite,
    mention_text,
    new_channel,
    post,
    reply_events,
    wait_for_message,
)
from harness.socket_client import connected

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


@pytest.fixture
def channel(apps_admin, installed_app) -> str:
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        assert invite(client, channel_id, installed_app["id"]).status_code == 200
    return channel_id


def _mention(actor, channel_id, app, text, **form):
    with actor.client() as client:
        return post(client, channel_id, mention_text(app, text), **form)


def _press(actor, app_id, message_id, action_id):
    with actor.client() as client:
        return client.post(
            f"/api/v1/apps/{app_id}/interactions",
            json={"message_id": message_id, "action_id": action_id},
        )


def test_a_mention_gets_a_private_answer(apps_admin, installed_app, reference_app, channel):
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel, installed_app, "hi there")
        event = reply_events(socket, message_id=posted["id"])
    assert event["app_id"] == installed_app["id"]
    assert event["app_name"] == installed_app["name"]
    assert event["channel_id"] == channel
    assert event["reply"]["visibility"] == "private"
    assert event["reply"]["content"].startswith("pong: ")
    seen = reference_app.wait_for("mention", app_id=installed_app["id"], channel_id=channel)
    assert seen["actor"] == {"id": apps_admin.id, "name": apps_admin.name, "is_admin": True}
    assert seen["data"]["message_id"] == posted["id"]


def test_a_private_answer_reaches_only_the_person_who_asked(
    apps_admin, make_apps_user, installed_app
):
    asker, bystander = make_apps_user(), make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[asker.id, bystander.id])
        invite(client, channel_id, installed_app["id"])
    with connected(asker) as asker_socket, connected(bystander) as bystander_socket:
        posted = _mention(asker, channel_id, installed_app, "just for me")
        reply_events(asker_socket, message_id=posted["id"])
        time.sleep(1)
        assert bystander_socket.app_events == []
    with bystander.client() as client:
        contents = [message["content"] for message in channel_messages(client, channel_id)]
    assert not any(content.startswith("pong:") for content in contents)


def test_a_public_answer_becomes_a_channel_message(apps_admin, installed_app, channel):
    _mention(apps_admin, channel, installed_app, "public")
    with apps_admin.client() as client:
        answer = wait_for_message(client, channel, f"public answer for {apps_admin.id}")
    assert answer["user"]["id"] == installed_app["id"]


def test_a_mention_in_a_thread_is_answered_in_that_thread(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        root = post(client, channel, "thread root")
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel, installed_app, "in thread", parent_id=root["id"])
        event = reply_events(socket, message_id=posted["id"])
    assert event["parent_id"] == root["id"]


def test_a_download_answer_carries_the_file(apps_admin, installed_app, channel):
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel, installed_app, "report")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["type"] == "download"
    assert event["reply"]["filename"] == "weekly-report.md"
    assert event["reply"]["title"] == "Weekly report"


def test_an_error_answer_reaches_the_asker(apps_admin, installed_app, channel):
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel, installed_app, "fail")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["type"] == "error"


@pytest.mark.parametrize("word", ["slow", "crash"])
def test_an_app_that_fails_to_answer_tells_the_asker(apps_admin, installed_app, channel, word):
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel, installed_app, word)
        event = reply_events(socket, message_id=posted["id"], timeout=30)
    assert event["reply"]["type"] == "error"
    assert "could not respond" in event["reply"]["content"]


def test_the_app_rechecks_the_execution_token(apps_admin, make_apps_user, installed_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, installed_app["id"])
    with connected(member) as socket:
        posted = _mention(member, channel_id, installed_app, "authorize")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["content"] == f"authorized {member.id} admin=False"


def test_mentioning_two_apps_asks_both(apps_instance, apps_admin, reference_app):
    from harness.apps import serving_reference_app

    with serving_reference_app(name="Other App") as other:
        with apps_admin.client() as client:
            first = installed(client, reference_app)
            second = installed(client, other)
            channel_id = new_channel(client)
            invite(client, channel_id, first["id"])
            invite(client, channel_id, second["id"])
        with connected(apps_admin) as socket:
            text = f"{mention_text(first, '')} {mention_text(second, 'both of you')}"
            with apps_admin.client() as client:
                posted = post(client, channel_id, text)
            reply_events(socket, message_id=posted["id"], app_id=first["id"])
            reply_events(socket, message_id=posted["id"], app_id=second["id"])


def test_a_mention_of_an_app_outside_the_channel_is_refused(apps_admin, installed_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel_id, installed_app, "are you here?")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["type"] == "error"


def test_a_user_without_access_to_the_app_gets_an_error(apps_admin, make_apps_user, reference_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        restricted = installed(client, reference_app, access_grants=[])
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, restricted["id"])
    with connected(member) as socket:
        posted = _mention(member, channel_id, restricted, "let me in")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["type"] == "error"
    assert reference_app.records("mention", app_id=restricted["id"]) == []


def test_a_mention_only_app_hears_mentions_but_not_other_messages(apps_admin, reference_app):
    with apps_admin.client() as client:
        quiet = installed(client, reference_app, scopes=["channels:mentions:read"])
        channel_id = new_channel(client)
        invite(client, channel_id, quiet["id"])
        post(client, channel_id, "an ordinary message")
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel_id, quiet, "only mentions")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["content"].startswith("pong:")
    time.sleep(1)
    assert reference_app.records("message.created", app_id=quiet["id"]) == []


def test_an_app_without_write_scope_cannot_post_publicly(apps_admin, reference_app):
    read_only = ["channels:mentions:read", "channels:messages:read", "channels:read"]
    with apps_admin.client() as client:
        app = installed(client, reference_app, scopes=read_only)
        channel_id = new_channel(client)
        invite(client, channel_id, app["id"])
        post(client, channel_id, "!send not allowed")
    refused = reference_app.wait_for("api_error", app_id=app["id"], channel_id=channel_id)
    assert refused["status"] == 403
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel_id, app, "public")
        event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["type"] == "error"


def test_an_app_without_channel_scope_cannot_list_channels(apps_admin, reference_app):
    scopes = ["channels:messages:read", "channels:messages:write"]
    with apps_admin.client() as client:
        app = installed(client, reference_app, scopes=scopes)
        channel_id = new_channel(client)
        invite(client, channel_id, app["id"])
        post(client, channel_id, "!channels")
    refused = reference_app.wait_for("api_error", app_id=app["id"], channel_id=channel_id)
    assert refused["status"] == 403
    assert refused["method"] == "list_channels"


def test_a_channel_message_with_buttons_stores_them(apps_admin, installed_app, channel):
    _mention(apps_admin, channel, installed_app, "buttons")
    with apps_admin.client() as client:
        message = wait_for_message(client, channel, "Choose")
        data = client.get(f"/api/v1/channels/{channel}/messages/{message['id']}/data").json()
    assert message["data"] is True, "the list only flags that the message carries data"
    labels = [action["label"] for action in data["app"]["actions"]]
    assert labels == ["Approve", "Who am I", "Reject", "Noted"]


def test_pressing_a_button_updates_the_message_for_everyone(apps_admin, installed_app, channel):
    _mention(apps_admin, channel, installed_app, "buttons")
    with apps_admin.client() as client:
        message = wait_for_message(client, channel, "Choose")
    pressed = _press(apps_admin, installed_app["id"], message["id"], "approve")
    assert pressed.status_code == 200, pressed.text
    assert pressed.json()["type"] == "update"
    with apps_admin.client() as client:
        wait_for_message(client, channel, f"Approved by {apps_admin.id}")


def test_a_button_without_its_own_handler_reaches_the_generic_one(
    apps_admin, installed_app, reference_app, channel
):
    with apps_admin.client() as client:
        post(client, channel, "!buttons")
        message = wait_for_message(client, channel, "Pick one")
    pressed = _press(apps_admin, installed_app["id"], message["id"], "reject")
    assert pressed.status_code == 200, pressed.text
    assert pressed.json()["content"] == 'unhandled reject {"reason": "unhandled"}'
    seen = reference_app.wait_for("action.generic", app_id=installed_app["id"])
    assert seen["data"]["context"] == {"reason": "unhandled"}


def test_a_button_answer_names_whoever_pressed_it(apps_admin, make_apps_user, installed_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, installed_app["id"])
        post(client, channel_id, "!buttons")
        message = wait_for_message(client, channel_id, "Pick one")
    by_member = _press(member, installed_app["id"], message["id"], "whoami")
    by_admin = _press(apps_admin, installed_app["id"], message["id"], "whoami")
    assert by_member.json()["content"] == f"you are {member.id} admin=False"
    assert by_admin.json()["content"] == f"you are {apps_admin.id} admin=True"


def test_an_unknown_button_is_refused(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!buttons")
        message = wait_for_message(client, channel, "Pick one")
    pressed = _press(apps_admin, installed_app["id"], message["id"], "self-destruct")
    assert pressed.status_code == 400
    assert pressed.json()["detail"] == "Unknown action"


def test_a_button_on_a_human_message_is_not_found(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        human = post(client, channel, "no buttons here")
    pressed = _press(apps_admin, installed_app["id"], human["id"], "approve")
    assert pressed.status_code == 404


def test_someone_outside_the_channel_cannot_press_its_buttons(
    apps_admin, make_apps_user, installed_app
):
    outsider = make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group")
        invite(client, channel_id, installed_app["id"])
        post(client, channel_id, "!buttons")
        message = wait_for_message(client, channel_id, "Pick one")
    pressed = _press(outsider, installed_app["id"], message["id"], "whoami")
    assert pressed.status_code == 403


def test_a_disabled_app_takes_no_button_presses(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!buttons")
        message = wait_for_message(client, channel, "Pick one")
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"is_active": False})
    pressed = _press(apps_admin, installed_app["id"], message["id"], "whoami")
    assert pressed.status_code == 403
    assert pressed.json()["detail"] == "App is inactive"


def test_an_ack_answers_without_any_message(apps_admin, installed_app, channel):
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel, installed_app, "ack")
        time.sleep(2)
        app_replies = [event for event in socket.app_events if event["message_id"] == posted["id"]]
    with apps_admin.client() as client:
        contents = [message["content"] for message in channel_messages(client, channel)]
    assert app_replies == [], "an acknowledgement shows nothing to the asker"
    assert contents == [posted["content"]], "and posts nothing in the channel"


def test_a_button_press_can_be_acknowledged_silently(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!buttons")
        message = wait_for_message(client, channel, "Pick one")
    _mention(apps_admin, channel, installed_app, "ack")
    pressed = _press(apps_admin, installed_app["id"], message["id"], "reject")
    assert pressed.status_code == 200


def test_background_work_publishes_its_result_after_an_ack(
    apps_admin, installed_app, reference_app, channel
):
    posted = _mention(apps_admin, channel, installed_app, "later")
    with apps_admin.client() as client:
        result = wait_for_message(client, channel, f"Job finished for {apps_admin.name}")
    assert result["reply_to_id"] == posted["id"]
    assert result["user"]["id"] == installed_app["id"]
    reference_app.wait_for("later.finished", app_id=installed_app["id"], channel_id=channel)


def test_background_work_loses_access_when_the_asker_leaves(
    apps_admin, make_apps_user, installed_app, reference_app
):
    member = make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, installed_app["id"])
    _mention(member, channel_id, installed_app, "later")
    # the app has acknowledged and is working; only now does the asker leave
    reference_app.wait_for("mention", app_id=installed_app["id"], channel_id=channel_id)
    with apps_admin.client() as client:
        removed = client.post(
            f"/api/v1/channels/{channel_id}/update/members/remove", json={"user_ids": [member.id]}
        )
    assert removed.status_code == 200, removed.text
    refused = reference_app.wait_for(
        "later.refused", app_id=installed_app["id"], channel_id=channel_id
    )
    assert refused["status"] == 403
    with apps_admin.client() as client:
        contents = [message["content"] for message in channel_messages(client, channel_id)]
    assert not any(content.startswith("Job finished") for content in contents)


def test_an_ack_with_content_is_refused(apps_admin, make_apps_user, channel):
    """Open WebUI checks the reply shape itself, whatever an app sends."""
    from harness.apps import connect, install_raw_app, serving_raw_app

    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            assert connect(client, raw.installed["id"]).status_code == 200
            raw.wait_until_ready()
            invite(client, channel, raw.installed["id"])
        raw.event_answer = {"type": "ack", "content": "sneaky"}
        with apps_admin.client() as client:
            post(client, channel, "!buttons")
        with connected(apps_admin) as socket:
            posted = _mention(apps_admin, channel, raw.installed, "anything")
            event = reply_events(socket, message_id=posted["id"])
    assert event["reply"]["type"] == "error"


def test_leaving_the_channel_revokes_interactions(apps_admin, make_apps_user, installed_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, installed_app["id"])
        post(client, channel_id, "!buttons")
        message = wait_for_message(client, channel_id, "Pick one")
        assert _press(member, installed_app["id"], message["id"], "whoami").status_code == 200
        removed = client.post(
            f"/api/v1/channels/{channel_id}/update/members/remove", json={"user_ids": [member.id]}
        )
    assert removed.status_code == 200, removed.text
    assert _press(member, installed_app["id"], message["id"], "whoami").status_code == 403


def test_losing_access_to_the_app_revokes_interactions(apps_admin, make_apps_user, installed_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, installed_app["id"])
        post(client, channel_id, "!buttons")
        message = wait_for_message(client, channel_id, "Pick one")
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"access_grants": []})
    pressed = _press(member, installed_app["id"], message["id"], "whoami")
    assert pressed.status_code == 403
    assert pressed.json()["detail"] == "App invocation denied"
