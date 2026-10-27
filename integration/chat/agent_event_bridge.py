"""The event bridge translates pi's session stream into the chat envelope.

PR3 of the pi-engine program (issue 6): a chat routed to the agent sidecar
answers through the ordinary Socket.IO `events` envelope with
`chat:completion` Responses-style output items, stop posts to the sidecar
abort route and flushes a done frame, a send during an active run steers
instead of interrupting, edit-resend forks the sidecar session, and a dead
provider lands as `chat:message:error` with a usable done state.

Every reply is scripted by `agent_sidecar/openai_fake.py`, so each test
asserts the roundtrip it scripted rather than a canned constant.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator

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


@pytest.fixture(scope="module")
def agent_instance(instance_with, sidecar) -> LaunchedInstance:
    """A scratch instance whose AGENT_LOOP_MODEL_IDS routes the model to the sidecar."""
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
        }
    )


@pytest.fixture(scope="module")
def admin(agent_instance: LaunchedInstance):
    """The admin of the agent-loop instance, not the shared regression instance."""
    return admin_of(agent_instance)


@pytest.fixture
def agent_model(admin, agent_instance: LaunchedInstance) -> str:
    """Publish the workspace model routed to the agent loop."""
    with agent_instance.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": AGENT_MODEL_ID,
                "name": "Agent Loop Test",
                # A preset over the harness mock: only presets appear in the
                # model registry as standalone entries.
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
        listed = client.get("/api/models", params={"refresh": "true"})
        listed.raise_for_status()
        body = listed.json()
        models = body.get("data") if isinstance(body, dict) else body
        assert any(model["id"] == AGENT_MODEL_ID for model in models), (
            f"{AGENT_MODEL_ID} missing from the refreshed registry: {[m.get('id') for m in models]}"
        )
    return AGENT_MODEL_ID


@pytest.fixture
def watcher(admin) -> Iterator[SocketSession]:
    """A signed-in socket tab that records the envelope the way a browser does."""
    with connected(admin) as session:
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


def test_tool_roundtrip_carries_status_pills_and_function_call_items(admin, agent_model, watcher):
    """A scripted tool call rides the bridge as function_call items plus status
    pill events, and the sidecar-executed result reaches the reply (issue 6)."""
    turn = send_message(admin.client(), "TOOL_ECHO parity", model=agent_model)
    stored = wait_for_reply(admin.client(), turn)
    assert "tool said:" in stored.get("content", "")

    items = stored.get("output") or []
    types = [item.get("type") for item in items]
    assert "function_call" in types and "function_call_output" in types, f"no tool-call cards in {types}"

    statuses = stored.get("statusHistory") or []
    assert any("echo" in json.dumps(status) for status in statuses), f"no status pill persisted: {statuses}"

    pills = [frame for frame in events_for(watcher, turn.assistant_message_id) if frame.get("type") == "status"]
    assert pills, "no status pill events reached the user room"


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
        assert stored.get("error"), f"no error persisted on a dead provider: {stored}"

        errors = [frame for frame in events_for(watcher, turn.assistant_message_id) if frame.get("type") == "chat:message:error"]
        assert errors, "no chat:message:error frame reached the user room"
    finally:
        subprocess.run([str(TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"), "stop"], check=False, env=_lane_env())
        _boot_sidecar()
