"""Journey: deferred tools run through `call_tool` while the offered tools never change.

With Tool Search on and tools large enough to defer, the model is offered `search_tools` and
`call_tool(tool_name, params)` alone; `params` is a JSON-encoded object string. Open WebUI unwraps
the dispatcher into the target tool before approvals and special handling, so the target keeps
its own behaviour (approval prompt, `ask_user` pause, access, MCP and builtin tools), and stores
the model's original call so later turns replay what the model actually sent, on Chat Completions
and the Responses API. A malformed dispatch comes back as a failed result starting `Error: ` and
the turn goes on. A tool already named `search_tools` or `call_tool`, a filter's included, turns
the dispatcher off. Added with dev 7b47c3b22.

Discriminates: in backend copies of dev 6276e81ce, unwrapping only at execution (not in the stream
handler) fails the run, replay, Responses, approval, ask_user, builtin and MCP tests; no unwrap at
all also fails every refused-dispatch case, the access, non-streamed resume and Ollama tests;
replaying the resolved target in place of the model's call fails the replay, Responses, approval,
ask_user and Ollama tests; leaving filter tools out of the name check fails the filter test; and
dropping the stored-item restore in the OpenAI router fails the relayed Responses test.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from typing import Iterator

import httpx
import pytest

from harness import responses_provider as responses_api
from harness import upstream as reply
from harness.actors import Actor
from harness.chat import ChatTurn, ask, send_message, wait_for_reply
from harness.mcp_server import TOOL_SERVERS, mcp_connection, serving_mcp
from harness.ollama_provider import OLLAMA_CONFIG, chat_stream, connect_ollama, serve_ollama
from harness.plugins import installed_function
from harness.python_tools import python_tool
from harness.second_provider import OPENAI_CONFIG
from harness.terminal_server import read_grant
from harness.upstream import Reply

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]

CHAT_CONFIG = ("/api/v1/chats/config", "/api/v1/chats/config")
DISPATCH_NAMES = {"search_tools", "call_tool"}
OLLAMA_MODEL = "llama3:latest"
WAIT = 20.0

HARBOUR_TOOLS = '''
class Tools:
    def harbour_lights(self, harbour: str) -> str:
        """Report whether the lights of a harbour are lit.

        :param harbour: Name of the harbour
        """
        return f"lit at {harbour}"
'''


@pytest.fixture
def chat_settings(admin, preserve):
    """`chat_settings(**changes)` updates the admin chat settings for the rest of the test."""
    preserve(CHAT_CONFIG)

    def change(**changes) -> None:
        with admin.client() as client:
            current = client.get(CHAT_CONFIG[0]).json()
            client.post(CHAT_CONFIG[1], json={**current, **changes}).raise_for_status()

    return change


@pytest.fixture
def tool_search(chat_settings):
    """Tool Search on with every tool deferred, so the model sees only the two dispatch tools."""
    chat_settings(
        ENABLE_TOOL_SEARCH=True,
        TOOL_SEARCH_DEFER_THRESHOLD=0,
        TOOL_SEARCH_ALWAYS_LOADED=[],
        TOOL_SEARCH_DEFER_BUILTIN_TOOLS=True,
    )
    return chat_settings


def dispatch_call(call_id: str, arguments: dict | str) -> dict:
    encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "call_tool", "arguments": encoded},
    }


def dispatch(tool_name: str, params: dict | str, call_id: str = "call_1") -> Reply:
    """The model calling `call_tool`, with `params` encoded the way the definition asks."""
    encoded = params if isinstance(params, str) else json.dumps(params)
    return Reply(tool_calls=[dispatch_call(call_id, {"tool_name": tool_name, "params": encoded})])


def offered(request: dict) -> list[str]:
    return [tool["function"]["name"] for tool in request.get("tools") or []]


def tool_messages(request: dict) -> dict[str, str]:
    return {
        entry["tool_call_id"]: entry["content"]
        for entry in request["messages"]
        if entry["role"] == "tool"
    }


def replayed_calls(request: dict) -> dict[str, dict]:
    return {
        call["id"]: call["function"]
        for entry in request["messages"]
        if entry["role"] == "assistant"
        for call in entry.get("tool_calls") or []
    }


def items(message: dict, kind: str) -> dict[str, dict]:
    return {item["call_id"]: item for item in message.get("output") or [] if item["type"] == kind}


def result_text(message: dict, call_id: str) -> str:
    return items(message, "function_call_output")[call_id]["output"][0]["text"]


def stored_output(client: httpx.Client, turn: ChatTurn) -> list[dict]:
    chat = client.get(f"/api/v1/chats/{turn.chat_id}").json()["chat"]
    return chat["history"]["messages"][turn.assistant_message_id].get("output") or []


def wait_for_status(client: httpx.Client, turn: ChatTurn, call_id: str, status: str) -> dict:
    deadline = time.monotonic() + WAIT
    calls: dict = {}
    while time.monotonic() < deadline:
        calls = {
            item.get("call_id"): item
            for item in stored_output(client, turn)
            if item.get("type") == "function_call"
        }
        if calls.get(call_id, {}).get("status") == status:
            return calls[call_id]
        time.sleep(0.1)
    raise AssertionError(f"{call_id} never reached {status}; the calls stood at {calls}")


def resolve(client: httpx.Client, turn: ChatTurn, call_id: str, action: str) -> None:
    resolved = client.post(
        f"/api/v1/chats/{turn.chat_id}/messages/{turn.assistant_message_id}/resolve",
        json={"call_id": call_id, "action": action},
    )
    assert resolved.status_code == 200, f"{action} {call_id}: {resolved.text}"


@contextlib.contextmanager
def private_tool(admin: Actor, source: str) -> Iterator[str]:
    """A workspace tool nobody but the admin may use."""
    tool_id = f"tool_{uuid.uuid4().hex[:8]}"
    with admin.client() as client:
        created = client.post(
            "/api/v1/tools/create",
            json={
                "id": tool_id,
                "name": "Private tools",
                "content": source,
                "meta": {"description": "Private tools"},
                "access_grants": [],
            },
        )
        assert created.status_code == 200, created.text
        try:
            yield tool_id
        finally:
            client.delete(f"/api/v1/tools/id/{tool_id}/delete")


def test_a_deferred_tool_runs_through_call_tool_and_later_turns_replay_the_dispatch(
    tool_search, admin, make_user, upstream
):
    sent_arguments = '{"tool_name":"harbour_lights",  "params":"{\\"harbour\\": \\"Pula\\"}"}'
    upstream.queue(
        reply.tool_call("search_tools", {"query": "harbour_lights"}),
        Reply(tool_calls=[dispatch_call("call_2", sent_arguments)]),
        reply.text("The lights are on."),
        reply.text("Still on."),
    )
    with python_tool(admin, HARBOUR_TOOLS) as tool_id, make_user().client() as client:
        turn, message = ask(client, "are the lights on in Pula?", tool_ids=[tool_id])
        ask(
            client,
            "and now?",
            tool_ids=[tool_id],
            chat_id=turn.chat_id,
            parent_id=turn.assistant_message_id,
        )

    first, after_search, after_call, next_turn = upstream.chat_requests()
    assert set(offered(first)) == DISPATCH_NAMES
    found = json.loads(tool_messages(after_search)["call_1"])["tools"]
    assert [tool["name"] for tool in found] == ["harbour_lights"]
    assert tool_messages(after_call)["call_2"] == "lit at Pula"
    assert message["content"] == "The lights are on."

    # the tools and the manifest stay byte for byte the same, so a prompt cache holds
    assert len({json.dumps(request["tools"]) for request in upstream.chat_requests()}) == 1
    manifests = {request["messages"][0]["content"] for request in upstream.chat_requests()}
    assert len(manifests) == 1 and "<available_tools>" in manifests.pop()

    shown = items(message, "function_call")["call_2"]
    assert (shown["name"], json.loads(shown["arguments"])) == (
        "harbour_lights",
        {"harbour": "Pula"},
    )
    assert shown["status"] == "completed"
    for request in (after_call, next_turn):
        assert replayed_calls(request)["call_2"] == {
            "name": "call_tool",
            "arguments": sent_arguments,
        }
    assert tool_messages(next_turn) == tool_messages(after_call)


REFUSED_DISPATCHES = [
    pytest.param(
        {"tool_name": "get_current_timestamp"},
        "Error: call_tool requires tool_name and params.",
        id="params-missing",
    ),
    pytest.param(
        {"tool_name": "get_current_timestamp", "params": "{}", "extra": 1},
        "Error: call_tool requires tool_name and params.",
        id="extra-key",
    ),
    pytest.param("[]", "Error: call_tool requires tool_name and params.", id="not-an-object"),
    pytest.param(
        {"tool_name": "", "params": "{}"},
        "Error: tool_name must be a non-empty string.",
        id="empty-tool-name",
    ),
    pytest.param(
        {"tool_name": "   ", "params": "{}"},
        "Error: tool_name must be a non-empty string.",
        id="blank-tool-name",
    ),
    pytest.param(
        {"tool_name": 7, "params": "{}"},
        "Error: tool_name must be a non-empty string.",
        id="tool-name-not-a-string",
    ),
    pytest.param(
        {"tool_name": "search_tools", "params": '{"query": "time"}'},
        "Error: call_tool cannot invoke search_tools or itself.",
        id="search-tools",
    ),
    pytest.param(
        {"tool_name": "call_tool", "params": "{}"},
        "Error: call_tool cannot invoke search_tools or itself.",
        id="itself",
    ),
    pytest.param(
        {"tool_name": "launch_rockets", "params": "{}"},
        'Error: Tool "launch_rockets" is not available. Search for an available tool.',
        id="unknown-tool",
    ),
    pytest.param(
        {"tool_name": "get_current_timestamp", "params": {}},
        "Error: params must be a JSON-encoded object string.",
        id="params-not-a-string",
    ),
    pytest.param(
        {"tool_name": "get_current_timestamp", "params": "[]"},
        "Error: params must decode to a JSON object.",
        id="params-not-an-object",
    ),
    pytest.param(
        {"tool_name": "get_current_timestamp", "params": "{broken"},
        "Error: ",
        id="params-malformed",
    ),
    pytest.param("{broken", "Error: ", id="arguments-malformed"),
]


@pytest.mark.parametrize(("arguments", "expected"), REFUSED_DISPATCHES)
def test_a_refused_dispatch_reaches_the_model_as_a_failed_result(
    tool_search, user, upstream, arguments, expected
):
    upstream.queue(Reply(tool_calls=[dispatch_call("call_1", arguments)]), reply.text("retrying"))
    with user.client() as client:
        _, message = ask(client, "use a tool")

    first, follow_up = upstream.chat_requests()
    sent_back = tool_messages(follow_up)["call_1"]
    if expected == "Error: ":
        assert sent_back.startswith(expected) and len(sent_back) > len(expected), sent_back
    else:
        assert sent_back == expected
    assert result_text(message, "call_1") == sent_back
    assert items(message, "function_call_output")["call_1"]["status"] == "failed"
    assert items(message, "function_call")["call_1"]["status"] == "failed"
    assert replayed_calls(follow_up)["call_1"]["name"] == "call_tool"
    assert follow_up["tools"] == first["tools"]
    assert message["content"] == "retrying"


def test_a_refused_dispatch_does_not_stop_the_other_and_both_replay_as_sent(
    tool_search, user, upstream
):
    bad = dispatch_call("call_bad", {"tool_name": "nope", "params": "{}"})
    good = dispatch_call("call_good", {"tool_name": "get_current_timestamp", "params": "{}"})
    upstream.queue(Reply(tool_calls=[bad, good]), reply.text("one of two"), reply.text("ok"))
    with user.client() as client:
        turn, message = ask(client, "what time is it?")
        ask(client, "thanks", chat_id=turn.chat_id, parent_id=turn.assistant_message_id)

    _, follow_up, next_turn = upstream.chat_requests()
    results = tool_messages(follow_up)
    assert (
        results["call_bad"] == 'Error: Tool "nope" is not available. Search for an available tool.'
    )
    assert "current_timestamp" in results["call_good"]
    assert message["content"] == "one of two"
    for request in (follow_up, next_turn):
        [assistant] = [
            entry
            for entry in request["messages"]
            if entry["role"] == "assistant" and entry.get("tool_calls")
        ]
        assert assistant["tool_calls"] == [bad, good]
    assert tool_messages(next_turn) == results


def test_the_responses_api_replays_the_dispatch_as_the_model_sent_it(
    tool_search, admin, preserve, listener
):
    preserve(OPENAI_CONFIG)
    dispatched = {"tool_name": "get_current_timestamp", "params": "{}"}
    with admin.client() as client:
        provider = responses_api.connect_responses(client, listener)
        provider.answer(
            responses_api.events_stream(
                *responses_api.function_call("call_tool", dispatched),
                responses_api.completed(),
            ),
            responses_api.events_stream(
                *responses_api.message("It is late."), responses_api.completed()
            ),
            responses_api.events_stream(
                *responses_api.message("Still late."), responses_api.completed()
            ),
        )
        turn, message = ask(client, "what time is it?", model=responses_api.RESPONSES_MODEL)
        ask(
            client,
            "and now?",
            model=responses_api.RESPONSES_MODEL,
            chat_id=turn.chat_id,
            parent_id=turn.assistant_message_id,
        )

    first, follow_up, next_turn = provider.sent()
    assert {tool["name"] for tool in first["tools"]} == DISPATCH_NAMES
    assert follow_up["tools"] == first["tools"] == next_turn["tools"]
    shown = items(message, "function_call")["call_1"]
    assert shown["name"] == "get_current_timestamp"
    assert "current_timestamp" in result_text(message, "call_1")
    for request in (follow_up, next_turn):
        [call] = [entry for entry in request["input"] if entry.get("type") == "function_call"]
        assert (call["name"], call["arguments"]) == ("call_tool", json.dumps(dispatched))
        [output] = [
            entry for entry in request["input"] if entry.get("type") == "function_call_output"
        ]
        assert "current_timestamp" in output["output"]
    assert message["content"] == "It is late."


def test_a_dispatched_tool_still_asks_for_approval(tool_search, make_user, upstream):
    tool_search(ENABLE_TOOL_PERMISSIONS=True)
    upstream.queue(dispatch("get_current_timestamp", {}), reply.text("It is late."))
    with make_user().client() as client:
        turn = send_message(client, "what time is it?", params={"tool_approval_mode": "ask"})
        pending = wait_for_status(client, turn, "call_1", "pending")
        assert pending["name"] == "get_current_timestamp"
        time.sleep(0.5)  # nothing runs while the approval is open
        assert len(upstream.chat_requests()) == 1
        resolve(client, turn, "call_1", "approve")
        message = wait_for_reply(client, turn)

    follow_up = upstream.chat_requests()[-1]
    assert "current_timestamp" in tool_messages(follow_up)["call_1"]
    assert replayed_calls(follow_up)["call_1"]["name"] == "call_tool"
    assert message["content"].endswith("It is late.")


def test_a_rejected_dispatch_never_runs(tool_search, make_user, upstream):
    tool_search(ENABLE_TOOL_PERMISSIONS=True)
    upstream.queue(dispatch("get_current_timestamp", {}), reply.text("Fine."))
    with make_user().client() as client:
        turn = send_message(client, "what time is it?", params={"tool_approval_mode": "ask"})
        wait_for_status(client, turn, "call_1", "pending")
        resolve(client, turn, "call_1", "reject")
        message = wait_for_reply(client, turn)

    assert (
        tool_messages(upstream.chat_requests()[-1])["call_1"]
        == "Error: tool call rejected by user."
    )
    assert items(message, "function_call")["call_1"]["status"] == "rejected"


def test_an_approved_dispatch_resumes_without_streaming(tool_search, make_user, upstream):
    tool_search(ENABLE_TOOL_PERMISSIONS=True)
    upstream.queue(dispatch("get_current_timestamp", {}), reply.text("It is late."))
    with make_user().client() as client:
        turn = send_message(client, "what time is it?", params={"tool_approval_mode": "ask"})
        wait_for_status(client, turn, "call_1", "pending")
        client.post(
            f"/api/v1/chats/{turn.chat_id}",
            json={"chat": {"params": {"stream_response": False, "tool_approval_mode": "ask"}}},
        ).raise_for_status()
        resolve(client, turn, "call_1", "approve")
        message = wait_for_reply(client, turn)

    follow_up = upstream.chat_requests()[-1]
    assert follow_up["stream"] is False
    assert "current_timestamp" in tool_messages(follow_up)["call_1"]
    assert replayed_calls(follow_up)["call_1"]["name"] == "call_tool"
    assert message["content"].endswith("It is late.")


def ask_user_question() -> dict:
    options = [{"label": "A", "description": "first"}, {"label": "B", "description": "second"}]
    return {"id": "q1", "question": "Which one?", "options": options}


def test_ask_user_through_call_tool_waits_for_the_answer(tool_search, make_user, upstream):
    tool_search(ENABLE_TOOL_PERMISSIONS=True)
    upstream.queue(
        dispatch("ask_user", {"questions": [ask_user_question()]}), reply.text("A it is.")
    )
    with make_user().client() as client:
        turn = send_message(client, "ask me something", params={"tool_approval_mode": "ask"})
        pending = wait_for_status(client, turn, "call_1", "pending")
        time.sleep(1)  # nothing more should reach the model while the question is open
        assert len(upstream.chat_requests()) == 1
        approved = client.post(
            f"/api/v1/chats/{turn.chat_id}/messages/{turn.assistant_message_id}/resolve",
            json={"call_id": "call_1", "action": "approve"},
        )
        assert approved.status_code == 400, "the question was treated as an approval prompt"
        resolve_answer = {"call_id": "call_1", "action": "answer", "answers": {"q1": ["A"]}}
        answered = client.post(
            f"/api/v1/chats/{turn.chat_id}/messages/{turn.assistant_message_id}/resolve",
            json=resolve_answer,
        )
        assert answered.status_code == 200, answered.text
        message = wait_for_reply(client, turn)

    arguments = json.loads(pending["arguments"])
    assert pending["name"] == "ask_user"
    assert arguments["timeout_ms"] == 120_000
    assert arguments["questions"][0]["options"][0] == {"label": "A", "description": "first"}
    follow_up = upstream.chat_requests()[-1]
    assert json.loads(tool_messages(follow_up)["call_1"])["answers"] == {"q1": ["A"]}
    assert replayed_calls(follow_up)["call_1"]["name"] == "call_tool"
    assert message["content"].endswith("A it is.")


def test_a_refused_ask_user_through_call_tool_matches_a_direct_call(tool_search, user, upstream):
    no_questions = {"questions": []}
    upstream.queue(
        reply.tool_call("ask_user", no_questions),
        reply.text("first"),
        dispatch("ask_user", no_questions),
        reply.text("second"),
    )
    with user.client() as client:
        _, direct = ask(client, "ask me something")
        _, dispatched = ask(client, "ask me something")

    direct_result = result_text(direct, "call_1")
    assert direct_result.startswith("Error:")
    assert result_text(dispatched, "call_1") == direct_result
    assert dispatched["content"] == "second"


def test_call_tool_reaches_only_tools_the_user_may_use_in_this_chat(
    tool_search, admin, make_user, upstream
):
    unavailable = 'Error: Tool "harbour_lights" is not available. Search for an available tool.'
    person = make_user()
    with (
        private_tool(admin, HARBOUR_TOOLS) as private_id,
        python_tool(admin, HARBOUR_TOOLS) as shared_id,
        person.client() as client,
    ):
        for tool_ids in ([private_id], []):
            upstream.queue(dispatch("harbour_lights", {"harbour": "Pula"}), reply.text("no"))
            _, message = ask(client, "are the lights on?", tool_ids=tool_ids)
            assert result_text(message, "call_1") == unavailable, tool_ids

        upstream.queue(dispatch("harbour_lights", {"harbour": "Pula"}), reply.text("yes"))
        _, message = ask(client, "are the lights on?", tool_ids=[shared_id])
    assert result_text(message, "call_1") == "lit at Pula"


FILTER_ADDS_TOOL = """
class Filter:
    async def inlet(self, body):
        parameters = {{"type": "object", "properties": {{}}}}
        spec = {{"name": "{name}", "description": "added by a filter", "parameters": parameters}}
        body["tools"] = [{{"type": "function", "function": spec}}]
        return body
