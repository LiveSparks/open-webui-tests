"""Journey: an app outliving restarts on either side, posting unprompted, and serving two instances.

The app keeps no session with Open WebUI: every call is freshly signed, so it posts proactively
after its own restart from a saved destination, keeps working after Open WebUI restarts, and one
app process with one key serves installations on two separate instances.

Discriminates: mutation, `get_app_encryption_cipher` deriving from a per-boot random key, turns
test_an_app_keeps_working_after_open_webui_restarts red. The two-instance test guards the
SDK's per-event destination binding, which no backend mutation reaches.
"""

from __future__ import annotations

import httpx
import pytest

from harness.actors import admin_of
from harness.apps import (
    APPS_ENV,
    connect,
    install,
    installed,
    invite,
    new_channel,
    post,
    serving_reference_app,
    wait_for_message,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source, pytest.mark.slow]


def test_the_app_posts_unprompted_into_a_channel_it_joined(apps_instance, apps_admin):
    with serving_reference_app([apps_instance.base_url], server="fastapi") as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        pushed = httpx.post(
            app.origin + "/proactive",
            json={
                "webui_url": apps_instance.base_url,
                "app_id": connected["id"],
                "channel_id": channel_id,
                "content": "good morning",
            },
            timeout=30,
        )
        assert pushed.status_code == 200, pushed.text
        with apps_admin.client() as client:
            message = wait_for_message(client, channel_id, "good morning")
    assert message["user"]["id"] == connected["id"]


def test_the_app_posts_after_its_own_restart_from_a_saved_destination(apps_instance, apps_admin):
    with serving_reference_app([apps_instance.base_url], server="fastapi") as first_run:
        with apps_admin.client() as client:
            connected = installed(client, first_run)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        saved = {
            "webui_url": apps_instance.base_url,
            "app_id": connected["id"],
            "channel_id": channel_id,
            "api_key": first_run.api_key,
            "private_key": first_run.private_key,
            "port": int(first_run.url.split(":")[2].split("/")[0]),
            "manifest_id": first_run.manifest_id,
        }
    with serving_reference_app(
        [apps_instance.base_url],
        server="fastapi",
        api_key=saved["api_key"],
        private_key=saved["private_key"],
        port=saved["port"],
        manifest_id=saved["manifest_id"],
    ) as second_run:
        pushed = httpx.post(
            second_run.origin + "/proactive",
            json={**saved, "content": "back again"},
            timeout=30,
        )
        assert pushed.status_code == 200, pushed.text
        with apps_admin.client() as client:
            post(client, channel_id, "!echo still listening")
            wait_for_message(client, channel_id, "echo: still listening")


def test_an_app_keeps_working_after_open_webui_restarts(instance_with):
    restarting = instance_with({**APPS_ENV, "ENABLE_APPS": "true"})
    admin = admin_of(restarting)
    with serving_reference_app([restarting.base_url]) as app:
        with admin.client() as client:
            connected = installed(client, app)
            channel_id = new_channel(client)
            invite(client, channel_id, connected["id"])
        restarting.restart()
        with admin.client() as client:
            listed = {entry["id"]: entry for entry in client.get("/api/v1/apps").json()}
            post(client, channel_id, "!echo after restart")
            wait_for_message(client, channel_id, "echo: after restart")
    assert listed[connected["id"]]["is_active"] is True


def test_one_app_serves_two_instances_independently(apps_instance, apps_admin, instance_with):
    other = instance_with({**APPS_ENV, "WEBUI_NAME": "Second"})
    other_admin = admin_of(other)
    with serving_reference_app([apps_instance.base_url, other.base_url]) as app:
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
    assert seen_here["webui_url"] == apps_instance.base_url
    assert seen_there["webui_url"] == other.base_url


def test_an_app_allowing_only_other_instances_refuses_to_connect(apps_instance, apps_admin):
    with serving_reference_app(["https://elsewhere.example"]) as app:
        with apps_admin.client() as client:
            created = install(client, app)
            connected = connect(client, created.json()["id"])
            after = client.get(f"/api/v1/apps/{created.json()['id']}").json()
    assert connected.status_code == 502
    assert "refused the connection" in connected.json()["detail"]
    assert after["is_active"] is False
