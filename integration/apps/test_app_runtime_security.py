"""Journey: the runtime API trusts only a live app's own signature, and only inside its channels.

The app's calls carry a 60-second EdDSA token signed by its private key, for this instance's
audience. These tests sign their own tokens with the reference app's key (or a wrong one) and
call the runtime API directly, as a misbehaving or stolen-credential app would.

Discriminates: mutation, `verify_app_token` not checking `aud`, turns
test_a_token_for_another_instance_is_refused red; `check_app_channel_membership` skipped in
`get_app_channel_messages` turns test_an_app_cannot_read_a_channel_it_was_not_invited_into red.
"""

from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from harness.apps import (
    answering_as_an_app,
    install_bare_app,
    installed,
    invite,
    new_channel,
    post,
    private_key_pem,
    unused_app_port,
)
from harness.listener import listening

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


def _token(app_id, private_pem, audience, lifetime=60, issued=None, typ="webui-app+jwt", **extra):
    now = issued if issued is not None else int(time.time())
    claims = {"sub": app_id, "aud": audience, "iat": now, "exp": now + lifetime, **extra}
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return jwt.encode(claims, key, algorithm="EdDSA", headers={"typ": typ})


def _runtime(instance, token, method="get", path="/me", headers=None, **kwargs):
    return httpx.request(
        method.upper(),
        f"{instance.base_url}/api/v1/apps/runtime{path}",
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
        timeout=30,
        **kwargs,
    )


def _audience(instance):
    return instance.base_url + "/api/v1/apps/runtime"


def test_a_correctly_signed_token_reads_the_installation(
    apps_instance, installed_app, reference_app
):
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    me = _runtime(apps_instance, token)
    assert me.status_code == 200, me.text
    assert me.json()["id"] == installed_app["id"]
    assert reference_app.api_key not in me.text


def test_a_token_signed_by_another_key_is_refused(apps_instance, installed_app):
    token = _token(installed_app["id"], private_key_pem(), _audience(apps_instance))
    assert _runtime(apps_instance, token).status_code == 401


def test_a_token_for_another_instance_is_refused(apps_instance, installed_app, reference_app):
    token = _token(
        installed_app["id"],
        reference_app.private_key,
        "https://elsewhere.example/api/v1/apps/runtime",
    )
    assert _runtime(apps_instance, token).status_code == 401


@pytest.mark.parametrize(
    "shape",
    [
        {"lifetime": 61},
        {"lifetime": 0},
        {"issued": int(time.time()) - 600},
        {"typ": "JWT"},
        {"typ": "webui-app-connect+jwt"},
    ],
    ids=["too-long", "zero-lifetime", "expired", "plain-jwt", "connect-proof"],
)
def test_badly_shaped_tokens_are_refused(apps_instance, installed_app, reference_app, shape):
    token = _token(
        installed_app["id"], reference_app.private_key, _audience(apps_instance), **shape
    )
    assert _runtime(apps_instance, token).status_code == 401


def test_a_token_naming_another_installation_is_refused(apps_instance, apps_admin, reference_app):
    with apps_admin.client() as client:
        first = installed(client, reference_app)
        second = installed(client, reference_app)
    # same key signs both installs, but each token is bound to its own subject
    token = _token(first["id"], reference_app.private_key, _audience(apps_instance))
    me = _runtime(apps_instance, token)
    assert me.json()["id"] == first["id"] != second["id"]


def test_missing_or_garbage_credentials_are_refused(apps_instance):
    for header in ("", "Bearer x", "Bearer not.a.jwt", "Basic abc"):
        response = httpx.get(
            f"{apps_instance.base_url}/api/v1/apps/runtime/me",
            headers={"Authorization": header} if header else {},
            timeout=30,
        )
        assert response.status_code == 401, header


def test_a_user_session_token_is_not_an_app_credential(apps_instance, apps_admin):
    assert _runtime(apps_instance, apps_admin.token).status_code == 401


def test_a_disabled_app_loses_runtime_access(
    apps_instance, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"is_active": False})
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    assert _runtime(apps_instance, token).status_code == 401


