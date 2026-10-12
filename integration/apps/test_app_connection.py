"""Journey: an admin adds an external app, Open WebUI checks its identity and the app goes live.

Adding an app is two calls from the admin's Add app form: Continue reads the app's manifest with
its API key, Connect stores the app and opens a Socket.IO connection to it. Open WebUI sends a
fresh challenge; the app answers with a proof signed by its private key, naming the installation
and a reconnect credential. Only a proof that verifies against the pinned public key and names
this challenge switches the app on, after a ping in each direction.

Discriminates: mutation, `verify_connection` skipping the challenge comparison, turns
test_a_proof_for_another_challenge_is_refused red; dropping the HTTPS check in
`validate_app_url` turns test_a_plain_http_app_off_the_allowlist_is_refused red; dropping the
saved-installation comparison turns test_a_reconnect_claiming_another_installation_is_refused red.
"""

from __future__ import annotations

import pytest

from harness.apps import (
    ALL_SCOPES,
    connect,
    install,
    install_raw_app,
    installed,
    preview,
    private_key_pem,
    serving_raw_app,
    serving_reference_app,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]
# every failed connection names all three suspects at once
GENERIC_FAILURE = (
    "App connection failed. Check the API key, pinned public key and Socket.IO endpoint."
)


def test_continue_reads_the_manifest_the_app_serves(apps_admin, reference_app):
    with apps_admin.client() as client:
        previewed = preview(client, reference_app)
    assert previewed.status_code == 200, previewed.text
    manifest = previewed.json()
    assert manifest["id"] == reference_app.manifest_id
    assert manifest["name"] == "Reference App"
    assert manifest["requested_scopes"] == ALL_SCOPES
    assert manifest["public_key"].strip() == reference_app.public_key.strip()


def test_continue_with_a_wrong_api_key_says_the_key_was_rejected(apps_admin, reference_app):
    with apps_admin.client() as client:
        previewed = preview(client, reference_app, api_key="not-the-key")
    assert previewed.status_code == 400
    assert "rejected the API key" in previewed.json()["detail"]


def test_continue_without_an_api_key_is_refused(apps_admin, reference_app):
    with apps_admin.client() as client:
        previewed = client.post("/api/v1/apps/manifest/preview", json={"url": reference_app.url})
    assert previewed.status_code == 400
    assert "API key is required" in previewed.json()["detail"]


def test_continue_against_a_post_only_app_points_to_manual_details(apps_instance, apps_admin):
    with serving_reference_app(manifest=False) as app:
        with apps_admin.client() as client:
            previewed = preview(client, app)
    assert previewed.status_code == 502
    assert "enter details manually" in previewed.json()["detail"]


def test_continue_against_nothing_listening_says_it_cannot_reach_the_app(apps_admin):
    with apps_admin.client() as client:
        previewed = client.post(
            "/api/v1/apps/manifest/preview",
            json={"url": "http://127.0.0.1:9/events", "api_key": "k" * 32},
        )
    assert previewed.status_code == 502
    assert "HTTPS" in previewed.json()["detail"]


def test_connect_switches_the_app_on_and_keeps_the_key_secret(apps_admin, reference_app):
    with apps_admin.client() as client:
        created = install(client, reference_app)
        assert created.status_code == 200, created.text
        app = created.json()
        assert app["is_active"] is False
        assert app["configured"] is True
        connected = connect(client, app["id"])
        listed = client.get("/api/v1/apps").json()
        details = client.get(f"/api/v1/apps/{app['id']}").json()
    assert connected.status_code == 200, connected.text
    assert connected.json()["is_active"] is True
    assert connected.json()["connection_status"] == "connected"
    assert "connection_token" not in connected.text
    assert connected.json()["data"]["installation_id"]
    for body in (created.text, connected.text, str(listed), str(details)):
        assert reference_app.api_key not in body
    assert any(entry["id"] == app["id"] and entry["is_active"] for entry in listed)


