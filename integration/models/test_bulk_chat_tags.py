"""Regression: the tag list went out of step with the chats after the bulk chat actions.

Fix 0ab3a2b33 (open-webui/open-webui#30453, issue open-webui/open-webui#30452). Unarchive all
brought the chats back without the tags that archiving had dropped from the tag list, so the
sidebar's tag suggestions and tag search lacked them. Delete all removed every chat and left
their tags behind in the suggestions with no chat to point at. Delete all now removes the tags no
chat of the account uses any more.

Since 78550396d (open-webui/open-webui#32200, issue open-webui/open-webui#30454) archiving keeps
the tag, as an archived chat still uses it; before, archiving the last chat with a tag dropped it
and unarchiving rebuilt it from its id, so "Harbour Log" came back as "harbour_log". The tag now
stays listed under its own name while archived and after unarchive all. The unarchive half is also
walked in the journey in test_bulk_chat_operations.py.

Discriminates: passes on dev 79d376a1f; with 78550396d reverted in a backend copy the archive
tests fail (the tag leaves the list on archive and comes back renamed), and with 0ab3a2b33
reverted the delete all test fails (the tag is still listed after delete all).
"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]


@pytest.fixture
def tag() -> str:
    return f"Harbour {uuid.uuid4().hex[:8]}"


def tagged_chat(client: httpx.Client, *tags: str) -> str:
    created = client.post("/api/v1/chats/new", json={"chat": {"title": "Tagged"}})
    assert created.status_code == 200, created.text
    chat_id = created.json()["id"]
    for name in tags:
        added = client.post(f"/api/v1/chats/{chat_id}/tags", json={"name": name})
        assert added.status_code == 200, added.text
    return chat_id


def tag_names(client: httpx.Client) -> set[str]:
    listed = client.get("/api/v1/chats/all/tags")
    assert listed.status_code == 200, listed.text
    return {entry["name"] for entry in listed.json()}


def chats_tagged(client: httpx.Client, tag: str) -> list[str]:
    found = client.post("/api/v1/chats/tags", json={"name": tag})
    assert found.status_code == 200, found.text
    return [chat["id"] for chat in found.json()]


def archive(client: httpx.Client, chat_id: str) -> None:
    archived = client.post(f"/api/v1/chats/{chat_id}/archive")
    assert archived.status_code == 200 and archived.json()["archived"] is True, archived.text


def unarchive_all(client: httpx.Client) -> None:
    restored = client.post("/api/v1/chats/unarchive/all")
    assert restored.status_code == 200 and restored.json() is True, restored.text


def delete_all(client: httpx.Client) -> None:
    deleted = client.delete("/api/v1/chats/")
    assert deleted.status_code == 200 and deleted.json() is True, deleted.text


def test_archiving_the_only_chat_with_a_tag_keeps_the_tag_and_its_name(make_user, tag):
    with make_user().client() as client:
        chat_id = tagged_chat(client, tag)
        archive(client, chat_id)
        assert tag in tag_names(client), (
            "archiving the only chat with a tag dropped the tag from the list (#30454)"
        )

        unarchive_all(client)

        assert tag in tag_names(client), "the tag came back renamed after unarchive all (#30454)"
        assert chats_tagged(client, tag) == [chat_id]


def test_delete_all_removes_the_tags_no_chat_uses_any_more(make_user, tag):
    with make_user().client() as client:
        tagged_chat(client, tag)
        archive(client, tagged_chat(client, f"{tag}-archived"))
        assert tag in tag_names(client)

        delete_all(client)

        assert tag_names(client) == set(), (
            "deleting every chat left their tags in the tag suggestions with no chat to point "
            "at (#30452)"
        )


def test_unarchive_all_keeps_every_tag_of_every_archived_chat(make_user, tag):
    first, second, shared = f"{tag} first", f"{tag} second", f"{tag} shared"
    with make_user().client() as client:
        first_id = tagged_chat(client, first, shared)
        second_id = tagged_chat(client, second, shared)
        archive(client, first_id)
        archive(client, second_id)
        assert {first, second, shared} <= tag_names(client)

        unarchive_all(client)

        assert {first, second, shared} <= tag_names(client)
        assert sorted(chats_tagged(client, shared)) == sorted([first_id, second_id])


def test_delete_all_leaves_another_accounts_tag_of_the_same_name(make_user, tag):
    owner, bystander = make_user(), make_user()
    with owner.client() as owner_client, bystander.client() as bystander_client:
        tagged_chat(owner_client, tag)
        bystander_chat = tagged_chat(bystander_client, tag)

        delete_all(owner_client)

        assert tag not in tag_names(owner_client)
        assert tag in tag_names(bystander_client)
        assert chats_tagged(bystander_client, tag) == [bystander_chat]


def test_unarchive_all_keeps_the_tags_already_listed(make_user, tag):
    with make_user().client() as client:
        tagged_chat(client, tag)
        archive(client, tagged_chat(client, tag))
        assert tag in tag_names(client), "a live chat still carries the tag"

        unarchive_all(client)

        assert tag in tag_names(client)
        assert len(chats_tagged(client, tag)) == 2
