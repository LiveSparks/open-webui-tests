"""Journey: an invited app hears channel messages and talks back through its signed API.

With `channels:messages:read` the app is sent every created, edited and deleted message of the
channels it joined, verifies each with Open WebUI, and answers through the runtime API with
`channels:messages:write`: replies, thread posts, history pages, its own edits guarded by an
etag. Several people talk to it at once; each message reaches it once, with its author.

Discriminates: mutation, `update_app_message` skipping the If-Match comparison, turns
test_an_edit_with_a_stale_etag_is_refused red; selecting every app for an event regardless of
channel, with the membership recheck before sending also dropped, turns
test_messages_in_other_channels_never_reach_the_app red (either guard alone still holds).
"""

from __future__ import annotations

import time

import pytest

from harness.apps import (
    channel_messages,
    invite,
    new_channel,
    post,
    wait_for_message,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


@pytest.fixture
def channel(apps_admin, installed_app) -> str:
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        assert invite(client, channel_id, installed_app["id"]).status_code == 200
    return channel_id


def _seen(reference_app, installed_app, kind, channel_id):
    return [
        entry
        for entry in reference_app.records(kind, app_id=installed_app["id"])
        if entry["channel_id"] == channel_id
    ]


def test_a_new_message_reaches_the_app_with_its_author(
    apps_admin, installed_app, reference_app, channel
):
    with apps_admin.client() as client:
        posted = post(client, channel, "hello app")
    seen = reference_app.wait_for("message.created", app_id=installed_app["id"], channel_id=channel)
    assert seen["message"]["id"] == posted["id"]
    assert seen["message"]["content"] == "hello app"
    assert seen["message"]["author"] == {"type": "user", "id": apps_admin.id}
    assert seen["webui_url"] == apps_admin.base_url


def test_the_app_replies_to_a_message_it_heard(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        posted = post(client, channel, "!echo ping")
        answer = wait_for_message(client, channel, "echo: ping")
    assert answer["reply_to_id"] == posted["id"]
    assert answer["user"]["id"] == installed_app["id"]
    assert answer["user"]["role"] == "app"


def test_an_app_message_is_shown_under_the_app_name(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!send shown as the app")
        answer = wait_for_message(client, channel, "shown as the app")
    assert answer["user"]["name"] == installed_app["name"]


def test_the_app_starts_a_thread_and_answers_inside_threads(apps_admin, channel):
    with apps_admin.client() as client:
        root = post(client, channel, "!thread first thread reply")
        in_thread = wait_for_message(client, channel, "first thread reply", parent_id=root["id"])
        post(client, channel, "!send threaded answer", parent_id=root["id"])
        threaded = wait_for_message(client, channel, "threaded answer", parent_id=root["id"])
        top_level = [message["content"] for message in channel_messages(client, channel)]
    assert in_thread["parent_id"] == root["id"]
    assert threaded["parent_id"] == root["id"]
    assert "threaded answer" not in top_level


def test_an_edited_message_reaches_the_app(apps_admin, installed_app, reference_app, channel):
    with apps_admin.client() as client:
        posted = post(client, channel, "first words")
        edited = client.post(
            f"/api/v1/channels/{channel}/messages/{posted['id']}/update",
            json={"content": "second words"},
        )
    assert edited.status_code == 200
    seen = reference_app.wait_for("message.updated", app_id=installed_app["id"], channel_id=channel)
    assert seen["message"]["id"] == posted["id"]
    assert seen["message"]["content"] == "second words"


def test_a_deleted_message_reaches_the_app_without_its_content(
    apps_admin, installed_app, reference_app, channel
):
    with apps_admin.client() as client:
        posted = post(client, channel, "soon gone")
        deleted = client.delete(f"/api/v1/channels/{channel}/messages/{posted['id']}/delete")
    assert deleted.status_code == 200
    seen = reference_app.wait_for("message.deleted", app_id=installed_app["id"], channel_id=channel)
    assert seen["message"]["id"] == posted["id"]
    assert "content" not in seen["message"]


def test_the_app_reads_the_channel_history(apps_admin, channel):
    with apps_admin.client() as client:
        for number in range(3):
            post(client, channel, f"older {number}")
        post(client, channel, "!history")
        wait_for_message(client, channel, "history: 4")


def test_history_pages_follow_the_cursor_without_overlap(
    apps_admin, installed_app, reference_app, channel
):
    with apps_admin.client() as client:
        older = post(client, channel, "the older one")
        newer = post(client, channel, "!page")
    paged = reference_app.wait_for("page", app_id=installed_app["id"], channel_id=channel)
    assert paged["ids"] == [newer["id"], older["id"]]


def test_the_app_lists_only_channels_it_joined(apps_admin, installed_app, reference_app):
    with apps_admin.client() as client:
        joined = new_channel(client, name="joined-by-app")
        new_channel(client, name="not-joined-by-app")
        invite(client, joined, installed_app["id"])
        post(client, joined, "!channels")
    listed = reference_app.wait_for("channels", app_id=installed_app["id"], channel_id=joined)
    assert [entry["id"] for entry in listed["channels"]] == [joined]
    assert listed["channels"][0]["is_private"] is True


def test_the_app_reads_its_own_installation(apps_admin, installed_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!me")
        wait_for_message(client, channel, f"me: {installed_app['slug']}")


def test_the_app_edits_its_own_message(apps_admin, channel):
    with apps_admin.client() as client:
        post(client, channel, "!progress")
        done = wait_for_message(client, channel, "Done.")
    assert done["updated_at"] > done["created_at"]


def test_an_edit_with_a_stale_etag_is_refused(apps_admin, installed_app, reference_app, channel):
    with apps_admin.client() as client:
        post(client, channel, "!progress")
        wait_for_message(client, channel, "Done.")
    refused = reference_app.wait_for("api_error", app_id=installed_app["id"], channel_id=channel)
    assert refused["status"] == 412
    assert refused["method"] == "PATCH"
    with apps_admin.client() as client:
        contents = [message["content"] for message in channel_messages(client, channel)]
    assert "Stale edit." not in contents


def test_the_app_ignores_its_own_messages_by_default(
    apps_admin, installed_app, reference_app, channel
):
    with apps_admin.client() as client:
        post(client, channel, "!loop")
        wait_for_message(client, channel, "loop: first")
    time.sleep(2)
    with apps_admin.client() as client:
        contents = [message["content"] for message in channel_messages(client, channel)]
    assert "loop: second" not in contents
    authors = [
        entry["message"]["author"]["type"]
        for entry in _seen(reference_app, installed_app, "message.created", channel)
    ]
    assert authors == ["user"]


def test_an_app_hearing_itself_answers_its_own_message_once(apps_instance, apps_admin):
    from harness.apps import installed, serving_reference_app

    with serving_reference_app([apps_instance.base_url], ignore_self=False) as app:
        with apps_admin.client() as client:
            loud = installed(client, app)
            channel_id = new_channel(client)
            invite(client, channel_id, loud["id"])
            post(client, channel_id, "!loop")
            wait_for_message(client, channel_id, "loop: second")


def test_messages_in_other_channels_never_reach_the_app(
    apps_admin, installed_app, reference_app, channel
):
    with apps_admin.client() as client:
        elsewhere = new_channel(client)
        post(client, elsewhere, "not for the app")
        post(client, channel, "for the app")
    reference_app.wait_for("message.created", app_id=installed_app["id"], channel_id=channel)
    time.sleep(1)
    assert _seen(reference_app, installed_app, "message.created", elsewhere) == []


def test_several_people_talking_at_once_each_reach_the_app_once(
    apps_admin, make_apps_user, installed_app, reference_app
):
    people = [make_apps_user() for _ in range(4)]
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[person.id for person in people])
        invite(client, channel_id, installed_app["id"])
    for round_number in range(3):
        for person in people:
            with person.client() as client:
                post(client, channel_id, f"{person.id} says {round_number}")
    deadline = time.monotonic() + 30
    seen = []
    while time.monotonic() < deadline and len(seen) < 12:
        seen = _seen(reference_app, installed_app, "message.created", channel_id)
        time.sleep(0.2)
    contents = sorted(entry["message"]["content"] for entry in seen)
    expected = sorted(f"{person.id} says {n}" for person in people for n in range(3))
    assert contents == expected
    for entry in seen:
        assert entry["message"]["content"].startswith(entry["message"]["author"]["id"])


def test_several_people_each_get_their_own_answer(apps_admin, make_apps_user, installed_app):
    people = [make_apps_user() for _ in range(3)]
    with apps_admin.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[person.id for person in people])
        invite(client, channel_id, installed_app["id"])
    posted = {}
    for person in people:
        with person.client() as client:
            posted[person.id] = post(client, channel_id, f"!echo from {person.name}")
    with people[0].client() as client:
        for person in people:
            answer = wait_for_message(client, channel_id, f"echo: from {person.name}")
            assert answer["reply_to_id"] == posted[person.id]["id"]


def test_one_message_reaches_every_app_in_the_channel(apps_instance, apps_admin, reference_app):
    from harness.apps import installed, serving_reference_app

    with serving_reference_app([apps_instance.base_url], name="Second App") as second:
        with apps_admin.client() as client:
            first_install = installed(client, reference_app)
            second_install = installed(client, second)
            channel_id = new_channel(client)
            invite(client, channel_id, first_install["id"])
            invite(client, channel_id, second_install["id"])
            post(client, channel_id, "for both apps")
        reference_app.wait_for("message.created", app_id=first_install["id"], channel_id=channel_id)
        second.wait_for("message.created", app_id=second_install["id"], channel_id=channel_id)


def test_apps_hear_each_others_messages(apps_instance, apps_admin, reference_app):
    from harness.apps import installed, serving_reference_app

    with serving_reference_app([apps_instance.base_url], name="Listener App") as listener_app:
        with apps_admin.client() as client:
            speaker = installed(client, reference_app)
            listener_install = installed(client, listener_app)
            channel_id = new_channel(client)
            invite(client, channel_id, speaker["id"])
            invite(client, channel_id, listener_install["id"])
            post(client, channel_id, "!send spoken by an app")
        heard = [
            entry
            for entry in _wait_all(listener_app, listener_install["id"], channel_id, 2)
            if entry["message"]["author"]["type"] == "app"
        ]
    assert heard[0]["message"]["author"]["id"] == speaker["id"]


def _wait_all(app, app_id, channel_id, count, timeout=20.0):
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        seen = [
            entry
            for entry in app.records("message.created", app_id=app_id)
            if entry["channel_id"] == channel_id
        ]
        if len(seen) >= count:
            return seen
        time.sleep(0.1)
    raise AssertionError(f"only {len(seen)} of {count} messages reached the app: {seen}")
