"""Journey: an app's runtime calls are trusted only inside the channels and scopes it was given.

An app has no HTTP route into Open WebUI any more: it calls the runtime (`get_me`,
`list_channels`, `list_messages`, `create_message`, `update_message`, `authorize`,
`verify_event`) as `app:request` packets over the Socket.IO connection Open WebUI opened to
it. These tests drive a scripted app endpoint (`harness.apps.RawApp`) that sends exactly the
packets a misbehaving app could, and read the `{status, error}` answers.

Discriminates: mutation, `check_app_channel_membership` returning without checking the
invitation, turns test_an_app_cannot_read_a_channel_it_was_not_invited_into red;
`update_app_message` skipping the If-Match comparison turns
test_an_edit_with_a_stale_etag_is_refused red; `AppConnection.request` serving requests
before `app:ready` turns test_nothing_but_a_ping_is_served_before_the_connection_is_ready red.
"""

from __future__ import annotations

import time

import httpx
import pytest

from harness.apps import (
    ALL_SCOPES,
    connect,
    install_raw_app,
    invite,
    new_channel,
    post,
    serving_raw_app,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


@pytest.fixture
def raw_app(apps_admin):
    """The scripted app, installed with every scope and connected."""
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            connected = connect(client, raw.installed["id"])
        assert connected.status_code == 200, connected.text
        raw.wait_until_ready()
        yield raw


def _joined_channel(admin, raw) -> str:
    with admin.client() as client:
        channel_id = new_channel(client)
        assert invite(client, channel_id, raw.installed["id"]).status_code == 200
    return channel_id


def _event_for(raw, message_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in list(raw.events):
            if (event.get("message") or {}).get("id") == message_id:
                return event
        time.sleep(0.1)
    raise AssertionError(f"the app never received message {message_id}")


def test_get_me_answers_with_the_installation_and_no_secret(raw_app):
    answer = raw_app.request("get_me")
    assert answer["status"] == 200
    assert answer["data"]["id"] == raw_app.installed["id"]
    assert raw_app.api_key not in str(answer)
    assert "connection_token" not in str(answer)


def test_an_unknown_operation_is_refused(raw_app):
    assert raw_app.request("drop_tables") == {"status": 400, "error": "Unknown app operation"}


def test_malformed_arguments_are_refused(raw_app, apps_admin):
    channel_id = _joined_channel(apps_admin, raw_app)
    answers = [
        raw_app.request("get_me", surprise=True),
        raw_app.request("list_messages", channel_id=channel_id, limit=1000),
        raw_app.request("create_message", content="no channel"),
        raw_app.request("create_message", channel_id=channel_id, content="x", admin=True),
    ]
    assert [answer["status"] for answer in answers] == [400, 400, 400, 400]


def test_an_app_reads_and_writes_a_channel_it_joined(raw_app, apps_admin):
    channel_id = _joined_channel(apps_admin, raw_app)
    with apps_admin.client() as client:
        human = post(client, channel_id, "hello app")
    listed = raw_app.request("list_messages", channel_id=channel_id)
    written = raw_app.request("create_message", channel_id=channel_id, content="hello human")
    assert listed["status"] == 200
    assert [m["id"] for m in listed["data"]["messages"]] == [human["id"]]
    assert written["status"] == 200
    assert written["data"]["app_id"] == raw_app.installed["id"]


def test_an_app_cannot_read_a_channel_it_was_not_invited_into(raw_app, apps_admin):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        post(client, channel_id, "secret")
    read = raw_app.request("list_messages", channel_id=channel_id)
    written = raw_app.request("create_message", channel_id=channel_id, content="hi")
    assert read == {"status": 403, "error": "Invite the app into this channel first"}
    assert written["status"] == 403


def test_list_channels_names_only_joined_channels(raw_app, apps_admin):
    joined = _joined_channel(apps_admin, raw_app)
    with apps_admin.client() as client:
        new_channel(client)
    listed = raw_app.request("list_channels")
    assert listed["status"] == 200
    assert [channel["id"] for channel in listed["data"]] == [joined]


def test_an_app_cannot_edit_messages_it_did_not_write(raw_app, apps_admin):
    channel_id = _joined_channel(apps_admin, raw_app)
    with apps_admin.client() as client:
        human = post(client, channel_id, "mine")
    edited = raw_app.request(
        "update_message", message_id=human["id"], content="hijacked", etag=str(human["updated_at"])
    )
    assert edited == {"status": 404, "error": "App message not found"}


def test_an_edit_with_a_stale_etag_is_refused(raw_app, apps_admin):
    channel_id = _joined_channel(apps_admin, raw_app)
    first = raw_app.request("create_message", channel_id=channel_id, content="v1")["data"]
    etag = str(first["updated_at"])
    second = raw_app.request("update_message", message_id=first["id"], content="v2", etag=etag)
    stale = raw_app.request("update_message", message_id=first["id"], content="v3", etag=etag)
    missing = raw_app.request("update_message", message_id=first["id"], content="v4")
    assert second["status"] == 200
    assert stale == {"status": 412, "error": "Message changed"}
    assert missing == {"status": 400, "error": "If-Match is required"}


def test_a_reply_to_a_message_from_another_channel_is_refused(raw_app, apps_admin):
    joined = _joined_channel(apps_admin, raw_app)
    with apps_admin.client() as client:
        foreign = post(client, new_channel(client), "elsewhere")
    for field in ("reply_to_id", "parent_id"):
        created = raw_app.request(
            "create_message", channel_id=joined, content="x", **{field: foreign["id"]}
        )
        assert created == {"status": 400, "error": "Invalid message reference"}, field


def test_history_refuses_a_cursor_from_another_channel(raw_app, apps_admin):
    joined = _joined_channel(apps_admin, raw_app)
    with apps_admin.client() as client:
        foreign = post(client, new_channel(client), "elsewhere")
    paged = raw_app.request("list_messages", channel_id=joined, before=foreign["id"])
    assert paged == {"status": 400, "error": "Invalid message cursor"}


def test_a_forged_execution_token_is_refused(raw_app):
    forged = raw_app.request("authorize", token="e30.AAAA")
    assert forged == {"status": 403, "error": "Invalid or expired app context"}


def test_an_event_token_verifies_only_while_the_app_is_in_the_channel(raw_app, apps_admin):
    channel_id = _joined_channel(apps_admin, raw_app)
    with apps_admin.client() as client:
        posted = post(client, channel_id, "seen by the app")
    event = _event_for(raw_app, posted["id"])
    verified = raw_app.request("verify_event", token=event["event_token"])
    with apps_admin.client() as client:
        client.delete(f"/api/v1/channels/{channel_id}/apps/{raw_app.installed['id']}")
    after_leaving = raw_app.request("verify_event", token=event["event_token"])
    assert verified["status"] == 200
    assert verified["data"]["message"]["id"] == posted["id"]
    assert after_leaving["status"] == 403


def test_an_event_token_of_one_installation_does_not_verify_for_another(apps_admin):
    with serving_raw_app() as first, serving_raw_app() as second:
        with apps_admin.client() as client:
            for raw in (first, second):
                raw.installed = install_raw_app(client, raw)
                assert connect(client, raw.installed["id"]).status_code == 200
                raw.wait_until_ready()
            channel_id = new_channel(client)
            invite(client, channel_id, first.installed["id"])
            invite(client, channel_id, second.installed["id"])
            posted = post(client, channel_id, "seen by both")
        token = _event_for(first, posted["id"])["event_token"]
        own = first.request("verify_event", token=token)
        replayed = second.request("verify_event", token=token)
    assert own["status"] == 200
    assert replayed["status"] == 403


def test_scopes_limit_the_runtime(apps_admin):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw, scopes=["channels:mentions:read"])
            assert connect(client, raw.installed["id"]).status_code == 200
            raw.wait_until_ready()
            channel_id = new_channel(client)
            invite(client, channel_id, raw.installed["id"])
        answers = {
            "list_channels": raw.request("list_channels"),
            "list_messages": raw.request("list_messages", channel_id=channel_id),
            "create_message": raw.request("create_message", channel_id=channel_id, content="x"),
        }
    for operation, answer in answers.items():
        assert answer == {"status": 403, "error": "App scope was not granted"}, operation


def test_scopes_granted_later_take_effect_without_reconnecting(apps_admin):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            client.patch(
                f"/api/v1/apps/{raw.installed['id']}",
                json={"granted_scopes": ["channels:mentions:read"]},
            )
            assert connect(client, raw.installed["id"]).status_code == 200
            raw.wait_until_ready()
            before = raw.request("list_channels")
            client.patch(f"/api/v1/apps/{raw.installed['id']}", json={"granted_scopes": ALL_SCOPES})
            after = raw.request("list_channels")
    assert before["status"] == 403
    assert after["status"] == 200


def _runtime_after(raw, change, timeout: float = 20.0):
    """The first answer to `get_me` that is not a success once `change` ran, or "disconnected"."""
    change()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            answer = raw.request("get_me")
        except Exception:
            return "disconnected"
        if answer["status"] != 200:
            return answer
        time.sleep(0.5)
    raise AssertionError(f"the app kept its runtime for {timeout}s")


def test_a_disabled_app_loses_its_runtime(raw_app, apps_admin):
    def disable():
        with apps_admin.client() as client:
            client.patch(f"/api/v1/apps/{raw_app.installed['id']}", json={"is_active": False})

    answer = _runtime_after(raw_app, disable)
    assert answer == "disconnected" or answer["status"] == 403, answer


def test_turning_apps_off_cuts_every_connection(raw_app, apps_admin, apps_instance, preserve):
    preserve("admin_config", on=apps_instance)

    def apps_off():
        with apps_admin.client() as client:
            config = client.get("/api/v1/auths/admin/config").json()
            client.post("/api/v1/auths/admin/config", json={**config, "ENABLE_APPS": False})

    answer = _runtime_after(raw_app, apps_off)
    assert answer == "disconnected" or answer["status"] in (403, 404), answer


def test_disabling_channels_cuts_channel_listing(apps_admin, apps_instance, preserve):
    preserve("admin_config", on=apps_instance)
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            assert connect(client, raw.installed["id"]).status_code == 200
            raw.wait_until_ready()
            config = client.get("/api/v1/auths/admin/config").json()
            client.post("/api/v1/auths/admin/config", json={**config, "ENABLE_CHANNELS": False})
        listed = raw.request("list_channels")
        me = raw.request("get_me")
    assert listed == {"status": 403, "error": "Channels are disabled"}
    assert me["status"] == 200, "the rest of the runtime stays up"


def test_nothing_but_a_ping_is_served_before_the_connection_is_ready(apps_admin):
    with serving_raw_app() as raw:
        raw.before_ready = ["get_me", "list_channels"]
        with apps_admin.client() as client:
            raw.installed = install_raw_app(client, raw)
            assert connect(client, raw.installed["id"]).status_code == 200
        raw.wait_until_ready()
        after_ready = raw.request("get_me")
    assert [answer["status"] for answer in raw.before_ready_answers] == [503, 503]
    assert after_ready["status"] == 200


def test_the_runtime_has_no_http_route_left(apps_instance):
    with httpx.Client(base_url=apps_instance.base_url, timeout=30) as client:
        schema = client.get("/openapi.json").json()
    runtime_paths = [path for path in schema["paths"] if path.startswith("/api/v1/apps/runtime")]
    assert runtime_paths == []
