"""Journey: a chat with Web Search on cites the page the model read, and only that page.

A person switches Web Search on in the chat input's Integrations menu and asks a question. The
model searches with `search_web` and reads one of the hits with `fetch_url`; the reply lists that
page as its one source, its inline marker names the page's site, and opening it shows the page's
text with a link to the page. The search hits the model never opened are not offered as sources
(open-webui/open-webui#29631). The search engine and the page are a local service, so the
instance runs with local fetching allowed.

Discriminates: passes on dev 176d31d1d; in a frontend copy, the source dialog taking marker `[n]`
as the n+1th source left the marker opening nothing; in a backend copy, `fetch_url` left out of the
tools whose results are cited turned the reply sourceless.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from harness import upstream as reply
from harness.actors import admin_of, create_user
from harness.listener import text_answer
from harness.web_retrieval import (
    LOCAL_WEB_FETCH,
    save_web_settings,
    serve_search_results,
    web_settings_restored,
)
from utils.chat_ui import chat_input, conversation, expect_reply, last_reply, send

pytestmark = [
    pytest.mark.journey,
    pytest.mark.requires_browser,
    pytest.mark.requires_source,
    pytest.mark.slow,
]

QUESTION = "How many herons are at the jetty?"
PAGE_TEXT = "Two grey herons stand at the jetty every morning."
PAGE = f"<html><body><h1>Heron watch</h1><p>{PAGE_TEXT}</p></body></html>"


@pytest.fixture
def searcher(instance_with, listener):
    """The local-fetch instance searching the listener, whose hits are the page and one more."""
    searching = instance_with(LOCAL_WEB_FETCH)
    if not searching.serves_frontend:
        pytest.skip("no built frontend (set OPEN_WEBUI_BUILD_DIR)")
    listener.route("GET", "/herons", text_answer(PAGE))
    hits = [f"{listener.base_url}/herons", f"{listener.base_url}/egrets"]
    with admin_of(searching).client() as client, web_settings_restored(client):
        save_web_settings(client, **serve_search_results(listener, hits))
        yield searching


def test_the_page_the_model_read_is_the_reply_source(page_for, searcher, listener):
    page_url = f"{listener.base_url}/herons"
    answering = reply.answering(QUESTION)
    searcher.upstream.queue(
        reply.tool_call("search_web", {"query": "herons jetty"}, "call_search", match=answering),
        reply.tool_call("fetch_url", {"url": page_url}, "call_fetch", match=answering),
        reply.text("Two herons [1].", match=answering),
    )
    page = page_for(create_user(searcher))
    expect(chat_input(page)).to_be_visible()

    page.get_by_role("button", name="Integrations", exact=True).last.click()
    page.get_by_role("menu").get_by_role("button", name="Web Search").click()
    page.keyboard.press("Escape")
    send(page, QUESTION)
    expect_reply(page, "Two herons")

    assert listener.requests_to("/search"), "the model's search never reached the engine"
    chat = conversation(page)
    expect(chat.get_by_role("button", name="Toggle 1 source")).to_be_visible()
    site = page_url.split("/")[2]
    marker = last_reply(page).get_by_role("button", name=f"View source: {site}")
    marker.click()
    citation = page.get_by_role("dialog")
    expect(citation.get_by_role("link", name=page_url, exact=True)).to_be_visible()
    expect(citation).to_contain_text(PAGE_TEXT)
    citation.get_by_role("button", name="Close citation modal").click()

    chat.get_by_role("button", name="Toggle 1 source").click()
    listed = chat.get_by_role("button", name=re.compile(r"^View source: \d+ http"))
    expect(listed).to_have_count(1)
    expect(listed).to_have_accessible_name(f"View source: 1 {page_url}")