def test_a_manual_install_of_a_post_only_app_connects(apps_instance, apps_admin):
    with serving_reference_app(manifest=False) as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
    assert connected["is_active"] is True


def test_the_fastapi_mount_connects_like_the_builtin_server(apps_instance, apps_admin):
    with serving_reference_app(server="fastapi", path="/hooks/owui") as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
    assert connected["is_active"] is True
    assert connected["data"]["url"].endswith("/hooks/owui")


def test_a_proof_for_another_challenge_is_refused(apps_admin):
    with serving_raw_app() as raw:
        raw.answer_connect = lambda data: raw.signed_proof(data, challenge="x" * 36)
        with apps_admin.client() as client:
            created = install_raw_app(client, raw)
            connected = connect(client, created["id"])
            after = client.get(f"/api/v1/apps/{created['id']}").json()
    assert raw.connects, "the instance never opened the connection"
    assert connected.status_code == 502
    assert after["is_active"] is False


def test_an_honest_proof_from_the_scripted_app_connects(apps_admin):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            created = install_raw_app(client, raw)
            connected = connect(client, created["id"])
        raw.wait_until_ready()
    assert connected.status_code == 200, connected.text
    assert connected.json()["data"]["installation_id"] == raw.installation_id
    assert raw.connects[0]["connection_token"] is None, "a first connection has no credential yet"


@pytest.mark.parametrize(
    "forged",
    [
        {"manifest_id": "someone-else"},
        {"app_id": "another-installation"},
        {"aud": "oix:connection"},
        {"exp": 4102444800},
        {"token_hash": "0" * 64},
    ],
    ids=["manifest", "app-id", "audience", "lifetime", "token-hash"],
)
def test_a_proof_with_a_forged_claim_is_refused(apps_admin, forged):
    with serving_raw_app() as raw:
        raw.answer_connect = lambda data: raw.signed_proof(data, **forged)
        with apps_admin.client() as client:
            created = install_raw_app(client, raw)
            connected = connect(client, created["id"])
    assert connected.status_code == 502, connected.text


def test_an_app_that_refuses_the_handshake_is_reported(apps_admin):
    with serving_raw_app() as raw:
        raw.answer_connect = lambda data: {"status": 403, "error": "no"}
        with apps_admin.client() as client:
            created = install_raw_app(client, raw)
            connected = connect(client, created["id"])
    assert connected.status_code == 403
    assert "rejected its connection credential" in connected.json()["detail"]


def test_a_reconnect_presents_the_saved_credential_and_keeps_the_installation(apps_admin):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            created = install_raw_app(client, raw)
            first = connect(client, created["id"]).json()
            client.patch(f"/api/v1/apps/{created['id']}", json={"is_active": False})
            again = connect(client, created["id"])
    assert again.status_code == 200, again.text
    assert raw.connects[-1]["connection_token"] == raw.connects[0]["connection_token"] or (
        raw.connects[-1]["connection_token"] is not None
    )
    assert again.json()["data"]["installation_id"] == first["data"]["installation_id"]


def test_a_reconnect_claiming_another_installation_is_refused(apps_admin):
    with serving_raw_app() as raw:
        with apps_admin.client() as client:
            created = install_raw_app(client, raw)
            assert connect(client, created["id"]).status_code == 200
            client.patch(f"/api/v1/apps/{created['id']}", json={"is_active": False})
            raw.installation_id = "00000000-0000-4000-8000-000000000000"
            again = connect(client, created["id"])
            after = client.get(f"/api/v1/apps/{created['id']}").json()
    assert again.status_code == 502
    assert after["is_active"] is False


def test_a_proof_signed_by_another_key_is_refused(apps_admin, reference_app):
    # an installed public key that is not the app's: the app's proof cannot verify
    manifest = reference_app.manifest(public_key=_other_public_key())
    with apps_admin.client() as client:
        created = install(client, reference_app, manifest=manifest)
        connected = connect(client, created.json()["id"])
        after = client.get(f"/api/v1/apps/{created.json()['id']}").json()
    assert connected.status_code == 502
    assert connected.json()["detail"] == GENERIC_FAILURE
    assert after["is_active"] is False


