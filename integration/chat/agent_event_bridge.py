"""The event bridge translates pi's session stream into the chat envelope.

PR3 of the pi-engine program (issue 6): a chat routed to the agent sidecar
answers through the ordinary Socket.IO `events` envelope with
`chat:completion` Responses-style output items, stop posts to the sidecar
abort route and flushes a done frame, a send during an active run steers
instead of interrupting, edit-resend forks the sidecar session, and a dead
provider lands as `chat:message:error` with a usable done state.

Every reply is scripted by `agent_sidecar/openai_fake.py`, so each test
asserts the roundtrip it scripted rather than a canned constant.

The steer race (issue 32) is staged, not timed: the bridge's running check
and its steer post are two calls, so a run can end in between and the
sidecar steer route answers 409. Those tests make the first check answer
stale and let everything downstream — the 409, the recheck, the prompt
path — hit the real sidecar.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from harness.chat import ChatTurn, ask, send_message, wait_for_reply
from harness.upstream import MOCK_MODEL_ID
from harness.instance import LaunchedInstance
from harness.actors import admin_of
from harness.socket_client import SocketSession, connected

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

AGENT_MODEL_ID = "agent-loop.test"
SIDECAR_PORT = 8942
LANE_DIR = "/tmp/agent_event_bridge-lane"
TESTS_REPO = Path(__file__).resolve().parents[2]


def _lane_env() -> dict:
    return {**os.environ, "AGENT_SIDECAR_LANE_DIR": LANE_DIR}


def _boot_sidecar() -> None:
    checkout = Path(os.environ.get("OPEN_WEBUI_CHECKOUT", ""))
    if not checkout.is_dir():
        candidate = TESTS_REPO.parent / "open-webui"
        assert (candidate / "sidecar").is_dir(), (
            "set OPEN_WEBUI_CHECKOUT to the open-webui worktree whose sidecar dist the lane builds"
        )
        checkout = candidate
    subprocess.run(
        [str(TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"), "start", str(checkout), str(SIDECAR_PORT)],
        check=True,
        env=_lane_env(),
    )


@pytest.fixture(scope="module")
def sidecar() -> Iterator[dict]:
    """The scripted fake provider plus the built sidecar, from the boot recipe."""
    boot = TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"
    assert boot.is_file(), f"boot recipe missing at {boot}"
    _boot_sidecar()
    yield {"port": SIDECAR_PORT}
    subprocess.run([str(boot), "stop"], check=False, env=_lane_env())


def _fake_connection(port: int) -> dict:
    """PR6 resolves every turn's provider from OWUI's own connection, so the
    connection must name the same scripted fake the sidecar's models.json
    points at (the boot recipe derives its port from the sidecar port)."""
    fake_port = 18930 + (port - 8930)
    return {
        "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{fake_port}/v1",
        "OPENAI_API_KEYS": "sk-lane",
    }


@pytest.fixture(scope="module")
def agent_instance(instance_with, sidecar) -> LaunchedInstance:
    """A scratch instance on the post-7bdf76d default gate: AGENT_TOOL_APPROVAL_MODE
    unset means 'full', so tool calls run ungated with no approval card."""
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            **_fake_connection(sidecar["port"]),
        }
    )


@pytest.fixture(scope="module")
def ask_instance(instance_with, sidecar) -> LaunchedInstance:
    """The ask-gated twin: AGENT_TOOL_APPROVAL_MODE=ask opts this deployment into
    the approval card on every tool call (the post-7bdf76d opt-in)."""
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            "AGENT_TOOL_APPROVAL_MODE": "ask",
            **_fake_connection(sidecar["port"]),
        }
    )


def _publish_agent_loop_model(client: Any) -> str:
    """Register the agent-loop preset on one instance's model registry."""
    created = client.post(
        "/api/v1/models/create",
        json={
            "id": AGENT_MODEL_ID,
            "name": "Agent Loop Test",
            # A preset over the connection's fake: only presets appear in
            # the model registry as standalone entries.
            "base_model_id": "fake-model",
            "meta": {},
            "params": {},
        },
    )
    if created.status_code != 200 and "already registered" not in created.text:
        assert created.status_code == 200, created.text
    grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
    base_shared = client.post(
        "/api/v1/models/model/access/update",
        json={"id": "fake-model", "name": "fake-model", "access_grants": [grant]},
    )
    assert base_shared.status_code == 200, base_shared.text
    shared = client.post(
        "/api/v1/models/model/access/update",
        json={"id": AGENT_MODEL_ID, "name": "Agent Loop Test", "access_grants": [grant]},
    )
    assert shared.status_code == 200, shared.text
    listed = client.get("/api/models", params={"refresh": "true"})
    listed.raise_for_status()
    body = listed.json()
    models = body.get("data") if isinstance(body, dict) else body
    assert any(model["id"] == AGENT_MODEL_ID for model in models), (
        f"{AGENT_MODEL_ID} missing from the refreshed registry: {[m.get('id') for m in models]}"
    )
    return AGENT_MODEL_ID


