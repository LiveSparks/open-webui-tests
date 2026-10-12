"""Journey: the admin's Apps settings after an app is connected, and who may see or manage apps.

Covers listing, the detail view with its access grants, renaming, scope changes, disabling,
uninstalling, a manifest update that needs a fresh approval, and the Apps feature switch.

Discriminates: mutation, `update_app_by_id` keeping `is_active` on a manifest change, turns
test_an_updated_manifest_switches_the_app_off_until_reconnected red; `get_apps` listing inactive
apps to users turns test_users_only_see_active_apps_they_may_use red.
"""

from __future__ import annotations

import pytest

from harness.apps import (
    ADMIN_CONFIG,
    connect,
    install,
    installed,
    invite,
    new_channel,
    preview,
)

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]

EVERYONE = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]


def _app(client, app_id):
    return client.get(f"/api/v1/apps/{app_id}")


def test_the_detail_view_shows_the_default_grant_for_everyone(apps_admin, installed_app):
    with apps_admin.client() as client:
        details = _app(client, installed_app["id"])
    assert details.status_code == 200
    assert details.json()["access_grants"] == EVERYONE
    assert details.json()["data"]["granted_scopes"] == installed_app["data"]["granted_scopes"]


def test_renaming_an_app_keeps_it_connected(apps_admin, installed_app):
    with apps_admin.client() as client:
        renamed = client.patch(f"/api/v1/apps/{installed_app['id']}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    assert renamed.json()["is_active"] is True


def test_settings_are_stored_beside_the_manifest(apps_admin, installed_app):
    with apps_admin.client() as client:
        saved = client.patch(
            f"/api/v1/apps/{installed_app['id']}", json={"settings": {"greeting": "hi"}}
        )
        reread = _app(client, installed_app["id"]).json()
    assert saved.status_code == 200
    assert reread["data"]["settings"] == {"greeting": "hi"}
    assert reread["data"]["manifest"]["id"] == installed_app["data"]["manifest"]["id"]


def test_narrowing_scopes_keeps_the_app_connected(apps_admin, installed_app):
    with apps_admin.client() as client:
        narrowed = client.patch(
            f"/api/v1/apps/{installed_app['id']}",
            json={"granted_scopes": ["channels:mentions:read"]},
        )
    assert narrowed.status_code == 200
    assert narrowed.json()["data"]["granted_scopes"] == ["channels:mentions:read"]
    assert narrowed.json()["is_active"] is True


def test_granting_a_scope_the_app_did_not_request_is_refused(apps_instance, apps_admin):
    from harness.apps import serving_reference_app

    with serving_reference_app([apps_instance.base_url], scopes=["channels:mentions:read"]) as app:
        with apps_admin.client() as client:
            connected = installed(client, app)
            widened = client.patch(
                f"/api/v1/apps/{connected['id']}",
                json={"granted_scopes": ["channels:messages:write"]},
            )
    assert widened.status_code == 400
    assert widened.json()["detail"] == "Unrequested scope"


def test_the_enable_switch_cannot_turn_an_app_on_without_connecting(apps_admin, installed_app):
    with apps_admin.client() as client:
        disabled = client.patch(f"/api/v1/apps/{installed_app['id']}", json={"is_active": False})
        forced = client.patch(f"/api/v1/apps/{installed_app['id']}", json={"is_active": True})
        reconnected = connect(client, installed_app["id"])
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert forced.status_code == 400
    assert forced.json()["detail"] == "Connect the app to enable it"
    assert reconnected.status_code == 200
    assert reconnected.json()["is_active"] is True


def test_an_updated_manifest_switches_the_app_off_until_reconnected(
    apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        manifest = preview(client, reference_app, app_id=installed_app["id"]).json()
        manifest["description"] = "A newer release."
        updated = client.patch(
            f"/api/v1/apps/{installed_app['id']}",
            json={"manifest": manifest, "granted_scopes": manifest["requested_scopes"]},
        )
        reconnected = connect(client, installed_app["id"])
    assert updated.status_code == 200, updated.text
    assert updated.json()["is_active"] is False
    assert updated.json()["data"]["manifest"]["description"] == "A newer release."
    assert reconnected.json()["is_active"] is True


def test_preview_for_an_installed_app_reuses_its_stored_key(
    apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        previewed = client.post(
            "/api/v1/apps/manifest/preview",
            json={"url": reference_app.url, "app_id": installed_app["id"]},
        )
        elsewhere = client.post(
            "/api/v1/apps/manifest/preview",
            json={"url": reference_app.url + "/other", "app_id": installed_app["id"]},
        )
    assert previewed.status_code == 200
    assert elsewhere.status_code == 400
    assert elsewhere.json()["detail"] == "App URL does not match the installed app"


def test_an_updated_manifest_cannot_change_the_app_identity(
    apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        manifest = reference_app.manifest(id="someone-else")
        updated = client.patch(
            f"/api/v1/apps/{installed_app['id']}",
            json={"manifest": manifest, "granted_scopes": []},
        )
    assert updated.status_code == 400
    assert updated.json()["detail"] == "App identity cannot change"


def test_an_updated_manifest_without_reviewed_scopes_is_refused(
    apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        updated = client.patch(
            f"/api/v1/apps/{installed_app['id']}", json={"manifest": reference_app.manifest()}
        )
    assert updated.status_code == 400
    assert updated.json()["detail"] == "Review the updated scopes"


def test_uninstall_drops_the_key_and_every_channel_membership(apps_admin, installed_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        assert invite(client, channel_id, installed_app["id"]).status_code == 200
        removed = client.delete(f"/api/v1/apps/{installed_app['id']}")
        members = client.get(f"/api/v1/channels/{channel_id}/apps").json()
        reconnect = connect(client, installed_app["id"])
        listed = client.get("/api/v1/apps").json()
    assert removed.status_code == 200
    assert removed.json()["is_active"] is False
    assert removed.json()["configured"] is False
    assert members == []
    assert reconnect.status_code == 400
    assert reconnect.json()["detail"] == "An app API key is required"
    assert any(entry["id"] == installed_app["id"] for entry in listed), "the row stays for history"


def test_an_uninstalled_app_reconnects_once_given_its_key_again(
    apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        client.delete(f"/api/v1/apps/{installed_app['id']}")
        reconnected = connect(client, installed_app["id"], api_key=reference_app.api_key)
    assert reconnected.status_code == 200
    assert reconnected.json()["is_active"] is True


def test_unknown_app_ids_are_not_found(apps_admin):
    with apps_admin.client() as client:
        responses = [
            client.get("/api/v1/apps/no-such-app"),
            client.patch("/api/v1/apps/no-such-app", json={"name": "x"}),
            client.post("/api/v1/apps/no-such-app/connect", json={}),
            client.delete("/api/v1/apps/no-such-app"),
        ]
    assert [response.status_code for response in responses] == [404, 404, 404, 404]


def test_users_only_see_active_apps_they_may_use(apps_admin, make_apps_user, reference_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        public = installed(client, reference_app)
        private = installed(client, reference_app, access_grants=[])
        granted = installed(
            client,
            reference_app,
            access_grants=[
                {"principal_type": "user", "principal_id": member.id, "permission": "read"}
            ],
        )
        disabled = installed(client, reference_app)
        client.patch(f"/api/v1/apps/{disabled['id']}", json={"is_active": False})
        admin_sees = {entry["id"] for entry in client.get("/api/v1/apps").json()}
    with member.client() as client:
        member_sees = {entry["id"] for entry in client.get("/api/v1/apps").json()}
    assert {public["id"], private["id"], granted["id"], disabled["id"]} <= admin_sees
    assert public["id"] in member_sees
    assert granted["id"] in member_sees
    assert private["id"] not in member_sees
    assert disabled["id"] not in member_sees


def test_access_grants_can_be_replaced_later(apps_admin, make_apps_user, installed_app):
    member = make_apps_user()
    with apps_admin.client() as client:
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"access_grants": []})
    with member.client() as client:
        hidden = {entry["id"] for entry in client.get("/api/v1/apps").json()}
    assert installed_app["id"] not in hidden


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/apps/manifest/preview", {"url": "https://x.example/e", "api_key": "k"}),
        ("post", "/api/v1/apps", {}),
        ("get", "/api/v1/apps/{id}", None),
        ("patch", "/api/v1/apps/{id}", {"name": "mine now"}),
        ("post", "/api/v1/apps/{id}/connect", {}),
        ("delete", "/api/v1/apps/{id}", None),
    ],
)
def test_only_admins_manage_apps(make_apps_user, installed_app, method, path, body):
    member = make_apps_user()
    with member.client() as client:
        kwargs = {"json": body} if body is not None else {}
        response = getattr(client, method)(path.format(id=installed_app["id"]), **kwargs)
    assert response.status_code in (401, 403), response.text


def test_signed_out_callers_cannot_list_apps(apps_instance):
    import httpx

    assert httpx.get(f"{apps_instance.base_url}/api/v1/apps").status_code == 401


def test_turning_apps_off_hides_every_app_endpoint(
    apps_admin, installed_app, preserve, apps_instance
):
    preserve("admin_config", on=apps_instance)
    with apps_admin.client() as client:
        config = client.get(ADMIN_CONFIG).json()
        client.post(ADMIN_CONFIG, json={**config, "ENABLE_APPS": False})
        listed = client.get("/api/v1/apps")
        details = _app(client, installed_app["id"])
        public_config = client.get("/api/config").json()
    assert listed.status_code == 404
    assert details.status_code == 404
    assert public_config["features"]["enable_apps"] is False


def test_the_apps_switch_is_reported_to_the_web_client(apps_admin):
    with apps_admin.client() as client:
        public_config = client.get("/api/config").json()
        admin_config = client.get(ADMIN_CONFIG).json()
    assert public_config["features"]["enable_apps"] is True
    assert admin_config["ENABLE_APPS"] is True


def test_saving_other_settings_leaves_apps_on(apps_admin, preserve, apps_instance):
    preserve("admin_config", on=apps_instance)
    with apps_admin.client() as client:
        config = client.get(ADMIN_CONFIG).json()
        config.pop("ENABLE_APPS")
        saved = client.post(ADMIN_CONFIG, json=config)
        after = client.get(ADMIN_CONFIG).json()
    assert saved.status_code == 200
    assert after["ENABLE_APPS"] is True


def test_installing_twice_from_one_manifest_gives_independent_apps(apps_admin, reference_app):
    with apps_admin.client() as client:
        first = install(client, reference_app).json()
        second = install(client, reference_app).json()
        client.delete(f"/api/v1/apps/{first['id']}")
        second_after = _app(client, second["id"]).json()
    assert second_after["configured"] is True
