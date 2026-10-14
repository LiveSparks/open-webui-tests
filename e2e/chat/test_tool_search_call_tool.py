"""Journey: with Tool Search on, a tool the model runs through call_tool shows as that tool.

Dev 7b47c3b22 lets the model dispatch a deferred tool through `call_tool(tool_name, params)`. The
chat unwraps the call before approvals and execution, so the reply shows the target tool by name
with its own input and result (also after a reload), and an approval prompt names it. A call to
an unknown tool comes back as an error the reply continues past. The next turn replays the call
the model actually made, so the provider sees its own `call_tool` call in the history.

Discriminates: passes on dev 6276e81ce; in a backend copy without the streaming handler's unwrap
the display and approval tests fail (the reply shows call_tool), without the replay of the
model's original call the display test fails on the next turn's history, and without the unknown
tool check the failure test fails (a different error reaches the model and the chat).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Iterator

import pytest
from playwright.sync_api import Page, expect

from harness import upstream as reply
from harness.python_tools import EVERYONE_READS, python_tool
from harness.upstream import MOCK_MODEL_ID
from utils.chat_ui import REPLY_TIMEOUT_MS, chat_input, conversation, expect_reply, last_reply, send
from utils.model_selector import select_model

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

CHAT_CONFIG = ("/api/v1/chats/config", "/api/v1/chats/config")
REJECTED = "Error: tool call rejected by user."
SHOUTING_TOOL = '''
class Tools:
    def shout(self, text: str) -> str:
        """Shout the text."""
        return text.upper()
'''


@pytest.fixture
def tool_search_on(admin, preserve) -> None:
    """Tool Search defers every workspace tool and leaves the built-in ones loaded."""
    preserve(CHAT_CONFIG)
    with admin.client() as client:
        current = client.get(CHAT_CONFIG[0]).json()
        client.post(
            CHAT_CONFIG[1],
            json={
                **current,
                "ENABLE_TOOL_SEARCH": True,
                "TOOL_SEARCH_DEFER_THRESHOLD": 0,
                "TOOL_SEARCH_ALWAYS_LOADED": [],
                "TOOL_SEARCH_DEFER_BUILTIN_TOOLS": False,
            },
        ).raise_for_status()


@pytest.fixture
def tool_approval_on(admin, tool_search_on) -> None:
    with admin.client() as client:
        current = client.get(CHAT_CONFIG[0]).json()
        client.post(
            CHAT_CONFIG[1], json={**current, "ENABLE_TOOL_PERMISSIONS": True}
        ).raise_for_status()


@pytest.fixture
def shouter(admin) -> Iterator[str]:
    """A preset of the scripted model with the shouting tool attached; yields its name."""
    name = f"Shouter {uuid.uuid4().hex[:8]}"
    model_id = name.lower().replace(" ", "-")
    with python_tool(admin, SHOUTING_TOOL) as tool_id, admin.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": model_id,
                "base_model_id": MOCK_MODEL_ID,
                "name": name,
                "meta": {"toolIds": [tool_id]},
                "params": {},
                "access_grants": [EVERYONE_READS],
            },
        )
        assert created.status_code == 200, created.text
        yield name
        client.post("/api/v1/models/model/delete", json={"id": model_id})


def call_tool(tool_name: str, params: dict, prompt: str, call_id: str) -> reply.Reply:
    arguments = {"tool_name": tool_name, "params": json.dumps(params)}
    return reply.tool_call("call_tool", arguments, call_id=call_id, match=reply.answering(prompt))


def requests_for(upstream, prompt: str) -> list[dict]:
    return [body for body in upstream.chat_requests() if reply.answering(prompt)(body)]


def tool_results(upstream, prompt: str) -> list[str]:
    final = requests_for(upstream, prompt)[-1]
    return [str(entry["content"]) for entry in final["messages"] if entry["role"] == "tool"]


def expect_shout_shown(page: Page, word: str) -> None:
    group = last_reply(page).get_by_text("Explored search_tools, shout")
    expect(group).to_be_visible()
    group.click()
    last_reply(page).get_by_text("View Result from shout").click()
    shown = re.compile(rf"Input\s*text\s*{word}\s*Output\s*{word.upper()}")
    expect(last_reply(page)).to_contain_text(shown)
    expect(last_reply(page)).not_to_contain_text("call_tool")


def set_approval_mode(page: Page, mode: str) -> None:
    expect(chat_input(page)).to_be_visible()
    # the sidebar has a "More" button of its own
    page.locator("#input-menu-button").click()
    page.get_by_role("button", name="Tool Permissions").click()
    page.get_by_role("button", name=mode).click()
    page.keyboard.press("Escape")


def approval_button(page: Page, name: str):
    button = conversation(page).get_by_role("button", name=name, exact=True)
    expect(button).to_have_count(1, timeout=REPLY_TIMEOUT_MS)
    return button


def test_a_dispatched_call_shows_as_its_tool_and_is_replayed_as_sent(
    tool_search_on, page_for, make_user, upstream, shouter
):
    prompt = f"shout ahoy {uuid.uuid4().hex[:6]}"
    upstream.queue(
        reply.tool_call(
            "search_tools", {"query": "shout"}, call_id="call_s", match=reply.answering(prompt)
        ),
        call_tool("shout", {"text": "ahoy"}, prompt, "call_c"),
        reply.text("Shouted it.", match=reply.answering(prompt)),
    )
    page = page_for(make_user())
    select_model(page, shouter)
    send(page, prompt)
    expect_reply(page, "Shouted it.")

    offered = [tool["function"]["name"] for tool in requests_for(upstream, prompt)[0]["tools"]]
    assert {"search_tools", "call_tool"} <= set(offered) and "shout" not in offered
    assert tool_results(upstream, prompt)[-1] == "AHOY"
    expect_shout_shown(page, "ahoy")

    page.reload()
    expect_shout_shown(page, "ahoy")

    follow_up = f"and again {uuid.uuid4().hex[:6]}"
    upstream.queue(reply.text("Once more.", match=reply.answering(follow_up)))
    send(page, follow_up)
    expect_reply(page, "Once more.")
    [history] = requests_for(upstream, follow_up)
    calls = [
        call["function"]
        for entry in history["messages"]
        for call in entry.get("tool_calls") or []
        if call["id"] == "call_c"
    ]
    assert len(calls) == 1 and calls[0]["name"] == "call_tool", calls
    assert json.loads(calls[0]["arguments"]) == {
        "tool_name": "shout",
        "params": json.dumps({"text": "ahoy"}),
    }


def test_a_call_to_an_unknown_tool_shows_the_error_and_the_reply_goes_on(
    tool_search_on, page_for, make_user, upstream, shouter
):
    prompt = f"whisper hush {uuid.uuid4().hex[:6]}"
    upstream.queue(
        call_tool("whisper", {"text": "hush"}, prompt, "call_w"),
        reply.text("No whispering here.", match=reply.answering(prompt)),
    )
    page = page_for(make_user())
    select_model(page, shouter)
    send(page, prompt)
    expect_reply(page, "No whispering here.")

    error = 'Error: Tool "whisper" is not available. Search for an available tool.'
    assert tool_results(upstream, prompt) == [error]
    last_reply(page).get_by_text("View Result from call_tool").click()
    expect(last_reply(page)).to_contain_text('Tool "whisper" is not available')


def test_an_approval_prompt_names_the_dispatched_tool(
    tool_approval_on, page_for, make_user, upstream, shouter
):
    page = page_for(make_user())
    select_model(page, shouter)
    set_approval_mode(page, "Ask for approval")

    allowed = f"shout yes {uuid.uuid4().hex[:6]}"
    upstream.queue(
        call_tool("shout", {"text": "granted"}, allowed, "call_a"),
        reply.text("Allowed and shouted.", match=reply.answering(allowed)),
    )
    send(page, allowed)
    approval_button(page, "Allow")
    last_reply(page).get_by_text("Allow shout?").click()
    expect(last_reply(page)).to_contain_text(re.compile(r"Input\s*text\s*granted"))
    approval_button(page, "Allow").click()
    expect_reply(page, "Allowed and shouted.")
    assert tool_results(upstream, allowed) == ["GRANTED"]

    denied = f"shout no {uuid.uuid4().hex[:6]}"
    upstream.queue(
        call_tool("shout", {"text": "refused"}, denied, "call_d"),
        reply.text("Not shouted.", match=reply.answering(denied)),
    )
    send(page, denied)
    approval_button(page, "Deny")
    expect(last_reply(page)).to_contain_text("Allow shout?")
    approval_button(page, "Deny").click()
    expect_reply(page, "Not shouted.")
    expect(last_reply(page)).to_contain_text("Denied shout")
    assert tool_results(upstream, denied)[-1:] == [REJECTED]
