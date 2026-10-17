"""Journey: an owner adds and removes channel members and a private channel stays closed.

The member list opens from the user count in the channel header. The owner sees "Add Member" and
a remove button per person; a plain member sees neither. Adding someone by name shows a toast and
raises the count, after which the new member can open the channel and read what was said before.
Removing someone lowers the count, and that person is sent home when they open the channel, as is
anyone who was never in it, and the channel is not in their sidebar.

Discriminates: passes on dev 176d31d1d; in a frontend copy, adding nobody on submit turns the add
test red, removing the wrong person turns the remove test red, offering the manager buttons to
every member turns the plain member test red, and staying on the channel page when it cannot be
loaded turns the outsider tests red.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from harness.channel_quotes import enable_channels, group_channel, post_message
from utils.chat_ui import chat_input
from utils.member_picker import pick_member

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]


@pytest.fixture
def channels_on(admin, preserve):
    preserve("admin_config")
    enable_channels(admin)


def _open_channel(page: Page, channel_id: str) -> None:
    page.goto(f"/channels/{channel_id}")
    expect(chat_input(page)).to_be_visible()


def _open_member_list(page: Page) -> None:
    page.get_by_role("button", name="User Count").click()
    expect(page.get_by_role("dialog").get_by_text("Members", exact=True)).to_be_visible()


def _member_count(page: Page):
    return page.get_by_role("button", name="User Count")


def _can_read(actor, channel_id: str) -> bool:
    with actor.client() as client:
        return client.get(f"/api/v1/channels/{channel_id}/messages").status_code == 200


def test_the_owner_adds_a_member_who_then_reads_the_earlier_messages(
    channels_on, make_user, page_for
):
    owner, member, newcomer = make_user(), make_user(), make_user()
    channel_id = group_channel(owner, member)
    post_message(owner, channel_id, "the harbour map is pinned")
    page = page_for(owner)
    _open_channel(page, channel_id)
    expect(_member_count(page)).to_contain_text("2")

    _open_member_list(page)
    page.get_by_role("button", name="Add Member").click()
    page.get_by_role("dialog").filter(has_text="Add Members").get_by_placeholder("Search").fill(
        newcomer.name
    )
    pick_member(page.get_by_role("dialog"), newcomer.name)
    page.get_by_role("dialog").get_by_role("button", name="Add", exact=True).click()

    expect(page.get_by_text("Members added successfully")).to_be_visible()
    expect(_member_count(page)).to_contain_text("3")
    newcomer_page = page_for(newcomer)
    _open_channel(newcomer_page, channel_id)
    expect(newcomer_page.get_by_text("the harbour map is pinned")).to_be_visible()


def test_the_owner_removes_a_member_who_is_then_sent_home(channels_on, make_user, page_for):
    owner, leaving, staying = make_user(), make_user(), make_user()
    channel_id = group_channel(owner, leaving, staying)
    page = page_for(owner)
    _open_channel(page, channel_id)
    expect(_member_count(page)).to_contain_text("3")

    _open_member_list(page)
    dialog = page.get_by_role("dialog")
    dialog.get_by_placeholder("Search").fill(leaving.name)
    expect(dialog.get_by_text(staying.name)).to_have_count(0)
    # the remove button has no name; with one person listed it is the dialog's last button
    dialog.get_by_role("button").last.click()

    expect(page.get_by_text("Member removed successfully")).to_be_visible()
    expect(_member_count(page)).to_contain_text("2")
    assert not _can_read(leaving, channel_id)
    assert _can_read(staying, channel_id)
    leaving_page = page_for(leaving, path=f"/channels/{channel_id}")
    expect(chat_input(leaving_page)).to_be_visible()
    expect(leaving_page).not_to_have_url(re.compile("/channels/"))


def test_a_plain_member_sees_the_list_without_add_or_remove(channels_on, make_user, page_for):
    owner, member = make_user(), make_user()
    channel_id = group_channel(owner, member)
    page = page_for(member)
    _open_channel(page, channel_id)

    _open_member_list(page)

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text(owner.name)).to_be_visible()
    expect(dialog.get_by_role("button", name="Add Member")).to_have_count(0)
    # a remove button has no name: only Close and the two profile buttons remain
    expect(dialog.locator("button")).to_have_count(3)


def test_someone_outside_a_private_channel_is_sent_home_and_never_sees_it(
    channels_on, make_user, page_for
):
    owner, member, outsider = make_user(), make_user(), make_user()
    channel_id = group_channel(owner, member)
    post_message(owner, channel_id, "the anchor code is 4417")
    page = page_for(outsider, path=f"/channels/{channel_id}")

    expect(chat_input(page)).to_be_visible()
    expect(page).not_to_have_url(re.compile("/channels/"))
    expect(page.get_by_text("the anchor code is 4417")).to_have_count(0)
    assert not _can_read(outsider, channel_id)