@pytest.fixture(scope="module")
def admin(agent_instance: LaunchedInstance):
    """The admin of the agent-loop instance, not the shared regression instance."""
    return admin_of(agent_instance)


@pytest.fixture(scope="module")
def ask_admin(ask_instance: LaunchedInstance):
    """The admin of the ask-gated instance."""
    return admin_of(ask_instance)


@pytest.fixture
def agent_model(admin, agent_instance: LaunchedInstance) -> str:
    """Publish the workspace model routed to the agent loop."""
    with agent_instance.client() as client:
        return _publish_agent_loop_model(client)


@pytest.fixture
def ask_model(ask_admin, ask_instance: LaunchedInstance) -> str:
    """The agent-loop model registered on the ask-gated instance too."""
    with ask_instance.client() as client:
        return _publish_agent_loop_model(client)


@pytest.fixture
def watcher(admin) -> Iterator[SocketSession]:
    """A signed-in socket tab on the default-gate instance."""
    with connected(admin) as session:
        yield session


@pytest.fixture
def ask_watcher(ask_admin) -> Iterator[SocketSession]:
    """A signed-in socket tab on the ask-gated instance."""
    with connected(ask_admin) as session:
        yield session


def events_for(session: SocketSession, message_id: str) -> list[dict]:
    """The envelope payloads streamed for one assistant message id."""
    return [
        entry["data"]
        for entry in session.events
        if entry.get("message_id") == message_id and isinstance(entry.get("data"), dict)
    ]


def message_text(message: dict) -> str:
    return "".join(
        part.get("text", "")
        for item in (message.get("output") or [])
        if item.get("type") == "message"
        for part in item.get("content", [])
    )


def test_scripted_chat_streams_envelope_output_items_and_persists(admin, agent_model, watcher):
    """A scripted chat answers through the ordinary envelope: message output
    item, done frame, and a persisted record that reload keeps."""
    turn = send_message(admin.client(), "ECHO: hello bridge", model=agent_model)
    stored = wait_for_reply(admin.client(), turn)
    assert stored["done"] is True
    assert message_text(stored) == "echoed: hello bridge"

    frames = events_for(watcher, turn.assistant_message_id)
    completions = [frame for frame in frames if frame.get("type") == "chat:completion"]
    assert completions, f"no chat:completion frames reached the user room: {frames}"

    # The streamed output item grows through deltas and lands done with the
    # same text the database persisted.
    streamed = [frame for frame in completions if frame["data"].get("done")]
    assert streamed and message_text(streamed[-1]["data"]) == "echoed: hello bridge"
    deltas = [frame for frame in completions if not frame["data"].get("done")]
    assert deltas, "the stream never carried an in-progress output item"


def test_stop_flushes_a_done_frame_with_partial_content(admin, agent_model, watcher):
    """Stop posts to the sidecar abort route: the turn ends done with partial content."""
    turn = send_message(admin.client(), "SLOW", model=agent_model)
    watcher.wait_for(turn.chat_id, "chat:completion")
    time.sleep(0.5)
    stopped = admin.client().post(f"/api/tasks/chat/{turn.chat_id}/stop")
    assert stopped.status_code == 200, stopped.text

    stored = wait_for_reply(admin.client(), turn, 30.0)
    assert stored["done"] is True
    assert message_text(stored).startswith("chunk"), f"partial content not preserved: {stored}"


