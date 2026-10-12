"""Journey: an app's picture reaches Open WebUI, and replicas behind Redis share one installation.

oix 0.5 embeds a local image file in the manifest as a data URL, which Open WebUI stores with the
app and shows wherever the app appears. An app with several replicas shares a Redis: a replica
that holds no connection to an installation routes its runtime call through the replica that
does, so a background worker can post through whichever replica Open WebUI happened to reach.

Discriminates: mutation, `Manifest.validate_profile_image_url` accepting any data URL, turns
test_a_profile_image_that_is_not_an_image_is_refused red. The replica test guards the SDK's
Redis routing, which no backend mutation reaches: with the replicas on separate Redis servers
it fails with ConnectionUnavailable.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from harness.apps import (
    install,
    installed,
    invite,
    new_channel,
    private_key_pem,
    serving_reference_app,
    wait_for_message,
)
from harness.backends import redis_server
from harness.image_engines import PNG_BASE64

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


def test_the_app_picture_is_stored_with_its_manifest(apps_admin, tmp_path):
    picture = tmp_path / "avatar.png"
    picture.write_bytes(base64.b64decode(PNG_BASE64))
    with serving_reference_app(profile_image=picture) as app:
        with apps_admin.client() as client:
            previewed = client.post(
                "/api/v1/apps/manifest/preview", json={"url": app.url, "api_key": app.api_key}
            ).json()
            connected = installed(client, app, manifest=previewed)
            listed = {entry["id"]: entry for entry in client.get("/api/v1/apps").json()}
    expected = "data:image/png;base64," + PNG_BASE64
    assert previewed["profile_image_url"] == expected
    assert listed[connected["id"]]["data"]["manifest"]["profile_image_url"] == expected


@pytest.mark.parametrize(
    "picture",
    ["data:text/html;base64,PGgxPg==", "data:image/png;base64,", "https://u:p@example.com/a.png"],
    ids=["not-an-image", "empty", "credentials"],
)
def test_a_profile_image_that_is_not_an_image_is_refused(apps_admin, reference_app, picture):
    with apps_admin.client() as client:
        created = install(
            client, reference_app, manifest=reference_app.manifest(profile_image_url=picture)
        )
    assert created.status_code == 422


def test_a_replica_without_the_connection_routes_through_the_one_with_it(apps_admin):
    with redis_server() as redis_url:
        shared = {"api_key": "replica-key-" + "r" * 32, "private_key": private_key_pem()}
        with serving_reference_app(
            server="fastapi", manifest_id="replicated", redis_url=redis_url, **shared
        ) as connected_replica:
            with apps_admin.client() as client:
                app = installed(client, connected_replica)
                channel_id = new_channel(client)
                invite(client, channel_id, app["id"])
            with serving_reference_app(
                server="fastapi", manifest_id="replicated", redis_url=redis_url, **shared
            ) as idle_replica:
                pushed = httpx.post(
                    idle_replica.origin + "/proactive",
                    json={
                        "installation_id": app["data"]["installation_id"],
                        "channel_id": channel_id,
                        "content": "sent from the idle replica",
                    },
                    timeout=30,
                )
                assert pushed.status_code == 200, pushed.text
                with apps_admin.client() as client:
                    message = wait_for_message(client, channel_id, "sent from the idle replica")
    assert message["user"]["id"] == app["id"]
