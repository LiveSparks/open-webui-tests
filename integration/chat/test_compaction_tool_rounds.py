"""A reply that ran many tool calls in a row could grow past the model's context.

Dev 67cfa917c (issue open-webui/open-webui#27599). With context compaction on and native
function calling, the context was compacted only before a reply started, so a reply that kept
calling tools sent every earlier round again on each round. Now, once the context passes the token
threshold between two tool rounds, the older rounds are summarized and the next request carries
the user's question, the summary as a system message and the recent rounds. The cut falls only
in front of an assistant message that calls tools, so every call stays with its results, and the
summary prompt now lists each call's tool name and arguments. Below the threshold, or with
compaction switched off, the whole run is sent on every round as before.

The provider's prefix cache only survives requests that append: here that holds for every pair
of requests except the one right after a summary was made.

Calling tools in legacy mode happens before the reply starts, so it has no rounds to compact and
gets no test here (the suite adds none for legacy mode).

Discriminates: in a backend copy of dev 436f06ca5 with 67cfa917c reverted, no summary is made
inside the reply and every test fails (the threshold-off cases on their control run, the same run
over the threshold, which must be compacted before the quiet run counts).
"""

from __future__ import annotations

import itertools
import json
import uuid

import httpx
import pytest

from harness import upstream as reply
from harness.chat import ChatTurn, ask
from harness.prompt_caching import prefix_break
from harness.python_tools import python_tool
from harness.upstream import Reply

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

CHAT_CONFIG = ("/api/v1/chats/config", "/api/v1/chats/config")
# two rounds of two results stay under it, a third round passes it
THRESHOLD = 2_400
ROUNDS = 6
ANSWER = "The harbour log is clean."
SUMMARIES = ("SUMMARY OF ROUNDS ONE TO THREE", "SUMMARY OF ROUNDS THREE TO FIVE")
CALL_IDS = itertools.count(1)

TOOL_SOURCE = '''
class Tools:
    def read_log(self, page: str) -> str:
        """Read one page of the harbour log."""
        return "entry " * 300
'''


@pytest.fixture(scope="module")
def log_tool(admin):
    with python_tool(admin, TOOL_SOURCE, name="Harbour log") as tool_id:
        yield tool_id


@pytest.fixture
def compaction(admin, preserve):
    """`compaction(enable, threshold)`: the chat settings, keeping 10% so a cut lands mid-round."""
    preserve(CHAT_CONFIG)

    def configure(enable: bool = True, threshold: int = THRESHOLD) -> None:
        with admin.client() as client:
            current = client.get(CHAT_CONFIG[0]).json()
            updated = client.post(
                CHAT_CONFIG[1],
                json={
                    **current,
                    "ENABLE_CONTEXT_COMPACTION": enable,
                    "CONTEXT_COMPACTION_TOKEN_THRESHOLD": threshold,
                    "CONTEXT_COMPACTION_TOKEN_CAP": threshold,
                    "CONTEXT_COMPACTION_RETENTION_PERCENTAGE": 10,
                },
            )
        assert updated.status_code == 200, updated.text

    return configure


def is_summary_request(body: dict) -> bool:
    return not body.get("stream")


def answering(prompt: str):
    """The chat's own requests only: the summary prompt quotes the question too."""
    asks_about = reply.answering(prompt)
    return lambda body: bool(body.get("stream")) and asks_about(body)


def page_name(number: int) -> str:
    return f"harbour-page-{number}"


def tool_round(number: int) -> Reply:
    """Round `number` reads two log pages at once."""
    calls = [
        reply.tool_call(
            "read_log", {"page": page_name(page)}, call_id=f"call_{next(CALL_IDS)}"
        ).tool_calls[0]
        for page in (2 * number - 1, 2 * number)
    ]
    return Reply(tool_calls=calls)


def long_tool_run(client: httpx.Client, upstream, tool_id: str) -> tuple[ChatTurn, dict]:
    prompt = f"read the whole harbour log [{uuid.uuid4().hex[:6]}]"
    for scripted in [*(tool_round(number) for number in range(1, ROUNDS + 1)), reply.text(ANSWER)]:
        scripted.match = answering(prompt)
        upstream.queue(scripted)
    upstream.queue(*(reply.text(summary, match=is_summary_request) for summary in SUMMARIES))
    return ask(client, prompt, tool_ids=[tool_id], timeout=90)


def provider_rounds(upstream) -> list[tuple[dict, bool]]:
    """Each streamed request, and whether a summary was made since the request before it."""
    rounds, summarized = [], False
    for body in upstream.chat_requests():
        if is_summary_request(body):
            summarized = True
            continue
        rounds.append((body, summarized))
        summarized = False
    return rounds


def summary_requests(upstream) -> list[dict]:
    return [body for body in upstream.chat_requests() if is_summary_request(body)]


