"""Journey: an app outliving restarts on either side, posting unprompted, and serving two instances.

Open WebUI holds the connection: it opens a Socket.IO connection to every active app, keeps it
alive, and reopens it with the saved reconnect credential after either side restarts, so the
installation keeps its identity. The app posts unprompted over that connection, addressing the
installation it saved, and one app process with one key serves installations on two instances.

Discriminates: mutation, `AppConnections.reconcile` never starting a connection for an active
app, turns test_an_app_keeps_working_after_open_webui_restarts red. A dropped saved-installation
check is caught by test_a_reconnect_claiming_another_installation_is_refused in
test_app_connection.py instead.
"""

from __future__ import annotations

import time

import httpx
import pytest

from harness.actors import admin_of
from harness.apps import (
    APPS_ENV,
    installed,
    invite,
    new_channel,
    post,
    serving_reference_app,
    wait_for_message,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source, pytest.mark.slow]

# Open WebUI rechecks its app connections every 5 s and backs off up to 30 s between attempts
RECONNECT_SECONDS = 45


def _push(app, installation_id: str, channel_id: str, content: str) -> httpx.Response:
    return httpx.post(
        app.origin + "/proactive",
        json={"installation_id": installation_id, "channel_id": channel_id, "content": content},
        timeout=30,
    )


def _push_until_delivered(app, installation_id: str, channel_id: str, content: str) -> None:
    deadline = time.monotonic() + RECONNECT_SECONDS
    while time.monotonic() < deadline:
        pushed = _push(app, installation_id, channel_id, content)
        if pushed.status_code == 200:
            return
        assert pushed.json() == {"error": "ConnectionUnavailable"}, pushed.text
        time.sleep(1)
    raise AssertionError(f"Open WebUI never reconnected to the app within {RECONNECT_SECONDS}s")


def test_the_app_posts_unprompted_into_a_channel_it_joined(apps_admin):
    with serving_reference_app(server="fastapi") as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        pushed = _push(app, connected["data"]["installation_id"], channel_id, "good morning")
        assert pushed.status_code == 200, pushed.text
        with apps_admin.client() as client:
            message = wait_for_message(client, channel_id, "good morning")
    assert message["user"]["id"] == connected["id"]


def test_an_unknown_installation_is_refused_without_sending(apps_admin):
    with serving_reference_app(server="fastapi") as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        pushed = _push(app, "00000000-0000-4000-8000-000000000000", channel_id, "lost")
        with apps_admin.client() as client:
            contents = [
                m["content"] for m in client.get(f"/api/v1/channels/{channel_id}/messages").json()
            ]
    assert pushed.status_code == 503
    assert pushed.json() == {"error": "ConnectionUnavailable"}
    assert "lost" not in contents


def test_an_app_keeps_its_installation_across_its_own_restart(apps_admin):
    with serving_reference_app(server="fastapi") as first_run:
        with apps_admin.client() as client:
            connected = installed(client, first_run)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        saved = {
            "installation_id": connected["data"]["installation_id"],
            "api_key": first_run.api_key,
            "private_key": first_run.private_key,
            "port": int(first_run.url.split(":")[2].split("/")[0]),
            "manifest_id": first_run.manifest_id,
        }
    with serving_reference_app(
        server="fastapi",
        api_key=saved["api_key"],
        private_key=saved["private_key"],
        port=saved["port"],
        manifest_id=saved["manifest_id"],
    ) as second_run:
        _push_until_delivered(second_run, saved["installation_id"], channel_id, "back again")
        with apps_admin.client() as client:
            wait_for_message(client, channel_id, "back again")
            post(client, channel_id, "!echo still listening")
            wait_for_message(client, channel_id, "echo: still listening")
            after = client.get(f"/api/v1/apps/{connected['id']}").json()
        seen = second_run.wait_for("message.created", app_id=connected["id"])
    assert seen["installation_id"] == saved["installation_id"]
    assert after["data"]["installation_id"] == saved["installation_id"]


def test_an_app_keeps_working_after_open_webui_restarts(instance_with):
    restarting = instance_with(APPS_ENV)
    admin = admin_of(restarting)
    with serving_reference_app() as app:
        with admin.client() as client:
            connected = installed(client, app)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        restarting.restart()
        with admin.client() as client:
            listed = {entry["id"]: entry for entry in client.get("/api/v1/apps").json()}
            post(client, channel_id, "!echo after restart")
            wait_for_message(client, channel_id, "echo: after restart", timeout=RECONNECT_SECONDS)
    assert listed[connected["id"]]["is_active"] is True


def test_the_app_list_reports_whether_each_app_is_connected(apps_admin):
    with serving_reference_app() as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
            while_up = client.get(f"/api/v1/apps/{connected['id']}").json()
    deadline = time.monotonic() + 30
    with apps_admin.client() as client:
        while time.monotonic() < deadline:
            after = client.get(f"/api/v1/apps/{connected['id']}").json()
            if after["connection_status"] != "connected":
                break
            time.sleep(0.5)
    assert while_up["connection_status"] == "connected"
    assert after["connection_status"] in ("connecting", "disconnected")
    assert after["is_active"] is True, "a dropped connection does not switch the app off"


def test_one_app_serves_two_instances_independently(apps_instance, apps_admin, instance_with):
    other = instance_with({**APPS_ENV, "WEBUI_NAME": "Second"})
    other_admin = admin_of(other)
    with serving_reference_app() as app:
        with apps_admin.client() as client:
            here = installed(client, app)
            here_channel = new_channel(client)
            invite(client, here_channel, here["id"])
        with other_admin.client() as client:
            there = installed(client, app)
            there_channel = new_channel(client)
            invite(client, there_channel, there["id"])
        with apps_admin.client() as client:
            post(client, here_channel, "!echo on the first")
            wait_for_message(client, here_channel, "echo: on the first")
        with other_admin.client() as client:
            post(client, there_channel, "!echo on the second")
            wait_for_message(client, there_channel, "echo: on the second")
        seen_here = app.wait_for("message.created", app_id=here["id"])
        seen_there = app.wait_for("message.created", app_id=there["id"])
    assert seen_here["installation_id"] == here["data"]["installation_id"]
    assert seen_there["installation_id"] == there["data"]["installation_id"]
    assert seen_here["installation_id"] != seen_there["installation_id"]
