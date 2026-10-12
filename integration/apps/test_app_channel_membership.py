"""Journey: inviting an app into channels, and who may invite, list or remove it.

An app only takes part in channels it was invited into. Managers of the channel (its owner, an
admin) invite it from the channel's member panel; the app must be active and usable by the one
inviting. Direct messages never take apps.

Discriminates: mutation, `invite_app` skipping `has_app_access`, turns
test_a_manager_cannot_invite_an_app_they_may_not_use red; removing an app from a channel without
clearing its membership's `is_active` turns test_a_removed_app_stops_receiving_messages red.
"""

from __future__ import annotations

import time

import pytest

from harness.apps import installed, invite, new_channel, post

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


def _channel_apps(client, channel_id):
    return client.get(f"/api/v1/channels/{channel_id}/apps")


def test_an_invited_app_is_listed_on_the_channel(apps_admin, installed_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        invited = invite(client, channel_id, installed_app["id"])
        listed = _channel_apps(client, channel_id).json()
    assert invited.status_code == 200
    assert [entry["id"] for entry in listed] == [installed_app["id"]]


def test_inviting_twice_keeps_one_membership(apps_admin, installed_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        invite(client, channel_id, installed_app["id"])
        again = invite(client, channel_id, installed_app["id"])
        listed = _channel_apps(client, channel_id).json()
    assert again.status_code == 200
    assert len(listed) == 1


def test_a_removed_app_can_be_invited_back(apps_admin, installed_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        invite(client, channel_id, installed_app["id"])
        removed = client.delete(f"/api/v1/channels/{channel_id}/apps/{installed_app['id']}")
        after_removal = _channel_apps(client, channel_id).json()
        invite(client, channel_id, installed_app["id"])
        after_return = _channel_apps(client, channel_id).json()
    assert removed.status_code == 200
    assert after_removal == []
    assert [entry["id"] for entry in after_return] == [installed_app["id"]]


def test_a_removed_app_stops_receiving_messages(apps_admin, installed_app, reference_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        invite(client, channel_id, installed_app["id"])
        post(client, channel_id, "before removal")
        reference_app.wait_for("message.created", app_id=installed_app["id"], channel_id=channel_id)
        client.delete(f"/api/v1/channels/{channel_id}/apps/{installed_app['id']}")
        post(client, channel_id, "after removal")
        post(client, channel_id, "!send still here?")
    time.sleep(2)
    seen = [
        entry["message"]["content"]
        for entry in reference_app.records("message.created", app_id=installed_app["id"])
        if entry["channel_id"] == channel_id
    ]
    assert seen == ["before removal"]


def test_an_inactive_app_cannot_be_invited(apps_admin, installed_app):
    with apps_admin.client() as client:
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"is_active": False})
        channel_id = new_channel(client)
        invited = invite(client, channel_id, installed_app["id"])
    assert invited.status_code == 403
    assert invited.json()["detail"] == "App is inactive"


def test_an_unknown_app_cannot_be_invited(apps_admin):
    with apps_admin.client() as client:
        invited = invite(client, new_channel(client), "no-such-app")
    assert invited.status_code == 404


def test_a_group_owner_invites_an_app_into_their_group(make_apps_user, installed_app):
    owner = make_apps_user()
    with owner.client() as client:
        channel_id = new_channel(client, type="group")
        invited = invite(client, channel_id, installed_app["id"])
        listed = _channel_apps(client, channel_id).json()
    assert invited.status_code == 200, invited.text
    assert [entry["id"] for entry in listed] == [installed_app["id"]]


def test_a_manager_cannot_invite_an_app_they_may_not_use(apps_admin, make_apps_user, reference_app):
    owner = make_apps_user()
    with apps_admin.client() as client:
        restricted = installed(client, reference_app, access_grants=[])
    with owner.client() as client:
        channel_id = new_channel(client, type="group")
        invited = invite(client, channel_id, restricted["id"])
    assert invited.status_code == 403
    assert invited.json()["detail"] == "App invocation denied"


def test_a_plain_member_cannot_invite_or_remove_apps(apps_admin, make_apps_user, installed_app):
    owner, member = make_apps_user(), make_apps_user()
    with owner.client() as client:
        channel_id = new_channel(client, type="group", user_ids=[member.id])
        invite(client, channel_id, installed_app["id"])
    with member.client() as client:
        listed = _channel_apps(client, channel_id)
        invited = invite(client, channel_id, installed_app["id"])
        removed = client.delete(f"/api/v1/channels/{channel_id}/apps/{installed_app['id']}")
    assert listed.status_code == 200
    assert [entry["id"] for entry in listed.json()] == [installed_app["id"]]
    assert invited.status_code == 403
    assert removed.status_code == 403


def test_an_outsider_cannot_see_which_apps_a_group_has(make_apps_user, installed_app):
    owner, outsider = make_apps_user(), make_apps_user()
    with owner.client() as client:
        channel_id = new_channel(client, type="group")
        invite(client, channel_id, installed_app["id"])
    with outsider.client() as client:
        listed = _channel_apps(client, channel_id)
        invited = invite(client, channel_id, installed_app["id"])
    assert listed.status_code == 403
    assert invited.status_code == 403


def test_direct_messages_take_no_apps(apps_admin, make_apps_user, installed_app):
    friend = make_apps_user()
    with apps_admin.client() as client:
        dm = client.post(
            "/api/v1/channels/create", json={"name": "", "type": "dm", "user_ids": [friend.id]}
        )
        invited = invite(client, dm.json()["id"], installed_app["id"])
    assert dm.status_code == 200, dm.text
    assert invited.status_code == 404
    assert invited.json()["detail"] == "Channel unavailable"


def test_an_archived_or_deleted_channel_takes_no_apps(apps_admin, installed_app):
    with apps_admin.client() as client:
        channel_id = new_channel(client)
        deleted = client.delete(f"/api/v1/channels/{channel_id}/delete")
        invited = invite(client, channel_id, installed_app["id"])
    assert deleted.status_code == 200, deleted.text
    assert invited.status_code == 404
