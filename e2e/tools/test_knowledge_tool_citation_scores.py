"""Regression: a citation from the model's knowledge search showed no relevance percentage.

Fix PR open-webui/open-webui#31307 (merged as `1c813902e`, issue open-webui/open-webui#29776)
in `utils/middleware.py`. With native function calling the citations built from
`query_knowledge_files` dropped the distance each chunk came back with, so opening one showed
the cited text without the relevance badge that classic retrieval shows for the same knowledge
base. A person reads the badge in the citation dialog.

Twin of integration/tools/test_knowledge_tool_citation_scores.py.

Discriminates: passes on dev bc2416c5d; with the fix reverted in a backend copy and the dev
build unchanged, the citation dialog shows the text with no relevance percentage.
"""

from __future__ import annotations

import re
import uuid

import pytest
from playwright.sync_api import expect

from harness import upstream as reply
from harness.knowledge_bases import add_text_file, knowledge_base
from utils.chat_ui import conversation, expect_reply, listed_source, send

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

NOTES = "Grey herons nest in colonies near water and hunt fish in the shallows."
PERCENTAGE = re.compile(r"\d+\.\d\d%")
PROMPT = "search my notes for herons"


@pytest.fixture
def shared_notes(admin, make_user):
    """A knowledge base holding one file, readable by a fresh account."""
    reader = make_user()
    tag = uuid.uuid4().hex[:8]
    grant = [{"principal_type": "user", "principal_id": reader.id, "permission": "read"}]
    filename = f"herons-{tag}.txt"
    with admin.client() as client, knowledge_base(client, f"Birds {tag}", grant) as knowledge_id:
        add_text_file(client, knowledge_id, filename, NOTES)
        yield reader, knowledge_id, filename


def test_a_knowledge_tool_citation_shows_its_relevance(page_for, shared_notes, upstream):
    reader, knowledge_id, filename = shared_notes
    arguments = {"query": "heron", "knowledge_ids": [knowledge_id]}
    upstream.queue(
        reply.tool_call("query_knowledge_files", arguments, match=reply.answering(PROMPT)),
        reply.text("Herons nest in colonies.", match=reply.answering(PROMPT)),
    )
    page = page_for(reader)

    send(page, PROMPT)
    expect_reply(page, "Herons nest in colonies.")

    chat = conversation(page)
    chat.get_by_role("button", name="Toggle 1 source").click()
    listed_source(page, filename).click()
    citation = page.get_by_role("dialog")
    expect(citation).to_contain_text(NOTES)
    expect(citation.get_by_text(PERCENTAGE)).to_be_visible()
