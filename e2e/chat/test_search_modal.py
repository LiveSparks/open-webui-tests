"""The chat search dialog finds a user's own chats, previews them and opens the one they pick.

The dialog opens from the sidebar's Search entry or the search shortcut. Typing sends the query to
`GET /api/v1/search` (since b4db56ae9), which matches chat titles and message text; a match in the
text shows as a highlighted snippet. The highlighted result shows in a preview pane beside the
list, and hovering or arrowing onto another result moves the preview there; Enter or the Open
button opens it. "Start a new conversation" in the Actions menu opens a new chat with the typed
text in its message box, where the person sends it (since 0ffd86967 the chat address only fills
the box). Archived chats and another account's matching chat never appear, the Location filter
narrows the results to one folder, and more matches than one page of 24 load as the result list
is scrolled to its end. Since b4db56ae9 the dialog has no filter words, no filter suggestions and
no per-result chat menu; its filter panel offers only View and Location.

Two tests pin fixed bugs: Enter on a highlighted chat closed the dialog without opening the chat
(#31003, fixed by PR #31004), and a new conversation started from the dialog dropped the text after
an `&` because the query went into the page address unencoded (#31469, fixed by PR #31592).

Twin of integration/models/test_chat_search_filters.py.

Discriminates: passes on dev f5a26f53a. In a build of it whose Enter opens nothing, whose ArrowUp
stays put, whose rows ignore the pointer, whose list never loads a second page, whose Location
filter is never sent, whose new conversation drops the query and whose snippet never highlights,
the Enter, arrow, hover, paging, Location, both new-conversation and highlight tests go red, and
the title, no-match and other-account tests stay green. In a build that puts the query into the
new chat's address unencoded the ampersand test goes red, and in a backend copy whose search
reaches every account's chats or includes archived ones the other-account test goes red.
"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from harness import upstream as reply
from utils.chat_ui import chat_input, expect_reply
from utils.search_dialog import (
    answers_query,
    no_matches,
    open_actions,
    open_search,
    open_search_from_sidebar,
    open_selected,
    preview,
    result,
    results,
    search,
    search_box,
)

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

LONG_AGO = 1_700_000_000


def unique_word() -> str:
    return f"quokka{uuid.uuid4().hex[:8]}"


def conversation(question: str, answer: str) -> dict:
    asked = {"id": "q", "parentId": None, "childrenIds": ["a"], "role": "user"}
    answered = {"id": "a", "parentId": "q", "childrenIds": [], "role": "assistant"}
    messages = {"q": {**asked, "content": question}, "a": {**answered, "content": answer}}
    return {"currentId": "a", "messages": messages}


def import_chat(
    client: httpx.Client,
    title: str,
    *,
    question: str = "hello",
    answer: str = "hi there",
    updated_at: int = LONG_AGO,
    **import_fields,
) -> str:
    """A stored chat with one question and answer, last changed at `updated_at`."""
    chat = {"title": title, "models": [], "history": conversation(question, answer)}
    entry = {"chat": chat, "created_at": updated_at, "updated_at": updated_at, **import_fields}
    imported = client.post("/api/v1/chats/import", json={"chats": [entry]})
    assert imported.status_code == 200, imported.text
    return imported.json()[0]["id"]


def post(client: httpx.Client, path: str, **body) -> dict:
    response = client.post(path, json=body or None)
    assert response.status_code == 200, response.text
    return response.json()


def type_query(dialog: Locator, text: str) -> None:
    """Types the way a person does, so the input's key handling runs as well."""
    search_box(dialog).click()
    with dialog.page.expect_response(answers_query(text)):
        search_box(dialog).press_sequentially(text)


def test_the_sidebar_entry_finds_a_chat_by_its_title_and_opens_it(page_for, make_user):
    account = make_user()
    word = unique_word()
    with account.client() as client:
        chat_id = import_chat(client, f"Trip {word}", answer="Pack a raincoat.")
        import_chat(client, "Weekly report")

    page = page_for(account)
    expect(chat_input(page)).to_be_visible()
    dialog = open_search_from_sidebar(page)
    search(dialog, word)

    expect(results(dialog)).to_have_count(1)
    expect(result(dialog, f"Trip {word}")).to_be_visible()
    result(dialog, f"Trip {word}").click()
    expect(preview(dialog)).to_contain_text("Pack a raincoat.")
    open_selected(dialog)

    expect(page).to_have_url(re.compile(f"/c/{chat_id}$"))
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("Pack a raincoat.")).to_be_visible()


def test_the_shortcut_finds_a_chat_by_its_message_text_and_highlights_the_match(
    page_for, make_user
):
    account = make_user()
    word = unique_word()
    with account.client() as client:
        import_chat(client, "Untitled trip", question=f"Where does the {word} live?")

    page = page_for(account)
    dialog = open_search(page)
    search(dialog, word)

    found = result(dialog, "Untitled trip")
    expect(results(dialog)).to_have_count(1)
    expect(found).to_be_visible()
    expect(found).to_contain_text(f"Where does the {word} live?")
    expect(found.get_by_role("mark")).to_have_text(word)


@pytest.fixture
def two_matching_chats(make_user) -> tuple:
    """An account with two chats matching one word: the newer lists first."""
    account = make_user()
    word = unique_word()
    with account.client() as client:
        older_id = import_chat(client, f"Older {word}", answer="The older answer.")
        newer_id = import_chat(
            client, f"Newer {word}", answer="The newer answer.", updated_at=LONG_AGO + 100
        )
    return account, word, older_id, newer_id


