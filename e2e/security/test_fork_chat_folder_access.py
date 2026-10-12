"""Regression: cloning a chat in a folder shared for writing moved the clone out of the folder.

Fix `d4d04dca0` (#31370, issue #31369): Clone in a chat's sidebar menu goes through the chat
import, which kept the chat's folder only when the user owned it. For a chat of theirs in a
folder another account shares with them for writing, the clone landed at the root of their chat
list. The import now keeps any folder the user may write to.

Twin of integration/security/test_fork_chat_folder_access.py.

Discriminates: passes on dev a5bc78300 with its build; with d4d04dca0 reverted in the backend the
shared folder does not list the clone and the stored clone has no folder.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Locator, Page, expect

from utils.sidebar_folders import folder_row

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

TITLE = "Packing list"
CONVERSATION = {
    "title": TITLE,
    "history": {
        "currentId": "m1",
        "messages": {
            "m1": {"id": "m1", "parentId": None, "childrenIds": [], "role": "user", "content": "hi"}
        },
    },
}


@pytest.fixture
def shared_folder(make_user):
    """Another account's folder shared for writing with a member, holding the member's chat."""
    owner, member = make_user(), make_user()
    name = f"Trip {uuid.uuid4().hex[:6]}"
    with owner.client() as client:
        created = client.post("/api/v1/folders/", json={"name": name})
        assert created.status_code == 200, created.text
        folder_id = created.json()["id"]
        grant = {"principal_type": "user", "principal_id": member.id, "permission": "write"}
        shared = client.post(
            f"/api/v1/folders/{folder_id}/access/update", json={"access_grants": [grant]}
        )
        assert shared.status_code == 200, shared.text
    with member.client() as client:
        chat = client.post("/api/v1/chats/new", json={"chat": CONVERSATION})
        assert chat.status_code == 200, chat.text
        moved = client.post(
            f"/api/v1/chats/{chat.json()['id']}/folder", json={"folder_id": folder_id}
        )
        assert moved.status_code == 200, moved.text
    return member, name, folder_id


def _expanded_shared_folder(page: Page, name: str, folder_id: str) -> Locator:
    """The shared folder's row in the sidebar with everything filed under it, opened."""
    page.get_by_role("button", name="Open Sidebar", exact=True).click()
    sidebar = page.get_by_role("navigation", name="Chat history")
    section = sidebar.get_by_role("button", name="Folders", exact=True)
    expect(section).to_be_visible()
    if section.get_attribute("aria-expanded") != "true":
        section.click()
    folder_row(sidebar, name).get_by_role("button").first.click()
    folder = sidebar.locator(f"#folder-{folder_id}-button").locator(
        "xpath=ancestor::div[@draggable][1]"
    )
    expect(folder.get_by_role("button", name=TITLE).first).to_be_visible()
    return folder


def _clone_from_the_chat_menu(folder: Locator) -> str:
    """Clone the chat from its sidebar menu; returns the clone's id."""
    row = folder.locator("#sidebar-chat-group").filter(has_text=TITLE)
    row.hover()
    row.get_by_role("button", name=TITLE).first.focus()
    row.get_by_role("button", name="Chat Menu").first.click()
    page = folder.page
    with page.expect_response(lambda response: response.url.endswith("/clone")) as cloned:
        page.get_by_role("menu").get_by_role("button", name="Clone").click()
    assert cloned.value.ok, cloned.value.text()
    return cloned.value.json()["id"]


def test_a_clone_in_a_writable_shared_folder_is_listed_in_it(page_for, shared_folder):
    member, name, folder_id = shared_folder
    page = page_for(member)

    folder = _expanded_shared_folder(page, name, folder_id)
    clone_id = _clone_from_the_chat_menu(folder)

    expect(
        folder.get_by_role("button", name=f"Clone of {TITLE}"),
        "the clone of a chat in a folder the member may write to is not listed in it (#31369)",
    ).to_be_visible()
    with member.client() as client:
        stored = client.get(f"/api/v1/chats/{clone_id}").json()
    assert stored["folder_id"] == folder_id, (
        "the clone of a chat in a folder the member may write to landed at the root of their "
        "chat list (#31369)"
    )
