"""Regression: a tool's inline HTML embed vanished, or changed, when the HTML held entities.

Fix PR open-webui/open-webui#31390 (issue open-webui/open-webui#28085) in the chat view. A tool
that returns an HTMLResponse with `Content-Disposition: inline` shows its page as an embed under
the reply. The view decoded HTML entities in the embeds, the arguments and the file links, which
is only right for chats saved by older versions. So an entity such as `&quot;` broke the embed
list and nothing was shown, and `&amp;` or `&lt;` in the page arrived as a real `&` or `<`. The
embed now shows exactly what the tool returned.

A workspace tool returns a page with entities, the model calls it, and the embed is read from
inside its iframe.

Discriminates: passes on the dev a5bc78300 build; with the encoding of the embeds reverted (the
mutation build) the `&quot;` page shows no embed and the `&lt;b&gt;` page gains a real bold element.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from harness import upstream as reply
from harness.python_tools import python_tool
from utils.cached_chat import turn_on_tool
from utils.chat_ui import expect_reply, last_reply, send

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

TOOL_SOURCE = '''
from fastapi.responses import HTMLResponse


class Tools:
    def show_quoted(self) -> HTMLResponse:
        """Show the quoted page."""
        page = '<p id="probe" title="a &quot;b&quot;">He said &quot;hi&quot; &amp; left</p>'
        return HTMLResponse(content=page, headers={"Content-Disposition": "inline"})

    def show_escaped(self) -> HTMLResponse:
        """Show the escaped page."""
        page = '<p id="probe">Fish &amp;amp; chips &lt;b&gt;plain&lt;/b&gt;</p>'
        return HTMLResponse(content=page, headers={"Content-Disposition": "inline"})
'''


@pytest.fixture(scope="module")
def embed_tool(admin):
    with python_tool(admin, TOOL_SOURCE, name="Page embedder") as tool_id:
        yield tool_id


def call_tool(page: Page, upstream, method: str) -> None:
    turn_on_tool(page, "Page embedder")
    upstream.queue(
        reply.tool_call(method, {}, match=reply.answering("show the page")),
        reply.text("Page shown.", match=reply.answering("show the page")),
    )
    send(page, "show the page")
    expect_reply(page, "Page shown.")


def test_an_embed_with_quote_entities_is_shown_as_the_tool_wrote_it(
    page_for, make_user, upstream, embed_tool
):
    page = page_for(make_user())

    call_tool(page, upstream, "show_quoted")

    probe = last_reply(page).frame_locator("iframe").locator("#probe")
    expect(probe, "the tool's embed vanished").to_be_visible()
    expect(probe).to_have_text('He said "hi" & left')
    expect(probe).to_have_attribute("title", 'a "b"')


def test_an_embed_keeps_escaped_markup_as_text(page_for, make_user, upstream, embed_tool):
    page = page_for(make_user())

    call_tool(page, upstream, "show_escaped")

    probe = last_reply(page).frame_locator("iframe").locator("#probe")
    expect(probe, "the tool's embed vanished").to_be_visible()
    expect(probe).to_have_text("Fish &amp; chips <b>plain</b>")
    expect(probe.locator("b"), "escaped markup turned into a real element").to_have_count(0)
