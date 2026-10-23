"""Regression: the sidebar search missed a chat whose title is not plain English.

open-webui commit `26f37426b` (shipped in 0.11.2). SQLite's LIKE and LOWER() fold ASCII letters
only, so the search behind the sidebar's Search dialog did not find "ΑΘΗΝΑ" when the user typed
"αθηνα". Every SQLite connection now gets a `like` that lowercases both sides in Python.

Twin of unit/models/test_sqlite_unicode_case_insensitive_search.py.

Discriminates: passes on dev f5a26f53a; without the `like` override the dialog lists no result.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from utils.chat_ui import chat_input
from utils.search_dialog import open_search_from_sidebar, results, search

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


def _new_chat(client, title: str) -> None:
    chat = {"title": title, "models": [], "history": {"currentId": None, "messages": {}}}
    client.post("/api/v1/chats/new", json={"chat": chat}).raise_for_status()


def test_the_sidebar_search_finds_a_greek_title_typed_in_lowercase(page_for, make_user):
    account = make_user()
    with account.client() as client:
        _new_chat(client, "ΑΘΗΝΑ")
        _new_chat(client, "Weekly report")

    page = page_for(account)
    expect(chat_input(page)).to_be_visible()
    dialog = open_search_from_sidebar(page)
    search(dialog, "αθηνα")

    expect(results(dialog)).to_have_count(1)
    expect(results(dialog)).to_contain_text("ΑΘΗΝΑ")