def test_hovering_a_result_previews_its_conversation(page_for, two_matching_chats):
    account, word, _older_id, _newer_id = two_matching_chats
    page = page_for(account)
    dialog = open_search(page)
    search(dialog, word)
    expect(results(dialog)).to_have_count(2)
    expect(preview(dialog)).to_contain_text("The newer answer.")

    result(dialog, f"Older {word}").hover()

    expect(preview(dialog)).to_contain_text("The older answer.")
    expect(preview(dialog)).not_to_contain_text("The newer answer.")


def test_arrow_keys_move_the_preview_from_result_to_result(page_for, two_matching_chats):
    account, word, _older_id, _newer_id = two_matching_chats
    page = page_for(account)
    dialog = open_search(page)
    type_query(dialog, word)
    expect(results(dialog)).to_have_count(2)

    # the selection stops at the last result, however far past it the arrow goes
    for _ in range(6):
        page.keyboard.press("ArrowDown")
    expect(preview(dialog)).to_contain_text("The older answer.")

    page.keyboard.press("ArrowUp")
    expect(preview(dialog)).to_contain_text("The newer answer.")
    expect(preview(dialog)).not_to_contain_text("The older answer.")


@pytest.mark.regression
def test_enter_opens_the_highlighted_chat(page_for, two_matching_chats):
    account, word, _older_id, newer_id = two_matching_chats
    page = page_for(account)
    dialog = open_search(page)
    type_query(dialog, word)
    expect(results(dialog)).to_have_count(2)
    for _ in range(6):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowUp")
    expect(preview(dialog)).to_contain_text("The newer answer.")

    page.keyboard.press("Enter")

    expect(page, "Enter closed the dialog without opening the chat (#31003)").to_have_url(
        re.compile(f"/c/{newer_id}$")
    )


def test_a_query_nothing_matches_says_no_results(page_for, make_user):
    account = make_user()
    with account.client() as client:
        import_chat(client, "Weekly report")

    page = page_for(account)
    dialog = open_search(page)
    expect(result(dialog, "Weekly report")).to_be_visible()
    search(dialog, unique_word())

    expect(no_matches(dialog)).to_be_visible()
    expect(results(dialog)).to_have_count(0)


def test_archived_and_other_accounts_matching_chats_never_show(page_for, make_user):
    account = make_user()
    word = unique_word()
    with account.client() as client:
        import_chat(client, f"Mine {word}")
        import_chat(client, f"Archived {word}", archived=True)
    with make_user().client() as other_client:
        import_chat(other_client, f"Theirs {word}", question=f"about {word}")

    page = page_for(account)
    dialog = open_search(page)
    search(dialog, word)

    expect(results(dialog)).to_have_count(1)
    expect(result(dialog, f"Mine {word}")).to_be_visible()


def test_the_location_filter_narrows_the_results_to_that_folder(page_for, make_user):
    account = make_user()
    word = unique_word()
    folder = f"Travel {word}"
    with account.client() as client:
        folder_id = post(client, "/api/v1/folders/", name=folder)["id"]
        import_chat(client, f"Filed {word}", folder_id=folder_id)
        import_chat(client, f"Loose {word}")

    page = page_for(account)
    dialog = open_search(page)
    search(dialog, word)
    expect(results(dialog)).to_have_count(2)
    dialog.get_by_role("button", name="Filters").click()
    page.get_by_role("button", name="Location").click()
    page.get_by_role("button", name=folder).click()

    expect(dialog.get_by_role("button", name="Remove location filter")).to_have_text(folder)
    expect(results(dialog)).to_have_count(1)
    expect(result(dialog, f"Filed {word}")).to_be_visible()


def start_a_new_conversation(page: Page, text: str) -> None:
    dialog = open_search(page)
    search(dialog, text)
    open_actions(dialog).get_by_role("button", name="Start a new conversation").click()


def test_start_a_new_conversation_sends_the_typed_text(page_for, make_user, upstream):
    question = f"How far can a {unique_word()} hop?"
    upstream.queue(reply.text("About two metres.", match=reply.answering(question)))
    page = page_for(make_user())

    start_a_new_conversation(page, question)
    expect(chat_input(page)).to_have_text(question)
    chat_input(page).click()
    page.keyboard.press("Enter")

    expect_reply(page, "About two metres.")
    expect(page.get_by_label("Chat Conversation").get_by_text(question)).to_be_visible()


@pytest.mark.regression
def test_start_a_new_conversation_keeps_an_ampersand(page_for, make_user):
    question = f"Q&A about the {unique_word()}"
    page = page_for(make_user())

    start_a_new_conversation(page, question)

    expect(
        chat_input(page),
        "the text after the & was dropped from the new chat's message box (#31469)",
    ).to_have_text(question)


# --------------------------------------------------------------------------- paging

PAGE_SIZE = 24


def test_more_matches_than_a_page_load_as_the_results_are_scrolled(page_for, make_user):
    account = make_user()
    word = unique_word()
    with account.client() as client:
        entries = [
            {
                "chat": {"title": f"Log {index:03d} {word}", "models": []},
                "created_at": LONG_AGO + index,
                "updated_at": LONG_AGO + index,
            }
            for index in range(PAGE_SIZE + 5)
        ]
        imported = client.post("/api/v1/chats/import", json={"chats": entries})
        assert imported.status_code == 200, imported.text

    page = page_for(account)
    dialog = open_search(page)
    search(dialog, word)
    expect(results(dialog)).to_have_count(PAGE_SIZE)
    expect(result(dialog, f"Log 000 {word}")).to_have_count(0)

    results(dialog).last.scroll_into_view_if_needed()

    expect(results(dialog)).to_have_count(PAGE_SIZE + 5)
    expect(result(dialog, f"Log 000 {word}")).to_be_visible()
