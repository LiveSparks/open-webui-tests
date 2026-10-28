"""The selected machine carries pi's rebound local tools (PR5, issue 8 / ADR 0002).

The machine-binding decision: pi's `bash`, `read`, `edit`, and `write` never
run on the sidecar. When the chat's turn selects a machine, the sidecar binds
same-named replacements whose operations call the OWUI loopback executor
(`/loopback/machines`), which re-resolves the machine from the turn's signed
binding and forwards through `execute_tool_server`. The scripted provider's
`TOOL:` prompts drive the rebound tools exactly as the model would.

A chat with no machine selected binds no local tools at all: the scripted
`bash` prompt comes back "tool bash was not offered" instead of an error. A
selected machine that is gone fails the turn with a clear status, never a
silent fallback. Per-chat cwd keying rides the terminal context headers, and
a mid-chat machine switch is the next turn's machine.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.instance import LaunchedInstance, WEBUI_SECRET_KEY as INSTANCE_SECRET
from harness.terminal_server import TERMINAL_SERVERS_CONFIG, configure_terminals, serving_terminal
from harness.upstream import MOCK_MODEL_ID

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

SIDECAR_PORT = 8944
LANE_DIR = "/tmp/agent_machine_binding-lane"
TESTS_REPO = Path(__file__).resolve().parents[2]
AGENT_MODEL_ID = "agent-machine.test"

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Harbour machine", "version": "1"},
    "paths": {
        "/execute": {
            "post": {
                "operationId": "run_command",
                "parameters": [
                    {"name": "wait", "in": "query", "schema": {"type": "number"}},
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"],
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/files/read": {
            "get": {
                "operationId": "read_file",
                "parameters": [{"name": "path", "in": "query", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/files/write": {
            "post": {
                "operationId": "write_file",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                "required": ["path", "content"],
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


def _command_answer(answer: str):
    def handler(request):
        body = json.loads(request.body or b"{}")
        command = body.get("command", "")
        return (200, {"Content-Type": "application/json"}, json.dumps({
            "id": "probe-1",
            "command": command,
            "status": "done",
            "exit_code": 0,
            "output": [{"type": "stdout", "data": f"{answer} ({command})\n"}],
            "truncated": False,
            "next_offset": 1,
            "log_path": f"/tmp/{answer}.jsonl",
        }).encode())

    return handler


def _read_answer(content: str):
    def handler(request):
        path = request.path.split("path=")[-1]
        return (200, {"Content-Type": "application/json"}, json.dumps({
            "path": path,
            "total_lines": 1,
            "content": content,
        }).encode())

    return handler


def _write_answer(request):
    body = json.loads(request.body or b"{}")
    return (
        200,
        {"Content-Type": "application/json"},
        json.dumps({"path": body.get("path", ""), "size": len(body.get("content", ""))}).encode(),
    )


def _machine(server, name: str, answer: str) -> dict:
    """A connected admin-configured terminal whose run_command answers `answer`."""
    server.route("GET", f"/{name}/openapi.json", (200, {"Content-Type": "application/json"}, json.dumps(SPEC).encode()))
    server.route("POST", f"/{name}/execute", _command_answer(answer))
    server.route("GET", f"/{name}/files/read", _read_answer(f"content of {answer}"))
    server.route("POST", f"/{name}/files/write", _write_answer)
    return {**server.connection(), "name": name, "url": f"{server.base_url}/{name}"}


@pytest.fixture(scope="module")
def sidecar() -> Iterator[dict]:
    boot = TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"
    assert boot.is_file(), f"boot recipe missing at {boot}"
    checkout = Path(os.environ.get("OPEN_WEBUI_CHECKOUT", ""))
    if not checkout.is_dir():
        checkout = TESTS_REPO.parent / "open-webui"
    subprocess.run(
        [str(boot), "start", str(checkout), str(SIDECAR_PORT)],
        check=True,
        env={**os.environ, "AGENT_SIDECAR_LANE_DIR": LANE_DIR},
    )
    yield {"port": SIDECAR_PORT}
    subprocess.run([str(boot), "stop"], check=False, env={**os.environ, "AGENT_SIDECAR_LANE_DIR": LANE_DIR})


@pytest.fixture(scope="module")
def machines_instance(instance_with, sidecar) -> LaunchedInstance:
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
        }
    )


@pytest.fixture(scope="module")
def machines_admin(machines_instance: LaunchedInstance):
    return admin_of(machines_instance)


@pytest.fixture
def agent_model(machines_admin) -> str:
    with machines_admin.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": AGENT_MODEL_ID,
                "name": "Machine Binding Test",
                "base_model_id": MOCK_MODEL_ID,
                "meta": {},
                "params": {},
            },
        )
        if created.status_code != 200 and "already registered" not in created.text:
            assert created.status_code == 200, created.text
        return AGENT_MODEL_ID


def message_text(message: dict) -> str:
    return "".join(
        part.get("text", "")
        for item in (message.get("output") or [])
        if item.get("type") == "message"
        for part in item.get("content", [])
    )


def _sign(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(INSTANCE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def test_a_machine_bound_bash_call_runs_on_the_selected_machine(machines_admin, agent_model):
    """A scripted bash call lands on the selected machine and the output
    reaches the model: the loopback executor route, end to end."""
    with serving_terminal() as server, machines_admin.client() as client:
        machine = _machine(server, "pier-a", "lighthouse-4")
        configure_terminals(client, machine)
        turn = send_message(
            client,
            'TOOL: bash :: {"command": "hostname"}',
            model=agent_model,
            terminal_id=machine["id"],
        )
        stored = wait_for_reply(client, turn)
        assert "lighthouse-4" in message_text(stored), stored

        [sent] = server.requests_to("/execute")[-1:]
        assert json.loads(sent.body)["command"] == "hostname", sent.body
        assert sent.headers.get("x-session-id") == turn.chat_id, sent.headers


def test_a_file_roundtrip_runs_through_the_machine(machines_admin, agent_model):
    """write then read reach the machine's file operations, and the read-back
    content the model sees is what the machine recorded."""
    with serving_terminal() as server, machines_admin.client() as client:
        machine = _machine(server, "pier-a", "lighthouse-4")
        configure_terminals(client, machine)
        written = send_message(
            client,
            'TOOL: write :: {"path": "ferry.txt", "content": "06:40 from pier 7"}',
            model=agent_model,
            terminal_id=machine["id"],
        )
        wait_for_reply(client, written)

        read_back = send_message(
            client,
            'TOOL: read :: {"path": "ferry.txt"}',
            model=agent_model,
            terminal_id=machine["id"],
            chat_id=written.chat_id,
            parent_id=written.assistant_message_id,
        )
        stored = wait_for_reply(client, read_back)
        assert "content of lighthouse-4" in message_text(stored), stored

        assert server.requests_to("/files/write"), "the machine never got the write"
        [read] = server.requests_to("/files/read")[-1:]
        assert "ferry.txt" in read.path, read.path


def test_no_machine_lists_no_local_tools(machines_admin, agent_model):
    """The same chat without a machine selection binds no local tools: the
    scripted bash prompt is declined as not offered, not errored."""
    with serving_terminal() as server, machines_admin.client() as client:
        configure_terminals(client, _machine(server, "pier-a", "lighthouse-4"))
        turn = send_message(client, 'TOOL: bash :: {"command": "hostname"}', model=agent_model)
        stored = wait_for_reply(client, turn)
        assert "tool bash was not offered" in message_text(stored), stored
        assert "error" not in stored, stored


def test_a_machine_switch_is_the_next_turns_machine(machines_admin, agent_model):
    """One machine at a time, re-resolved per turn: switching the selection
    between turns moves the binding, and the new machine serves the call."""
    with serving_terminal() as server, machines_admin.client() as client:
        pier_a = _machine(server, "pier-a", "lighthouse-4")
        pier_b = _machine(server, "pier-b", "wavebreaker-9")
        configure_terminals(client, pier_a, pier_b)
        first = send_message(
            client,
            'TOOL: bash :: {"command": "hostname"}',
            model=agent_model,
            terminal_id=pier_a["id"],
        )
        wait_for_reply(client, first)

        second = send_message(
            client,
            'TOOL: bash :: {"command": "hostname"}',
            model=agent_model,
            terminal_id=pier_b["id"],
            chat_id=first.chat_id,
            parent_id=first.assistant_message_id,
        )
        stored = wait_for_reply(client, second)
        assert "wavebreaker-9" in message_text(stored), stored


def test_a_gone_machine_fails_the_turn_with_a_clear_status(machines_admin, agent_model):
    """Fail closed: a selection whose machine vanished between turns refuses
    the turn with a clear status, never a silent fallback or another machine."""
    with serving_terminal() as server, machines_admin.client() as client:
        machine = _machine(server, "pier-a", "lighthouse-4")
        configure_terminals(client, machine)
        first = send_message(
            client,
            'TOOL: bash :: {"command": "hostname"}',
            model=agent_model,
            terminal_id=machine["id"],
        )
        wait_for_reply(client, first)

        configure_terminals(client)  # the machine is gone
        refused = client.post(
            "/api/chat/completions",
            json={
                "model": agent_model,
                "messages": [{"role": "user", "content": "run hostname again"}],
                "stream": True,
                "chat_id": first.chat_id,
                "terminal_id": machine["id"],
            },
        )
        assert refused.status_code == 404, refused.text
        assert "machine" in refused.text.lower(), refused.text


def test_the_per_chat_cwd_key_survives_turns_and_splits_chats(machines_admin, agent_model):
    """The session cwd is a per-chat directory on the machine: both turns of
    one chat carry the same X-Session-Id (and chat context id), a second chat
    carries its own."""
    with serving_terminal() as server, machines_admin.client() as client:
        machine = _machine(server, "pier-a", "lighthouse-4")
        configure_terminals(client, machine)
        first = send_message(
            client,
            'TOOL: bash :: {"command": "pwd"}',
            model=agent_model,
            terminal_id=machine["id"],
        )
        wait_for_reply(client, first)
        next_turn = send_message(
            client,
            'TOOL: bash :: {"command": "ls"}',
            model=agent_model,
            terminal_id=machine["id"],
            chat_id=first.chat_id,
            parent_id=first.assistant_message_id,
        )
        wait_for_reply(client, next_turn)
        other = send_message(
            client,
            'TOOL: bash :: {"command": "pwd"}',
            model=agent_model,
            terminal_id=machine["id"],
        )
        wait_for_reply(client, other)

        sessions = [request.headers.get("x-session-id") for request in server.requests_to("/execute")]
        assert sessions[0] == sessions[1] == first.chat_id, sessions
        assert sessions[2] not in (None, first.chat_id), sessions
        # A plain (non-orchestrator) connection keys the per-chat cwd by
        # X-Session-Id alone; an orchestrator adds X-Terminal-Context-Id on top.
        contexts = {request.headers.get("x-terminal-context-id") for request in server.requests_to("/execute")}
        assert contexts == {None}, contexts


def test_the_executor_refuses_an_unbound_call_at_the_wire(machines_instance, machines_admin, agent_model):
    """Direct executor access answers 401 without a token and 404 for a
    binding whose machine is gone — the same fail-closed contract the sidecar
    meets, asserted at the wire level."""
    with serving_terminal() as server, machines_admin.client() as client, httpx.Client(
        timeout=60, base_url=machines_instance.base_url
    ) as raw:
        configure_terminals(client, _machine(server, "pier-a", "lighthouse-4"))

        no_token = raw.post("/loopback/machines", json={"op": "bash", "command": "hostname"})
        assert no_token.status_code == 401, no_token.text

        claims = {
            "chat_id": "chat-wire-probe",
            "user_id": machines_admin.id,
            "terminal_id": "terminal-gone",
            "exp": int(time.time()) + 600,
        }
        gone = raw.post("/loopback/machines", json={"op": "bash", "command": "hostname"}, headers={
            "Authorization": "Bearer " + _sign(claims)
        })
        assert gone.status_code == 404, gone.text

        machine = _machine(server, "pier-a", "lighthouse-4")
        configure_terminals(client, machine)
        claims["terminal_id"] = machine["id"]
        executed = raw.post("/loopback/machines", json={"op": "bash", "command": "hostname"}, headers={
            "Authorization": "Bearer " + _sign(claims)
        })
        assert executed.status_code == 200, executed.text
        assert executed.json()["ok"] is True, executed.text
        assert "lighthouse-4" in json.dumps(executed.json()["result"]), executed.text


def test_the_sidecar_never_learns_a_machine_url(machines_admin, agent_model):
    """Isolation (lane 8's unit shadow): the machine binding the sidecar
    carries points at the backend's own loopback surface, never at the
    machine — the machine URL and key live only in the backend."""
    with serving_terminal() as server, machines_admin.client() as client:
        machine = _machine(server, "pier-a", "lighthouse-4")
        configure_terminals(client, machine)
        turn = send_message(
            client,
            'TOOL: bash :: {"command": "hostname"}',
            model=agent_model,
            terminal_id=machine["id"],
        )
        wait_for_reply(client, turn)

        sidecar_log = Path(LANE_DIR) / "sidecar.log"
        if sidecar_log.is_file():
            carried = sidecar_log.read_text()
            assert machine["url"] not in carried, "the sidecar saw the machine URL"