"""


@pytest.mark.parametrize("name", ["call_tool", "search_tools"])
def test_a_filter_tool_with_a_dispatch_name_turns_tool_search_off(
    tool_search, admin, user, upstream, name
):
    with installed_function(admin, FILTER_ADDS_TOOL.format(name=name), is_global=True):
        upstream.queue(reply.text("ok"))
        with user.client() as client:
            ask(client, "hello")

    request = upstream.chat_requests()[-1]
    names = offered(request)
    assert names.count(name) == 1
    assert not DISPATCH_NAMES - {name} & set(names)
    assert "get_current_timestamp" in names
    assert "<available_tools>" not in json.dumps(request["messages"])


def test_a_filter_tool_with_another_name_keeps_tool_search_on(tool_search, admin, user, upstream):
    with installed_function(admin, FILTER_ADDS_TOOL.format(name="harbour_beacon"), is_global=True):
        upstream.queue(reply.text("ok"))
        with user.client() as client:
            ask(client, "hello")

    assert set(offered(upstream.chat_requests()[-1])) == DISPATCH_NAMES | {"harbour_beacon"}


def test_a_builtin_tool_writes_a_note_through_call_tool(tool_search, make_user, upstream):
    title = f"Harbour log {uuid.uuid4().hex[:6]}"
    upstream.queue(
        dispatch("write_note", {"title": title, "content": "lights lit at dusk"}),
        reply.text("Saved."),
    )
    with make_user().client() as client:
        _, message = ask(client, "note that the lights are lit")
        notes = client.get("/api/v1/notes/").json()

    assert items(message, "function_call")["call_1"]["name"] == "write_note"
    assert items(message, "function_call_output")["call_1"]["status"] == "completed"
    assert [note["title"] for note in notes] == [title]


def test_an_mcp_tool_runs_through_call_tool(tool_search, admin, preserve, make_user, upstream):
    preserve(TOOL_SERVERS)
    person = make_user()
    server_id = f"pier_{uuid.uuid4().hex[:8]}"
    with serving_mcp() as url:
        with admin.client() as client:
            saved = client.post(
                TOOL_SERVERS[1],
                json={
                    "TOOL_SERVER_CONNECTIONS": [
                        mcp_connection(url, server_id, [read_grant(person.id)])
                    ]
                },
            )
            assert saved.status_code == 200, saved.text
        upstream.queue(
            dispatch(f"{server_id}_echo", {"text": "the lamp is lit"}), reply.text("Echoed.")
        )
        with person.client() as client:
            _, message = ask(client, "echo it", tool_ids=[f"server:mcp:{server_id}"])

    first, follow_up = upstream.chat_requests()
    assert set(offered(first)) == DISPATCH_NAMES
    assert tool_messages(follow_up)["call_1"] == "the lamp is lit"
    assert items(message, "function_call")["call_1"]["name"] == f"{server_id}_echo"


def test_an_ollama_model_dispatches_through_call_tool(tool_search, admin, preserve, listener):
    preserve(OLLAMA_CONFIG)
    server = serve_ollama(listener, OLLAMA_MODEL)
    dispatched = {"tool_name": "get_current_timestamp", "params": "{}"}
    call = {"function": {"name": "call_tool", "arguments": dispatched}}
    with admin.client() as client:
        connect_ollama(client, listener)
        client.get("/api/models").raise_for_status()
        server.queue_chat(
            chat_stream(OLLAMA_MODEL, {"tool_calls": [call]}),
            chat_stream(OLLAMA_MODEL, {"content": "It is late."}),
        )
        _, message = ask(client, "what time is it?", model=OLLAMA_MODEL)

    first, follow_up = server.chat_requests()
    assert {tool["function"]["name"] for tool in first["tools"]} == DISPATCH_NAMES
    assert follow_up["tools"] == first["tools"]
    [stored] = items(message, "function_call").values()
    assert stored["name"] == "get_current_timestamp"
    assistant, tool = follow_up["messages"][-2:]
    assert [entry["function"] for entry in assistant["tool_calls"]] == [call["function"]]
    assert "current_timestamp" in tool["content"]
    assert message["content"] == "It is late."


def test_a_stored_dispatch_relayed_to_the_responses_api_keeps_the_models_call(
    admin, preserve, listener
):
    preserve(OPENAI_CONFIG)
    sent_arguments = '{"tool_name": "get_current_timestamp", "params": "{}"}'
    stored_call = {
        "type": "function_call",
        "id": "call_1",
        "call_id": "call_1",
        "name": "get_current_timestamp",
        "arguments": "{}",
        "status": "completed",
        "provider_function": {"name": "call_tool", "arguments": sent_arguments},
    }
    stored_result = {
        "type": "function_call_output",
        "id": "fco_1",
        "call_id": "call_1",
        "output": [{"type": "input_text", "text": "noon"}],
        "status": "completed",
    }
    with admin.client() as client:
        provider = responses_api.connect_responses(client, listener)
        provider.answer(
            responses_api.events_stream(*responses_api.message("Noon."), responses_api.completed())
        )
        relayed = client.post(
            "/openai/chat/completions",
            json={
                "model": responses_api.RESPONSES_MODEL,
                "stream": True,
                "messages": [
                    {"role": "user", "content": "what time is it?"},
                    {"role": "assistant", "content": "", "output": [stored_call, stored_result]},
                    {"role": "user", "content": "and now?"},
                ],
            },
        )
        assert relayed.status_code == 200, relayed.text

    [call] = [item for item in provider.sent()[-1]["input"] if item.get("type") == "function_call"]
    assert (call["name"], call["arguments"]) == ("call_tool", sent_arguments)
    assert "provider_function" not in call
