"""Every arm that abandons an agent run tells the page, through the translator.

This is the PR14 rehoming of the intent of unit/chat/test_middleware_stream_assembly.py
(the AST audit over `utils/middleware.py`'s abandoning follow-up arms, retired with the
PR14 deletion round): the arms that abandon a run now live on the pi side, so the audit
drives the module that owns them. Two failure surfaces must still speak:

- `PiEventTranslator`: a run that ends in failure (`error` event, or a `done` frame with
  `reason=error`) closes with a `response.failed` frame whose `error.content` carries the
  message — `agent_chat_response_handler` persists that through the ordinary error paths
  (`chat:message:error` + the stored error) — and an aborted run closes without a
  fabricated error, so stop keeps partial content.
- `generate_agent_chat_completion`'s stream arms: the `HTTPException` and generic
  exception arms inside `sse_stream` frame the failure the same way and still terminate
  the SSE body materially (`data: [DONE]`), the setup arm outside the stream raises the
  ordinary 502, and the prompt stream's own EOF guard refuses to pass a clean disconnect
  off as a completed turn.

Discriminates: each assertion fails when its arm is mutated to swallow the failure (drop
the `response.failed` yield, drop the terminating `[DONE]`, or let the exception
propagate out of the body instead of framing it).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

pytestmark = pytest.mark.regression

CHAT_ID = "pr14-translator-audit-chat"


@pytest.fixture(scope="session")
def agent_module(owui_module):
    return owui_module("open_webui.routers.agent")


def _collect(chunks: list[str]) -> list[dict]:
    frames = []
    done = False
    for chunk in "".join(chunks).split("\n\n"):
        line = chunk.strip()
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            frames.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
    assert done, "the SSE body did not terminate with `data: [DONE]`, so a page left mid-answer sees no end"
    return frames


def _failed_contents(frames: list[dict]) -> list[str]:
    return [
        frame["response"]["error"]["content"]
        for frame in frames
        if isinstance(frame, dict)
        and isinstance(frame.get("response"), dict)
        and isinstance(frame["response"].get("error"), dict)
        and isinstance(frame["response"]["error"].get("content"), str)
    ]


def _agent_request():
    """The narrow request surface `generate_agent_chat_completion` reads."""
    return SimpleNamespace(state=SimpleNamespace(max_tool_call_iterations=256))


def _form_data():
    return {
        "model": "pr14-audit-model",
        "metadata": {"chat_id": CHAT_ID},
        "messages": [{"role": "user", "content": "hello"}],
    }


async def _translated(agent_module, event: str, data: dict) -> list[dict]:
    """Translate one event the dispatch loop does, then close the run."""
    translator = agent_module.PiEventTranslator()
    yielded = [line async for line in translator.translate(event, data)]
    yielded.extend(translator.flush_lines())
    yielded.append(translator.final_line())
    return _collect(yielded)


@pytest.mark.asyncio
async def test_a_failed_run_closes_with_a_reported_error(agent_module):
    """The `error` event arm: the failure message reaches the final failed frame."""
    frames = await _translated(agent_module, "error", {"type": "error", "error": "boom: provider died"})
    assert _failed_contents(frames) == ["boom: provider died"], _failed_contents(frames)


@pytest.mark.asyncio
async def test_a_run_that_finishes_failed_closes_with_a_reported_error(agent_module):
    """The `done` `reason=error` arm: the same frame, from the prompt result."""
    frames = await _translated(agent_module, "done", {"type": "done", "reason": "error", "error": "cap: turn budget"})
    assert _failed_contents(frames) == ["cap: turn budget"], _failed_contents(frames)


@pytest.mark.asyncio
async def test_an_aborted_run_closes_without_a_fabricated_error(agent_module):
    """The `reason=aborted` arm: stop keeps partial content; no invented failure."""
    translator = agent_module.PiEventTranslator()
    frames = await _translated(agent_module, "done", {"type": "done", "reason": "aborted"})
    assert translator.aborted is True
    assert _failed_contents(frames) == [], _failed_contents(frames)


@pytest.fixture()
def bridge_ready(agent_module, monkeypatch):
    """Patch away the session/bridge setup so the stream arms are reachable.

    Port 15014 is this audit's dead sidecar port; no setup call goes through."""
    monkeypatch.setenv("AGENT_SIDECAR_PORT", "15014")

    async def _session(client, chat_id, system):
        return {"running": False, "userTurns": 100}

    async def _head_section():
        "A prompt-head section stand-in, so no Config store is touched."
        return "## Code execution\n\n(audit head section)"

    monkeypatch.setattr(agent_module, "ensure_session", _session)
    monkeypatch.setattr(agent_module, "mint_session_binding", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent_module, "_code_interpreter_head_section", _head_section)

    # The dispatch imports the turn envelope at the source module; patch there.
    import open_webui.routers.agent_runtime as agent_runtime

    async def _turn_envelope(request, user, form_data, metadata):
        return {"model": "fake/fake-model", "headers": {}}

    monkeypatch.setattr(agent_runtime, "build_turn_envelope", _turn_envelope)
    return agent_module