def test_connect_with_a_wrong_stored_key_says_the_app_rejected_it(apps_admin, reference_app):
    with apps_admin.client() as client:
        created = install(client, reference_app)
        connected = connect(client, created.json()["id"], api_key="wrong-" + "k" * 32)
        after = client.get(f"/api/v1/apps/{created.json()['id']}").json()
    assert connected.status_code == 502
    assert connected.json()["detail"] == GENERIC_FAILURE
    assert after["is_active"] is False


def test_reconnecting_with_the_right_key_after_a_failure_goes_live(apps_admin, reference_app):
    with apps_admin.client() as client:
        created = install(client, reference_app)
        connect(client, created.json()["id"], api_key="wrong-" + "k" * 32)
        fixed = connect(client, created.json()["id"], api_key=reference_app.api_key)
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["is_active"] is True


def test_connect_to_an_app_that_went_away_reports_it_unreachable(apps_instance, apps_admin):
    with serving_reference_app() as app:
        with apps_admin.client() as client:
            created = install(client, app)
    with apps_admin.client() as client:
        connected = connect(client, created.json()["id"])
    assert connected.status_code == 502
    assert connected.json()["detail"] == GENERIC_FAILURE


def test_a_plain_http_app_off_the_allowlist_is_refused(apps_admin, reference_app):
    # the same app behind a loopback name the instance was not told about
    moved_url = reference_app.url.replace("127.0.0.1", "localhost")
    with apps_admin.client() as client:
        previewed = client.post(
            "/api/v1/apps/manifest/preview",
            json={"url": moved_url, "api_key": reference_app.api_key},
        )
        created = install(client, reference_app, manifest=None)
        created_elsewhere = client.post(
            "/api/v1/apps",
            json={
                "manifest": reference_app.manifest(),
                "url": moved_url,
                "api_key": reference_app.api_key,
                "slug": "off-the-allowlist",
                "granted_scopes": [],
            },
        )
    assert previewed.status_code == 502
    assert "HTTPS" in previewed.json()["detail"]
    assert created.status_code == 200
    assert created_elsewhere.status_code == 400
    assert created_elsewhere.json()["detail"] == "Invalid app URL"


def test_an_install_refuses_scopes_the_app_never_requested(apps_instance, apps_admin):
    with serving_reference_app(scopes=["channels:mentions:read"]) as app:
        with apps_admin.client() as client:
            created = install(client, app, scopes=["channels:mentions:read", "channels:read"])
    assert created.status_code == 400
    assert created.json()["detail"] == "Unrequested scope"


def test_a_second_install_with_the_same_handle_is_a_conflict(apps_admin, reference_app):
    with apps_admin.client() as client:
        first = install(client, reference_app, slug="same-handle-app")
        second = install(client, reference_app, slug="same-handle-app")
    assert first.status_code == 200, first.text
    assert second.status_code == 409


@pytest.mark.parametrize("slug", ["Upper", "1digit", "under_score", "x" * 41, ""])
def test_an_invalid_handle_is_refused(apps_admin, reference_app, slug):
    with apps_admin.client() as client:
        created = install(client, reference_app, slug=slug)
    assert created.status_code == 422


def test_a_manifest_with_a_non_ed25519_key_is_refused(apps_admin, reference_app):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    p256 = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    with apps_admin.client() as client:
        created = install(client, reference_app, manifest=reference_app.manifest(public_key=p256))
    assert created.status_code == 422


def test_one_app_serves_two_installations_with_their_own_ids(apps_admin, reference_app):
    with apps_admin.client() as client:
        first = installed(client, reference_app)
        second = installed(client, reference_app)
    assert first["id"] != second["id"]
    assert first["is_active"] and second["is_active"]


def _other_public_key() -> str:
    from harness.apps import public_key_pem

    return public_key_pem(private_key_pem())