def called_pages(messages: list[dict]) -> list[str]:
    return [
        json.loads(call["function"]["arguments"])["page"]
        for message in messages
        for call in message.get("tool_calls") or []
    ]


def unpaired(messages: list[dict]) -> list[str]:
    """Calls sent without their result and results sent without their call."""
    problems, open_calls = [], set()
    for message in messages:
        if message["role"] == "tool":
            if message.get("tool_call_id") not in open_calls:
                problems.append(f"result {message.get('tool_call_id')} without its call")
            open_calls.discard(message.get("tool_call_id"))
            continue
        problems += [f"call {call_id} without its result" for call_id in sorted(open_calls)]
        open_calls = {call["id"] for call in message.get("tool_calls") or []}
    return problems + [f"call {call_id} without its result" for call_id in sorted(open_calls)]


def test_a_long_tool_run_is_compacted_between_rounds(user, upstream, compaction, log_tool):
    compaction()
    with user.client() as client:
        turn, message = long_tool_run(client, upstream, log_tool)

    rounds = provider_rounds(upstream)
    assert len(rounds) == ROUNDS + 1
    assert [summarized for _, summarized in rounds] == [
        False,
        False,
        False,
        True,
        False,
        True,
        False,
    ], "the reply was not compacted after its third and fifth rounds"
    fourth = rounds[3][0]["messages"]
    assert [entry["role"] for entry in fourth] == ["system", "user", "assistant", "tool", "tool"]
    assert fourth[0]["content"] == f"[CONVERSATION SUMMARY]\n{SUMMARIES[0]}"
    assert fourth[1]["content"].startswith("read the whole harbour log")
    assert called_pages(fourth) == [page_name(5), page_name(6)]
    last = rounds[-1][0]["messages"]
    assert last[0]["content"] == f"[CONVERSATION SUMMARY]\n{SUMMARIES[1]}"
    assert called_pages(last) == [page_name(page) for page in range(9, 13)]

    assert ANSWER in message["content"]
    calls = [item for item in message["output"] if item["type"] == "function_call"]
    assert len(calls) == 2 * ROUNDS, "the stored reply lost rounds the request left out"


def test_each_tool_call_stays_with_its_result(user, upstream, compaction, log_tool):
    compaction()
    with user.client() as client:
        long_tool_run(client, upstream, log_tool)

    rounds = provider_rounds(upstream)
    assert any(summarized for _, summarized in rounds), "the reply was never compacted"
    for number, (body, _) in enumerate(rounds, start=1):
        assert unpaired(body["messages"]) == [], f"request {number}"


def test_the_summary_request_names_the_tools_and_arguments(user, upstream, compaction, log_tool):
    compaction()
    with user.client() as client:
        long_tool_run(client, upstream, log_tool)

    first, second = (body["messages"][-1]["content"] for body in summary_requests(upstream))
    assert "read_log" in first
    for page in range(1, 7):
        assert page_name(page) in first, f"page {page} is missing from the summary prompt"
    assert SUMMARIES[0] in second, "the second summary was not given the first one"
    assert page_name(9) in second


def test_between_compactions_requests_only_append(user, upstream, compaction, log_tool):
    compaction()
    with user.client() as client:
        long_tool_run(client, upstream, log_tool)

    rounds = provider_rounds(upstream)
    appended_after_a_summary = False
    for number in range(1, len(rounds)):
        (earlier, earlier_summarized), (later, summarized) = rounds[number - 1], rounds[number]
        if summarized:
            continue
        broken = prefix_break(earlier, later)
        assert broken is None, f"request {number + 1} of {len(rounds)}: {broken}"
        appended_after_a_summary |= earlier_summarized
    assert appended_after_a_summary, "no request followed a compacted one without a summary"


@pytest.mark.parametrize(
    "enable,threshold",
    [
        pytest.param(True, 1_000_000, id="under-threshold"),
        pytest.param(False, THRESHOLD, id="switched-off"),
    ],
)
def test_without_passing_an_active_threshold_the_run_is_sent_whole(
    user, upstream, compaction, log_tool, enable, threshold
):
    compaction()
    with user.client() as client:
        long_tool_run(client, upstream, log_tool)
        assert summary_requests(upstream), "control: the same run over the threshold was not compacted"
        upstream.reset()
        compaction(enable=enable, threshold=threshold)
        _, message = long_tool_run(client, upstream, log_tool)

    assert summary_requests(upstream) == []
    requests = [body for body, _ in provider_rounds(upstream)]
    for number, (earlier, later) in enumerate(zip(requests, requests[1:]), start=2):
        assert prefix_break(earlier, later) is None, f"request {number}"
    assert called_pages(requests[-1]["messages"]) == [page_name(page) for page in range(1, 13)]
    assert ANSWER in message["content"]