def _text_delta(contentIndex: int = 0) -> tuple:
    return (
        "message_update",
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": contentIndex, "delta": "x"}},
    )


async def _driven_body(agent_module, events):
    """Build the agent response with `stream_sidecar_events` replaced.

    `events` carries (event, data) tuples with one exception mixed in, since the
    arms under audit live in the `except` clauses around the stream loop."""

    async def stream(*args, **kwargs):
        for event in events:
            if isinstance(event, Exception):
                raise event
            yield event

    with patch.object(agent_module, "stream_sidecar_events", stream):
        response = await agent_module.generate_agent_chat_completion(_agent_request(), _form_data(), None)
    return _collect([chunk async for chunk in response.body_iterator])


@pytest.mark.asyncio
async def test_a_generic_exception_in_the_stream_frames_the_failure(bridge_ready):
    """The generic-exception arm: framed (`agent sidecar error: ...`), and the
    body still terminates with [DONE]."""
    frames = await _driven_body(
        bridge_ready,
        [_text_delta(), RuntimeError("socket died mid-run")],
    )
    contents = _failed_contents(frames)
    assert contents == ["agent sidecar error: socket died mid-run"], contents


@pytest.mark.asyncio
async def test_a_stream_refusal_frames_its_detail(bridge_ready):
    """The `HTTPException` arm: a sidecar refusal mid-run frames its detail."""
    frames = await _driven_body(
        bridge_ready,
        [_text_delta(), bridge_ready.HTTPException(status_code=507, detail="session refused the prompt")],
    )
    contents = _failed_contents(frames)
    assert contents == ["session refused the prompt"], contents


@pytest.mark.asyncio
async def test_the_run_result_frames_a_failed_prompt(bridge_ready):
    """The clean-run arm: the `done` result frame's outcome frames the close
    (failure crosses; abort does not)."""
    frames = await _driven_body(
        bridge_ready,
        [_text_delta(), ("done", {"reason": "error", "error": "cap: turn budget"})],
    )
    assert _failed_contents(frames) == ["cap: turn budget"], _failed_contents(frames)


@pytest.mark.asyncio
async def test_a_setup_failure_reports_through_the_status(agent_module, monkeypatch):
    """The setup arm outside the stream: unreachable sidecar = the ordinary 502."""
    monkeypatch.setenv("AGENT_SIDECAR_PORT", "15014")

    async def dead_session(client, chat_id, system):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(agent_module, "ensure_session", dead_session)
    with pytest.raises(agent_module.HTTPException) as excinfo:
        await agent_module.generate_agent_chat_completion(_agent_request(), _form_data(), None)
    assert excinfo.value.status_code == 502
    assert "agent sidecar unreachable" in excinfo.value.detail


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, json=None, timeout=None):
        return _FakeStreamCtx(self._lines)


@pytest.mark.asyncio
async def test_a_prompt_stream_that_ends_without_done_is_a_disconnection(agent_module):
    """The EOF arm in the stream wrapper: a clean-but-frameless close raises the
    mid-run disconnection instead of passing as a completed turn."""
    with pytest.raises(agent_module.HTTPException) as excinfo:
        async for _ in agent_module.stream_sidecar_events(_FakeClient([]), CHAT_ID, "hello"):
            pass
    assert excinfo.value.status_code == 502
    assert "agent sidecar disconnected mid-run" in excinfo.value.detail
