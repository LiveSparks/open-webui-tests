"""Journey: people use an app inside a channel, from inviting it to pressing its buttons.

A channel manager invites the app from the member panel (the user count in the header). People
then mention it from the `@` picker; a private answer appears under their message marked "Only
visible to you" for them alone, while a channel answer and the app's own posts show to everyone
under the app's name. Buttons on an app message call the app; deferred work shows "Working…"
until the app has finished, and a download answer offers its file.

Discriminates: passes on dev 1786d6cdb; in a frontend copy, never polling a deferred answer
leaves it on "Working…" and turns test_deferred_work_shows_working_then_the_result red.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from harness.apps import installed, invite, new_channel, post, wait_for_message
from utils.chat_ui import chat_input
from utils.tooltips import tooltip_button

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

ANSWER_TIMEOUT_MS = 20_000


def _open_channel(page: Page, channel_id: str) -> None:
    page.goto(f"/channels/{channel_id}")
    expect(chat_input(page)).to_be_visible()


def _type_mention(page: Page, app_name: str, text: str) -> None:
    chat_input(page).click()
    page.keyboard.type("@" + app_name.split()[0])
    page.get_by_role("button", name=app_name).first.click()
    page.keyboard.type(" " + text)
    page.keyboard.press("Enter")


def _private_reply(page: Page, app_id: str):
    return page.locator(f"[data-app-reply='{app_id}']")


def _channel_with_app(admin, app, **form) -> str:
    with admin.client() as client:
        channel_id = new_channel(client, **form)
        assert invite(client, channel_id, app["id"]).status_code == 200
    return channel_id


def _open_member_list(page: Page):
    page.get_by_role("button", name="User Count").click()
    members = page.get_by_role("dialog").filter(has_text="Members")
    expect(members.first).to_be_visible()
    return members.first


def test_a_manager_invites_and_removes_an_app_from_the_member_list(
    page_for, apps_admin, installed_app
):
    with apps_admin.client() as client:
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"name": "Invitable App"})
        channel_id = new_channel(client)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    members = _open_member_list(page)
    members.get_by_role("button", name="Add Member").click()
    adding = page.get_by_role("dialog").filter(has_text="Add Members")
    adding.get_by_placeholder("Search").fill("Invitable")
    adding.get_by_label("Invitable App").click()
    adding.get_by_role("button", name="Add", exact=True).click()
    expect(page.get_by_text("Members added successfully")).to_be_visible()
    with apps_admin.client() as client:
        joined = client.get(f"/api/v1/channels/{channel_id}/apps").json()
    assert [app["id"] for app in joined] == [installed_app["id"]]
    expect(members.get_by_text("Invitable App")).to_be_visible()
    members.get_by_role("button", name="Remove Invitable App").click()
    expect(members.get_by_text("Invitable App")).to_have_count(0)
    with apps_admin.client() as client:
        assert client.get(f"/api/v1/channels/{channel_id}/apps").json() == []


def test_a_plain_member_sees_the_app_but_cannot_manage_it(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    page = page_for(member)
    _open_channel(page, channel_id)
    members = _open_member_list(page)
    expect(members.get_by_text(installed_app["name"])).to_be_visible()
    remove = members.get_by_role("button", name=f"Remove {installed_app['name']}")
    expect(remove).to_have_count(0)
    expect(members.get_by_role("button", name="Add Member")).to_have_count(0)


def test_the_app_answers_a_command_for_everyone_under_its_name(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    asker, watcher = page_for(apps_admin), page_for(member)
    _open_channel(asker, channel_id)
    _open_channel(watcher, channel_id)
    chat_input(asker).click()
    asker.keyboard.type("!echo seen by all")
    asker.keyboard.press("Enter")
    for page in (asker, watcher):
        answer = page.get_by_text("echo: seen by all")
        expect(answer).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
        expect(page.get_by_text(installed_app["name"]).last).to_be_visible()


def test_a_mention_from_the_picker_gets_a_private_answer(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    _type_mention(page, installed_app["name"], "hello")
    reply = _private_reply(page, installed_app["id"])
    expect(reply).to_contain_text("pong:", timeout=ANSWER_TIMEOUT_MS)
    expect(reply).to_contain_text("Only visible to you")
    reply.get_by_role("button", name="Dismiss").click()
    expect(reply).to_have_count(0)


def test_a_private_answer_shows_only_to_the_person_who_asked(
    page_for, apps_admin, make_apps_user, installed_app
):
    asker_account, watcher_account = make_apps_user(), make_apps_user()
    channel_id = _channel_with_app(
        apps_admin, installed_app, type="group", user_ids=[asker_account.id, watcher_account.id]
    )
    asker, watcher = page_for(asker_account), page_for(watcher_account)
    _open_channel(asker, channel_id)
    _open_channel(watcher, channel_id)
    _type_mention(asker, installed_app["name"], "secret question")
    expect(_private_reply(asker, installed_app["id"])).to_contain_text(
        "pong:", timeout=ANSWER_TIMEOUT_MS
    )
    expect(watcher.get_by_text("secret question")).to_be_visible()
    expect(_private_reply(watcher, installed_app["id"])).to_have_count(0)


def test_a_public_mention_answer_shows_to_everyone(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    asker, watcher = page_for(apps_admin), page_for(member)
    _open_channel(asker, channel_id)
    _open_channel(watcher, channel_id)
    _type_mention(asker, installed_app["name"], "public")
    expected = f"public answer for {apps_admin.id}"
    expect(asker.get_by_text(expected)).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(watcher.get_by_text(expected)).to_be_visible(timeout=ANSWER_TIMEOUT_MS)


def test_pressing_a_button_updates_the_message_for_everyone(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    with apps_admin.client() as client:
        post(client, channel_id, "!buttons")
        wait_for_message(client, channel_id, "Pick one")
    presser, watcher = page_for(member), page_for(apps_admin)
    _open_channel(presser, channel_id)
    _open_channel(watcher, channel_id)
    presser.get_by_role("button", name="Approve").click()
    expected = f"Approved by {member.id}"
    expect(presser.get_by_text(expected)).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(watcher.get_by_text(expected)).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    expect(watcher.get_by_text("Pick one")).to_have_count(0)


def test_a_button_with_a_private_answer_tells_only_the_presser(
    page_for, apps_admin, make_apps_user, installed_app
):
    member = make_apps_user()
    channel_id = _channel_with_app(apps_admin, installed_app, type="group", user_ids=[member.id])
    with apps_admin.client() as client:
        post(client, channel_id, "!buttons")
        wait_for_message(client, channel_id, "Pick one")
    presser, watcher = page_for(member), page_for(apps_admin)
    _open_channel(presser, channel_id)
    _open_channel(watcher, channel_id)
    presser.get_by_role("button", name="Who am I").click()
    reply = _private_reply(presser, installed_app["id"])
    expect(reply).to_contain_text(f"you are {member.id} admin=False", timeout=ANSWER_TIMEOUT_MS)
    expect(_private_reply(watcher, installed_app["id"])).to_have_count(0)


def test_deferred_work_shows_working_then_the_result(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    _type_mention(page, installed_app["name"], "defer")
    reply = _private_reply(page, installed_app["id"])
    expect(reply.get_by_role("status")).to_have_text("Working…", timeout=ANSWER_TIMEOUT_MS)
    expect(reply).to_contain_text("Job finished", timeout=ANSWER_TIMEOUT_MS)
    expect(reply.get_by_role("status")).to_have_count(0)


def test_a_download_answer_offers_its_file(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    _type_mention(page, installed_app["name"], "report")
    reply = _private_reply(page, installed_app["id"])
    button = reply.get_by_role("button", name="Download weekly-report.md")
    expect(button).to_be_visible(timeout=ANSWER_TIMEOUT_MS)
    with page.expect_download() as download:
        button.click()
    assert download.value.suggested_filename == "weekly-report.md"


def test_an_app_failure_tells_the_asker(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    _type_mention(page, installed_app["name"], "fail")
    reply = _private_reply(page, installed_app["id"])
    expect(reply.get_by_role("alert")).to_contain_text(
        "could not complete", timeout=ANSWER_TIMEOUT_MS
    )


def test_the_picker_offers_only_apps_in_this_channel(
    page_for, apps_admin, installed_app, reference_app
):
    with apps_admin.client() as client:
        outsider = installed(client, reference_app)
        client.patch(f"/api/v1/apps/{outsider['id']}", json={"name": "Outsider App"})
        client.patch(f"/api/v1/apps/{installed_app['id']}", json={"name": "Insider App"})
    channel_id = _channel_with_app(apps_admin, installed_app)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    chat_input(page).click()
    page.keyboard.type("@")
    expect(page.get_by_role("button", name="Insider App")).to_be_visible()
    expect(page.get_by_role("button", name="Outsider App")).to_have_count(0)


def test_the_app_starts_a_thread_people_can_open(page_for, apps_admin, installed_app):
    channel_id = _channel_with_app(apps_admin, installed_app)
    page = page_for(apps_admin)
    _open_channel(page, channel_id)
    chat_input(page).click()
    page.keyboard.type("!thread written in a thread")
    page.keyboard.press("Enter")
    with apps_admin.client() as client:
        root = wait_for_message(client, channel_id, "!thread written in a thread")
        wait_for_message(client, channel_id, "written in a thread", parent_id=root["id"])
    parent = page.locator(f"[id='message-{root['id']}']").first
    parent.hover()
    tooltip_button(parent, "Reply in Thread").click()
    in_thread = page.locator(f"[id^='message-{root['id']}-']").filter(
        has_text="written in a thread"
    )
    expect(in_thread.first).to_be_visible()
