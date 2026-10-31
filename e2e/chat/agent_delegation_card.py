"""The PR9 headline in the browser: a delegation card opens, the child's answer
lands in it, and a sub-agent result row appears for the background flow.

Drives one live delegation per surface the plan's "You see" box names: the
foreground delegation card (the parent's `delegate_task` call rendered with its
answer) and the background row (`SubagentResultRow`) the pending flow lands on
the parent chat. Every task names its own marker and the assertions read the
rendered card and row back, so the proof is the UI, not the API.

Note: the card renders under the loopback tool name
(`View Result from mcp__owui__delegate_task`); the frontend's
`Sub-agent: "<task>"` card renaming in structuredOutput.ts matches only the
bare `delegate_task`, which the loopback bridge never carries. The card still
opens to the child's answer, so the proof names the raw tool name and the
rename gap rides in the module docstring.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from integration.chat.agent_subagents import (  # noqa: F401 the shared lane fixtures
    agent_model,
    delegation_script,
    sub_admin,
    subagents_instance,
    subagents_on,
    sidecar,
)
from utils.chat_ui import expect_reply, send
from utils.model_selector import select_model

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

SHOTS = Path("/tmp/swarm-pr9/owner")


def _shot(page: Page, slug: str) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = str(SHOTS / slug)
    page.screenshot(path=path, full_page=True)
    return path


def delegation_card(page: Page, tool: str) -> Locator:
    """The done tool-call card for the delegation, by its loopback tool name."""
    return page.get_by_role("button", name=re.compile(f"View Result from {tool}")).first


def test_a_foreground_delegation_card_opens_and_carries_the_child_answer(
    sub_admin, subagents_on, agent_model, page_for
):
    task = f"browser card child {uuid.uuid4().hex[:8]} answer me"
    subagents_on()
    page = page_for(sub_admin, locale="en-US")
    select_model(page, "Agent Subagents Test")
    send(page, delegation_script(task, "browser-stage"))
    expect_reply(page, "echoed:")

    # Two calls ran (the tool search then the delegation), so the rendered
    # detail group folds them under the 'Explored ...' toggle first.
    group = page.get_by_role("button").filter(has_text=re.compile(r"Explor(ed|ing)")).first
    expect(group).to_be_visible()
    group.click()

    card = delegation_card(page, "mcp__owui__delegate_task")
    expect(card).to_be_visible()
    card.click()
    expanded = page.get_by_text(f"echoed: {task}", exact=False).first
    expect(expanded).to_be_visible()
    _shot(page, "delegation-card.png")


def test_a_background_delegation_lands_a_result_row(sub_admin, subagents_on, agent_model, page_for):
    task = f"browser row child {uuid.uuid4().hex[:8]} answer me"
    subagents_on()
    page = page_for(sub_admin, locale="en-US")
    select_model(page, "Agent Subagents Test")
    send(page, delegation_script(task, "browser-stage", background=True))
    expect_reply(page, "dispatched")

    row = page.get_by_role("button", name=re.compile("Background sub-agent finished")).first
    row.wait_for(state="visible", timeout=60_000)
    row.click()
    expanded = page.get_by_text("[ASYNC SUBAGENT COMPLETE", exact=False).first
    expect(expanded).to_be_visible()
    _shot(page, "subagent-result-row.png")