def test_an_app_cannot_read_a_channel_it_was_not_invited_into(
    apps_instance, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        post(client, channel_id, "secret")
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    read = _runtime(apps_instance, token, path=f"/channels/{channel_id}/messages")
    written = _runtime(
        apps_instance, token, "post", f"/channels/{channel_id}/messages", json={"content": "hi"}
    )
    assert read.status_code == 403
    assert read.json()["detail"] == "Invite the app into this channel first"
    assert written.status_code == 403


def test_an_app_cannot_edit_messages_it_did_not_write(
    apps_instance, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        invite(client, channel_id, installed_app["id"])
        human = post(client, channel_id, "mine")
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    edited = _runtime(
        apps_instance,
        token,
        "patch",
        f"/messages/{human['id']}",
        json={"content": "hijacked"},
        headers={"If-Match": f'"{human["updated_at"]}"'},
    )
    assert edited.status_code == 404


def test_an_edit_without_if_match_is_refused(
    apps_instance, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        invite(client, channel_id, installed_app["id"])
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    created = _runtime(
        apps_instance, token, "post", f"/channels/{channel_id}/messages", json={"content": "v1"}
    )
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    edited = _runtime(
        apps_instance, token, "patch", f"/messages/{created.json()['id']}", json={"content": "v2"}
    )
    assert created.status_code == 201, created.text
    assert edited.status_code == 428


def test_a_reply_to_a_message_from_another_channel_is_refused(
    apps_instance, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        joined = new_channel(client)
        invite(client, joined, installed_app["id"])
        foreign = post(client, new_channel(client), "elsewhere")
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    for field in ("reply_to_id", "parent_id"):
        token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
        created = _runtime(
            apps_instance,
            token,
            "post",
            f"/channels/{joined}/messages",
            json={"content": "x", field: foreign["id"]},
        )
        assert created.status_code == 400, field
        assert created.json()["detail"] == "Invalid message reference"


def test_history_refuses_a_cursor_from_another_channel(
    apps_instance, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        joined = new_channel(client)
        invite(client, joined, installed_app["id"])
        foreign = post(client, new_channel(client), "elsewhere")
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    paged = _runtime(
        apps_instance, token, path=f"/channels/{joined}/messages", params={"before": foreign["id"]}
    )
    assert paged.status_code == 400


def test_a_forged_execution_token_is_refused(apps_instance, installed_app, reference_app):
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    forged = _runtime(apps_instance, token, "post", "/authorize", json={"token": "e30.AAAA"})
    assert forged.status_code == 403
    assert forged.json()["detail"] == "Invalid or expired app context"


def test_an_event_token_verifies_only_for_the_installation_it_was_sent_to(
    apps_instance, apps_admin, installed_app, reference_app
):
    private_pem = private_key_pem()
    with listening(port=unused_app_port()) as door:
        door.route("POST", "/events", answering_as_an_app(private_pem))
        door.route("GET", "/events", lambda _request: (401, {}, b""))
        with apps_admin.client() as client:
            recorder = install_bare_app(client, door, private_pem)
            channel_id = new_channel(client)
            invite(client, channel_id, recorder["id"])
            invite(client, channel_id, installed_app["id"])
            posted = post(client, channel_id, "seen by both")
        envelope = _wait_for_envelope(door, posted["id"])
    own = _runtime(
        apps_instance,
        _token(recorder["id"], private_pem, _audience(apps_instance)),
        "post",
        "/events/verify",
        json={"token": envelope["event_token"]},
    )
    replayed = _runtime(
        apps_instance,
        _token(installed_app["id"], reference_app.private_key, _audience(apps_instance)),
        "post",
        "/events/verify",
        json={"token": envelope["event_token"]},
    )
    assert own.status_code == 200, own.text
    assert own.json()["message"]["id"] == posted["id"]
    assert replayed.status_code == 403


def test_an_event_token_stops_verifying_once_the_app_leaves_the_channel(apps_instance, apps_admin):
    private_pem = private_key_pem()
    with listening(port=unused_app_port()) as door:
        door.route("POST", "/events", answering_as_an_app(private_pem))
        with apps_admin.client() as client:
            recorder = install_bare_app(client, door, private_pem)
            channel_id = new_channel(client)
            invite(client, channel_id, recorder["id"])
            posted = post(client, channel_id, "before leaving")
            envelope = _wait_for_envelope(door, posted["id"])
            client.delete(f"/api/v1/channels/{channel_id}/apps/{recorder['id']}")
    verified = _runtime(
        apps_instance,
        _token(recorder["id"], private_pem, _audience(apps_instance)),
        "post",
        "/events/verify",
        json={"token": envelope["event_token"]},
    )
    assert verified.status_code == 403


def test_disabling_apps_cuts_runtime_access(
    apps_instance, apps_admin, installed_app, reference_app, preserve
):
    preserve("admin_config", on=apps_instance)
    with apps_admin.client() as client:
        config = client.get("/api/v1/auths/admin/config").json()
        client.post("/api/v1/auths/admin/config", json={**config, "ENABLE_APPS": False})
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    assert _runtime(apps_instance, token).status_code == 404


def test_disabling_channels_cuts_channel_listing(
    apps_instance, apps_admin, installed_app, reference_app, preserve
):
    preserve("admin_config", on=apps_instance)
    with apps_admin.client() as client:
        config = client.get("/api/v1/auths/admin/config").json()
        client.post("/api/v1/auths/admin/config", json={**config, "ENABLE_CHANNELS": False})
    token = _token(installed_app["id"], reference_app.private_key, _audience(apps_instance))
    listed = _runtime(apps_instance, token, path="/channels")
    assert listed.status_code == 403
    assert listed.json()["detail"] == "Channels are disabled"


def _wait_for_envelope(door, message_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for request in door.requests_to("/events"):
            body = request.json()
            if (body.get("message") or {}).get("id") == message_id:
                return body
        time.sleep(0.1)
    raise AssertionError(f"the recorder never received message {message_id}")
