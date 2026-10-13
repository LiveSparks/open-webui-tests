"""Regression: deleting a folder with its chats left the share links of those chats working.

Fix `ffe21bef8` (open-webui/open-webui#31306, issue open-webui/open-webui#31305): deleting a folder
together with its contents removed the chats and their messages but not their shared snapshots, so
everyone holding a share link could still open a conversation its owner had deleted. The share rows
of those chats now go with them. Deleting all chats, loose and filed, already removed the shares and
is pinned here too, as is a folder deleted without its contents, whose chats and shares stay.
Deleting one chat is pinned in integration/chat/test_chat_sharing.py.

Discriminates: passes on dev efe63bd34; with ffe21bef8 reverted in a backend copy both folder
tests with contents fail (the share still opens). The other tests pass on both.
"""

from __future__ import annotations

import uuid

import pytest

from harness.chat_history import seed_chat

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]


def _folder(owner, parent_id: str | None = None) -> str:
    with owner.client() as client:
        created = client.post(
            "/api/v1/folders/",
            json={"name": f"folder {uuid.uuid4().hex[:6]}", "parent_id": parent_id},
        )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _shared_chat(owner, reader, folder_id: str | None = None) -> tuple[str, str]:
    """A chat, in `folder_id` if given, shared with `reader`; returns (chat id, share id)."""
    with owner.client() as client:
        chat_id, _ = seed_chat(
            client,
            [
                {"role": "user", "content": "what is the door code?"},
                {"role": "assistant", "content": "It is 4711."},
            ],
        )
        if folder_id:
            moved = client.post(f"/api/v1/chats/{chat_id}/folder", json={"folder_id": folder_id})
            assert moved.status_code == 200, moved.text
        shared = client.post(f"/api/v1/chats/{chat_id}/share")
        assert shared.status_code == 200, shared.text
        opened = client.post(
            f"/api/v1/chats/shared/{chat_id}/access/update",
            json={
                "access_grants": [
                    {"principal_type": "user", "principal_id": reader.id, "permission": "read"}
                ]
            },
        )
        assert opened.status_code == 200, opened.text
    return chat_id, shared.json()["share_id"]


def _share_opens(reader, share_id: str) -> bool:
    with reader.client() as client:
        return client.get(f"/api/v1/chats/share/{share_id}").status_code == 200


def _delete_folder(owner, folder_id: str, delete_contents: bool) -> None:
    with owner.client() as client:
        deleted = client.delete(
            f"/api/v1/folders/{folder_id}",
            params={"delete_contents": str(delete_contents).lower()},
        )
    assert deleted.status_code == 200, deleted.text


def test_deleting_a_folder_with_its_chats_revokes_their_share_links(make_user):
    owner, reader = make_user(), make_user()
    folder_id = _folder(owner)
    _, share_id = _shared_chat(owner, reader, folder_id)
    assert _share_opens(reader, share_id)

    _delete_folder(owner, folder_id, delete_contents=True)

    assert not _share_opens(reader, share_id), (
        "the chat was deleted with its folder, yet its share link still opened it (#31305)"
    )


def test_deleting_a_parent_folder_revokes_the_share_links_in_its_subfolders(make_user):
    owner, reader = make_user(), make_user()
    parent_id = _folder(owner)
    child_id = _folder(owner, parent_id)
    _, share_id = _shared_chat(owner, reader, child_id)

    _delete_folder(owner, parent_id, delete_contents=True)

    assert not _share_opens(reader, share_id), (
        "a chat deleted with its parent folder stayed reachable through its share link (#31305)"
    )


def test_a_folder_deleted_without_its_contents_keeps_the_share_links(make_user):
    owner, reader = make_user(), make_user()
    folder_id = _folder(owner)
    chat_id, share_id = _shared_chat(owner, reader, folder_id)

    _delete_folder(owner, folder_id, delete_contents=False)

    assert _share_opens(reader, share_id), "the chats were kept but their share links were not"
    with owner.client() as client:
        assert client.get(f"/api/v1/chats/{chat_id}").status_code == 200


def test_deleting_all_chats_revokes_their_share_links(make_user):
    owner, reader = make_user(), make_user()
    _, loose_share = _shared_chat(owner, reader)
    _, filed_share = _shared_chat(owner, reader, _folder(owner))

    with owner.client() as client:
        deleted = client.delete("/api/v1/chats/")
    assert deleted.status_code == 200, deleted.text

    assert (_share_opens(reader, loose_share), _share_opens(reader, filed_share)) == (False, False)