def _stored_chat_json(client: Any, chat_id: str) -> dict:
    return client.get(f"/api/v1/chats/{chat_id}").json()["chat"]


def _wait_for_pending_call(client: Any, turn: ChatTurn, timeout: float = 20.0) -> tuple[str, dict]:
    """The stored assistant message with its approval-gated function call."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        metadata = _stored_chat_json(client, turn.chat_id)["history"]["messages"]
        message = metadata.get(turn.assistant_message_id, {})
        call = next(
            (
                item
                for item in message.get("output") or []
                if item.get("type") == "function_call" and item.get("status") == "pending"
            ),
            None,
        )
        if call:
            return call.get("call_id") or call.get("id") or "", message
        time.sleep(0.2)
    raise AssertionError(f"no pending approval card landed: {message}")


def _resolve(client: Any, turn: ChatTurn, call_id: str, action: str) -> dict:
    resolved = client.post(
        f"/api/v1/chats/{turn.chat_id}/messages/{turn.assistant_message_id}/resolve",
        json={"call_id": call_id, "action": action},
    )
    assert resolved.status_code == 200, resolved.text
    return resolved.json()


def test_default_full_runs_the_tool_silently_with_no_card(admin, agent_model):
    """The default gate is 'full' (post-7bdf76d): a scripted tool call runs
    ungated — the call lands completed, the answer arrives, and no pending
    card ever exists for a browser to resolve."""
    client = admin.client()
    turn = send_message(client, "TOOL_ECHO ungated default", model=agent_model)
    stored = wait_for_reply(client, turn)
    assert "tool said:" in stored.get("content", ""), f"the ungated run did not answer: {stored}"

    calls = [item for item in stored.get("output") or [] if item.get("type") == "function_call"]
    assert calls and all(item.get("status") == "completed" for item in calls), (
        f"the default 'full' gate leaked a non-completed call: {stored}"
    )


def test_tool_call_gates_on_the_approval_card_and_the_approve_verdict_resumes(ask_admin, ask_model, ask_watcher):
    """A scripted tool call raises the approval card, the browser's approve
    reaches the sidecar interaction route, and the run resumes with the tool
    result as the answer (issue 6, the AGENT_TOOL_APPROVAL_MODE=ask opt-in)."""
    client = ask_admin.client()
    turn = send_message(client, "TOOL_ECHO parity", model=ask_model)

    # The pending card lands while the run waits on the gate; nothing errors.
    call_id, message = _wait_for_pending_call(client, turn)
    errors = [
        frame
        for frame in events_for(ask_watcher, turn.assistant_message_id)
        if frame.get("type") == "chat:message:error"
    ]
    assert not errors, f"the gated run errored while pending: {errors}"
    pending_frames = [
        frame
        for frame in events_for(ask_watcher, turn.assistant_message_id)
        if frame.get("type") == "chat:completion"
        and any(
            item.get("type") == "function_call" and item.get("status") == "pending"
            for item in frame.get("data", {}).get("output", [])
        )
    ]
    assert pending_frames, "the pending card never reached the user room"

    _resolve(client, turn, call_id, "approve")
    stored = wait_for_reply(client, turn)
    assert "tool said:" in stored.get("content", ""), f"approve did not resume the run: {stored}"

    items = stored.get("output") or []
    calls = [item for item in items if item.get("type") == "function_call"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert calls and outputs, f"no tool-call cards in {items}"
    assert any((item.get("call_id") or item.get("id")) == call_id for item in outputs), (
        f"the executed call lost its result item: {call_id}"
    )

    statuses = message.get("statusHistory") or stored.get("statusHistory") or []
    assert any("echo" in json.dumps(status) for status in statuses), f"no status pill persisted: {statuses}"
    pills = [frame for frame in events_for(ask_watcher, turn.assistant_message_id) if frame.get("type") == "status"]
    assert pills, "no status pill events reached the user room"


def test_the_deny_verdict_reaches_the_sidecar_and_lands_a_rejected_card(ask_admin, ask_model):
    """Deny rides the same resolve route: the sidecar skips the tool, the run
    answers with the rejected verdict, and the card lands rejected (issue 6,
    the AGENT_TOOL_APPROVAL_MODE=ask opt-in)."""
    client = ask_admin.client()
    turn = send_message(client, "TOOL_ECHO deny parity", model=ask_model)
    call_id, _ = _wait_for_pending_call(client, turn)
    _resolve(client, turn, call_id, "reject")
    stored = wait_for_reply(client, turn)
    assert "Tool call rejected by the user." in stored.get("content", ""), f"deny did not reach the run: {stored}"

    calls = [item for item in stored.get("output") or [] if item.get("type") == "function_call"]
    assert any(item.get("status") == "rejected" for item in calls), f"no rejected card: {calls}"


def test_send_during_generation_steers_instead_of_interrupting(admin, agent_model, watcher):
    """A send during an active run posts to the steer route; the running
    stream continues and no interrupt rebuilds the message (issue 6)."""
    first = send_message(admin.client(), "SLOW", model=agent_model)
    time.sleep(1.0)
    steered = send_message(
        admin.client(),
        "ECHO: steer question",
        model=agent_model,
        chat_id=first.chat_id,
        parent_id=first.user_message_id,
        history=[{"role": "user", "content": "SLOW"}],
    )
    first_stored = wait_for_reply(admin.client(), first, 30.0)
    steered_stored = wait_for_reply(admin.client(), steered, 30.0)

    # The interrupted run never happened: the first message finishes with its
    # streamed content and no error, the steer lands on its own message.
    assert first_stored["done"] is True and not first_stored.get("error")
    assert message_text(first_stored).startswith("chunk")
    assert steered_stored["done"] is True and not steered_stored.get("error")

    errors = [frame for frame in events_for(watcher, first.assistant_message_id) if frame.get("type") == "chat:message:error"]
    assert not errors, f"steering produced an error frame on the running message: {errors}"


def test_edit_resend_forks_the_session(admin, agent_model):
    """Edit-resend with truncated history follows the fork: the sidecar
    session replays only the kept turns (issue 6, ADR 0005)."""
    first_turn, first = ask(admin.client(), "COUNT_HISTORY", model=agent_model)
    assert "history: 1" in first.get("content", "")
    second_turn, second = ask(
        admin.client(),
        "COUNT_HISTORY",
        model=agent_model,
        chat_id=first_turn.chat_id,
        parent_id=first_turn.user_message_id,
        history=[
            {"role": "user", "content": "COUNT_HISTORY"},
            {"role": "assistant", "content": first.get("content", "")},
        ],
    )
    assert "history: 2" in second.get("content", "")

    # Resend turn two as an edit: the request carries turn one only, so the
    # bridge restores the session to one user turn and the resent turn
    # replays one prior user message.
    edited = send_message(
        admin.client(),
        "COUNT_HISTORY",
        model=agent_model,
        chat_id=first_turn.chat_id,
        parent_id=first_turn.user_message_id,
        history=[{"role": "user", "content": "COUNT_HISTORY"}],
    )
    stored = wait_for_reply(admin.client(), edited, 30.0)
    assert "history: 2" in stored.get("content", ""), f"fork did not restore history: {stored}"


@pytest.fixture(scope="module")
def tasks_instance(instance_with, sidecar) -> LaunchedInstance:
    """A second scratch instance with title, tags, and follow-ups enabled."""
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            **_fake_connection(sidecar["port"]),
            "ENABLE_TITLE_GENERATION": "true",
            "ENABLE_TAGS_GENERATION": "true",
            "ENABLE_FOLLOW_UP_GENERATION": "true",
        }
    )


@pytest.fixture(scope="module")
def tasks_model(tasks_instance: LaunchedInstance) -> str:
    """The agent-loop model registered on the tasks instance too."""
    from harness.actors import admin_of

    admin = admin_of(tasks_instance)
    with admin.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": AGENT_MODEL_ID,
                "name": "Agent Loop Test",
                "base_model_id": MOCK_MODEL_ID,
                "meta": {},
                "params": {},
            },
        )
        if created.status_code != 200 and "already registered" not in created.text:
            assert created.status_code == 200, created.text
        grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
        shared = client.post(
            "/api/v1/models/model/access/update",
            json={"id": AGENT_MODEL_ID, "name": "Agent Loop Test", "access_grants": [grant]},
        )
        assert shared.status_code == 200, shared.text
        client.get("/api/models", params={"refresh": "true"}).raise_for_status()
    return AGENT_MODEL_ID


def _sidecar_session_turns(port: int, chat_id: str) -> int:
    """User turns the sidecar session hosts, straight from its registry."""
    response = httpx.post(f"http://127.0.0.1:{port}/sessions", json={"chatId": chat_id}, timeout=5.0)
    response.raise_for_status()
    return response.json().get("userTurns", 0)


def test_tasks_keep_their_python_path_and_the_session_its_history(tasks_instance, tasks_model, sidecar):
    """Title, tags, and follow-ups arrive through the unchanged background
    path, the sidecar session keeps its turns (a task call never forks it to
    zero), and the next turn holds its context (fix round 1, lane 8)."""
    from harness.actors import admin_of

    admin = admin_of(tasks_instance)
    client = admin.client()
    """Title, tags, and follow-ups arrive through the unchanged background
    path, the sidecar session keeps its turns (a task call never forks it to
    zero), and the next turn holds its context (fix round 1, lane 8)."""
    client = admin.client()
    tasks = {"title_generation": True, "tags_generation": True, "follow_up_generation": True}
    turn = send_message(client, "COUNT_HISTORY", model=tasks_model, background_tasks=tasks)
    stored = wait_for_reply(client, turn)
    assert "history: 1" in stored.get("content", "")

    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        record = client.get(f"/api/v1/chats/{turn.chat_id}").json()
        metadata = record["chat"]
        message = metadata["history"]["messages"].get(turn.assistant_message_id, {})
        if metadata.get("title") and (record.get("meta") or {}).get("tags") and message.get("followUps"):
            break
        time.sleep(0.4)
    assert metadata.get("title"), f"no title arrived: {metadata.get('title')}"
    assert (record.get("meta") or {}).get("tags"), f"no tags arrived: {(record.get('meta') or {}).get('tags')}"
    assert message.get("followUps"), "no follow-ups arrived"

    # The background calls ran with the task model set to the agent-loop
    # model — exactly the payload that used to fork the session to zero.
    assert _sidecar_session_turns(sidecar["port"], turn.chat_id) == 1, (
        "a background task call touched the sidecar session"
    )

    _, second = ask(
        client,
        "COUNT_HISTORY",
        model=tasks_model,
        chat_id=turn.chat_id,
        parent_id=turn.user_message_id,
        history=[
            {"role": "user", "content": "COUNT_HISTORY"},
            {"role": "assistant", "content": stored.get("content", "")},
        ],
    )
    assert "history: 2" in second.get("content", ""), f"the next turn lost its context: {second}"


def test_the_bridge_refuses_an_empty_chat_id_with_a_clean_400(admin, agent_model):
    """Empty metadata.chat_id reaches nothing: the bridge answers 400 before
    the session lookup (fix round 1, the `chatId: ""` 500 guard)."""
    rejected = admin.client().post(
        "/api/chat/completions",
        json={
            "model": agent_model,
            "messages": [{"role": "user", "content": "ECHO: orphan"}],
            "stream": True,
            "metadata": {"chat_id": ""},
        },
    )
    assert rejected.status_code == 400, rejected.text


def test_dead_provider_lands_as_chat_message_error_with_a_done_state(admin, agent_model, watcher):
    """A terminal provider failure emits `chat:message:error` and the stored
    message lands done with the error recorded (issue 6)."""
    fake_pid = (Path(LANE_DIR) / "fake.pid").read_text().strip()
    subprocess.run(["kill", fake_pid], check=False)
    time.sleep(0.5)
    try:
        turn = send_message(admin.client(), "ECHO: nobody home", model=agent_model)
        stored = wait_for_reply(admin.client(), turn, 60.0)
        assert stored["done"] is True
        assert stored.get("error") == {"content": {"content": "The agent run failed."}}, (
            f"no terminal error persisted on a dead provider: {stored.get('error')}"
        )

        errors = [frame for frame in events_for(watcher, turn.assistant_message_id) if frame.get("type") == "chat:message:error"]
        assert errors, "no chat:message:error frame reached the user room"
    finally:
        subprocess.run([str(TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"), "stop"], check=False, env=_lane_env())
        _boot_sidecar()


def _run_bridge_turn(agent_bridge, form_data: dict) -> tuple[Any, str]:
    """Drive `generate_agent_chat_completion` directly and read its stream.

    Returns the response (for its headers) plus the collected SSE body, so a
    test asserts what a client would actually receive.
    """

    async def run():
        response = await agent_bridge.generate_agent_chat_completion(request=None, form_data=form_data, user=None)
        body = "".join([chunk async for chunk in response.body_iterator])
        return response, body

    return asyncio.run(run())


def test_steer_meeting_run_end_falls_through_to_a_new_turn(sidecar, monkeypatch):
    """A steer whose run ends between the bridge's running check and the
    steer post meets a 409 from the sidecar (issue 32): the bridge re-reads
    the session, sees the run ended, and answers the send as a normal new
    turn instead of failing it."""
    from open_webui.routers import agent as agent_bridge

    monkeypatch.setenv("AGENT_SIDECAR_PORT", str(sidecar["port"]))
    chat_id = f"steer-race-{uuid.uuid4().hex[:8]}"
    real_ensure = agent_bridge.ensure_session
    real_steer = agent_bridge.steer_session
    steer_attempts: list[dict] = []

    async def stale_then_real(client, checked_chat_id, system):
        info = await real_ensure(client, checked_chat_id, system)
        if not steer_attempts:
            # The stale answer: the check ran before the run ended.
            return {**info, "running": True}
        return info

    async def recorded_steer(client, steered_chat_id, message):
        try:
            disposition = await real_steer(client, steered_chat_id, message)
            steer_attempts.append({"status": 200})
            return disposition
        except httpx.HTTPStatusError as e:
            steer_attempts.append({"status": e.response.status_code})
            raise

    monkeypatch.setattr(agent_bridge, "ensure_session", stale_then_real)
    monkeypatch.setattr(agent_bridge, "steer_session", recorded_steer)

    form_data = {
        "messages": [{"role": "user", "content": "ECHO: after run end"}],
        "metadata": {"chat_id": chat_id},
    }
    response, body = _run_bridge_turn(agent_bridge, form_data)

    # The race really fired: the steer post hit the sidecar and drew the 409.
    assert steer_attempts == [{"status": 409}], f"expected one 409 steer attempt: {steer_attempts}"
    # The send was answered as a new turn — not a steer, not a failure.
    assert response.headers.get("x-agent-steer") is None, dict(response.headers)
    deltas = [
        json.loads(frame[6:]).get("delta", "")
        for frame in body.splitlines()
        if frame.startswith("data: ") and "output_text.delta" in frame
    ]
    assert "".join(deltas) == "echoed: after run end", body
    assert "data: [DONE]" in body, body


def test_steer_409_with_the_session_running_again_retries_once(sidecar, monkeypatch):
    """The narrow sibling of the run-end race: the recheck after the 409
    finds the session running again, so the bridge retries the steer once
    and the send still lands as a steer."""
    from open_webui.routers import agent as agent_bridge

    monkeypatch.setenv("AGENT_SIDECAR_PORT", str(sidecar["port"]))
    chat_id = f"steer-retry-{uuid.uuid4().hex[:8]}"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/steer") and calls.count(f"POST {request.url.path}") == 1:
            return httpx.Response(409, json={"ok": False, "error": "run ended"})
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"ok": True, "running": True, "userTurns": 1})
        return httpx.Response(200, json={"ok": True, "disposition": "steered"})

    monkeypatch.setattr(
        agent_bridge,
        "_client",
        lambda: httpx.AsyncClient(base_url="http://sidecar.test", transport=httpx.MockTransport(handler)),
    )

    form_data = {
        "messages": [{"role": "user", "content": "ECHO: retry"}],
        "metadata": {"chat_id": chat_id},
    }
    response, body = _run_bridge_turn(agent_bridge, form_data)

    assert [call.split(" ", 1)[1] for call in calls] == [
        "/sessions",
        f"/sessions/{chat_id}/steer",
        "/sessions",
        f"/sessions/{chat_id}/steer",
    ], calls
    assert response.headers.get("x-agent-steer") == "true", dict(response.headers)
    assert "agent-steer" in body, body
