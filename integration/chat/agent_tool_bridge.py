"""The loopback tool bridge reaches OWUI's own capabilities from the sidecar.

PR4 of the pi-engine program (issue 7): the sidecar's pi sessions consume
Open WebUI's capabilities through the loopback MCP server instead of guessing.
A chat turn scripts the bridge with the provider fake's `TOOL:` / `TOOL_CHAIN:`
prompts — a direct spine tool (`mcp__owui__search_web`) is declared to the
model straight away, and a deferred family is reached through `tool_search`
first, exactly as the exposure split promises.

Every bridged call carries the short-TTL signed binding the backend minted at
chat start; the denial path is exercised directly against the bridge endpoint:
no token, a foreign signature, and an expired token all answer 401.

The workspace Tools registry runs over the bridge too, including a sync tool
that blocks on a call back to this very server — the shape that deadlocked
trunk's single uvicorn worker, and runs to completion on the offload guard.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from harness.actors import admin_of, create_user
from harness.chat import send_message, wait_for_reply
from harness.code_interpreter import fake_jupyter
from harness.image_engines import save_image_settings, serve_automatic1111
from harness.instance import LaunchedInstance
from harness.knowledge_bases import add_text_file, knowledge_base
from harness.python_tools import EVERYONE_READS
from harness.search_apis import ENGINES_BY_NAME, API_KEY, Hit, carries, search_apis_env, serving_search_apis
from harness.upstream import MOCK_MODEL_ID
from harness.web_retrieval import save_web_settings, web_settings_restored

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

TOOL_SERVERS = ("/api/v1/configs/tool_servers", "/api/v1/configs/tool_servers")


@contextlib.contextmanager
def serving_echo_mcp() -> Iterator[str]:
    """A streamable-HTTP MCP echo server the backend relays (mcp SDK 1.x, the
    line this checkout's backend pins; harness.mcp_server speaks 2.x)."""
    import threading

    import uvicorn
    from mcp.server.fastmcp import FastMCP

    pier = FastMCP("pier")

    @pier.tool(description="Repeat the text back.")
    def echo(text: str) -> str:
        return text

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(pier.streamable_http_app(), host="127.0.0.1", port=port, log_config=None, log_level="warning")
    runner = uvicorn.Server(config)
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not runner.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the relay MCP server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        runner.should_exit = True
        thread.join(timeout=10)


def _mcp_connection(url: str, server_id: str) -> dict:
    """An MCP tool server connection without auth, the way the admin panel saves one."""
    return {
        "url": url,
        "path": "",
        "type": "mcp",
        "auth_type": "none",
        "key": "",
        "config": {"enable": True, "access_grants": []},
        "info": {"id": server_id, "name": server_id},
    }


AGENT_MODEL_ID = "agent-loop.test"
SIDECAR_PORT = 8952
LANE_DIR = "/tmp/agent_tool_bridge-lane"
TESTS_REPO = Path(__file__).resolve().parents[2]

HARBOUR = Hit("https://harbour.example/ferry", "Ferry times", "The ferry leaves at noon.")

# A workspace tool that blocks on a synchronous call back to this server: the
# sync-tool offload shape (trunk runs it on the loop and deadlocks the single
# worker; the bridge's Python side offloads plain-Python tools to a thread).
BLOCKING_PROBE_TOOL = """
import urllib.request

class Tools:
    def probe(self) -> str:
        \"\"\"Probe this server from inside a tool.\"\"\"
        with urllib.request.urlopen("__BASE__/health", timeout=10) as response:
            return "probe:" + str(response.status)
"""

FOREIGN_TOOL = """
class Tools:
    def whisper(self) -> str:
        \"\"\"Whisper a secret.\"\"\"
        return "the secret"
"""

SHOUT_TOOL = """
class Tools:
    def shout(self, text: str) -> str:
        \"\"\"Shout the text back.\"\"\"
        return text.upper()
"""


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
    boot = TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"
    assert boot.is_file(), f"boot recipe missing at {boot}"
    _boot_sidecar()
    yield {"port": SIDECAR_PORT}
    subprocess.run([str(boot), "stop"], check=False, env=_lane_env())


@pytest.fixture(scope="module")
def apis():
    with serving_search_apis() as serving:
        yield serving


@pytest.fixture(scope="module")
def bridge_instance(instance_with, apis, sidecar) -> LaunchedInstance:
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            **search_apis_env(apis),
        }
    )


@pytest.fixture(scope="module")
def bridge_admin(bridge_instance: LaunchedInstance):
    return admin_of(bridge_instance)


@pytest.fixture(scope="module")
def kernel():
    with fake_jupyter() as jupyter:
        yield jupyter


def _publish_agent_loop_model(client: httpx.Client) -> str:
    """The agent-loop preset with file context off, so attached chat files are
    tool-shaped (the file family bridges) instead of pre-injected."""
    created = client.post(
        "/api/v1/models/create",
        json={
            "id": AGENT_MODEL_ID,
            "name": "Agent Loop Test",
            "base_model_id": MOCK_MODEL_ID,
            "meta": {"capabilities": {"file_context": False}},
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
def agent_model(bridge_admin) -> str:
    with bridge_admin.client() as client:
        return _publish_agent_loop_model(client)


def _tool_reply(client: httpx.Client, turn, script: str, model: str, **options):
    """Send one scripted turn and return the stored assistant message."""
    turn = send_message(client, script, model=model, **options)
    return wait_for_reply(client, turn)


def message_text(message: dict) -> str:
    return "".join(
        part.get("text", "")
        for item in (message.get("output") or [])
        if item.get("type") == "message"
        for part in item.get("content", [])
    )


def _bridge_url(instance: LaunchedInstance) -> str:
    return f"{instance.base_url}/loopback/mcp"


def _sign(claims: dict, secret: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


# ---------------------------------------------------------------------------
# The spine, direct: web search and memory through scripted provider turns.
# ---------------------------------------------------------------------------


def test_search_web_runs_over_the_bridge_with_the_engine_hits(
    bridge_admin, agent_model, apis, bridge_instance
):
    """A scripted search_web call reaches the configured engine through the
    bridge and the model reads the hits back."""
    with bridge_admin.client() as client, web_settings_restored(client):
        save_web_settings(
            client,
            ENABLE_WEB_SEARCH=True,
            WEB_SEARCH_ENGINE="brave",
            WEB_SEARCH_RESULT_COUNT=5,
            BRAVE_SEARCH_API_KEY=API_KEY,
        )
        ENGINES_BY_NAME["brave"].serve(apis, [HARBOUR])
        turn = send_message(
            client,
            'TOOL: mcp__owui__search_web :: {"query": "ferry harbour"}',
            model=agent_model,
            features={"web_search": True},
        )
        stored = wait_for_reply(client, turn)
        reply_text = message_text(stored)
        assert "Ferry times" in reply_text, stored
        assert "harbour.example" in reply_text, stored

        [sent] = ENGINES_BY_NAME["brave"].searches(apis)[-1:]
        assert carries(sent, "ferry harbour"), f"engine never got the query: {sent}"


def test_memory_writes_and_reads_back_through_the_bridge(bridge_admin, agent_model):
    """add_memory lands a user-owned row, and search_memories reads it back in
    a later turn — the mutating memory writers ride unguarded, per the bridge
    decision, with ownership scoping only."""
    with bridge_admin.client() as client:
        marker = f"prefers quiet harbour moorings {uuid.uuid4().hex[:6]}"
        turn = send_message(
            client,
            f'TOOL: mcp__owui__add_memory :: {{"content": "{marker}"}}',
            model=agent_model,
            features={"memory": True},
        )
        stored = wait_for_reply(client, turn)
        assert "tool said:" in message_text(stored), stored

        memories = client.get("/api/v1/memories/")
        assert memories.status_code == 200, memories.text
        contents = [row["content"] for row in memories.json()]
        assert any(marker in content for content in contents), contents

        read_back = send_message(
            client,
            'TOOL: mcp__owui__search_memories :: {"query": "harbour"}',
            model=agent_model,
            features={"memory": True},
        )
        stored_read = wait_for_reply(client, read_back)
        assert marker in message_text(stored_read), stored_read


def test_a_plain_turn_does_not_poison_the_next_turns_bridge_call(
    bridge_admin, agent_model, apis, bridge_instance
):
    """The lane A blocker: the binding is minted fresh every turn, so the
    bridge re-registers between turns. A mid-run re-register hid the bridge
    tools while the run already held its snapshot and the next tool call died
    with "Tool mcp__owui__... not found". The sidecar now applies the fresh
    binding before the run starts; this sequence proves the swap actually ran,
    because the tool only exists under the second turn's claims (Web Search
    toggles on after a plain first turn)."""
    with bridge_admin.client() as client, web_settings_restored(client):
        save_web_settings(
            client,
            ENABLE_WEB_SEARCH=True,
            WEB_SEARCH_ENGINE="brave",
            WEB_SEARCH_RESULT_COUNT=5,
            BRAVE_SEARCH_API_KEY=API_KEY,
        )
        ENGINES_BY_NAME["brave"].serve(apis, [HARBOUR])
        plain = send_message(client, "ECHO: plain turn one", model=agent_model)
        stored_plain = wait_for_reply(client, plain)
        assert "echoed: plain turn one" in message_text(stored_plain), stored_plain

        turn = send_message(
            client,
            'TOOL: mcp__owui__search_web :: {"query": "ferry harbour"}',
            model=agent_model,
            chat_id=plain.chat_id,
            parent_id=plain.assistant_message_id,
            features={"web_search": True},
        )
        stored = wait_for_reply(client, turn)
        reply_text = message_text(stored)
        assert "Ferry times" in reply_text, stored
        assert "harbour.example" in reply_text, stored

        [sent] = ENGINES_BY_NAME["brave"].searches(apis)[-1:]
        assert carries(sent, "ferry harbour"), f"engine never got the query: {sent}"


# ---------------------------------------------------------------------------
# The deferred families, reached through tool_search first.
# ---------------------------------------------------------------------------


def test_knowledge_bases_are_reached_through_tool_search(bridge_admin, agent_model, bridge_instance):
    """The KB family stays deferred: tool_search discovers it, then the search
    runs through the bridge and names the base it found."""
    with bridge_admin.client() as client:
        name = f"Harbour rules {uuid.uuid4().hex[:6]}"
        with knowledge_base(client, name) as knowledge_id:
            add_text_file(client, knowledge_id, "mooring.txt", "Mooring lines cross at the north pier.")
            first = send_message(
                client,
                'TOOL_CHAIN: tool_search :: {"query": "knowledge base search"} -> mcp__owui__search_knowledge_bases :: {"query": "harbour"}',
                model=agent_model,
            )
            stored = wait_for_reply(client, first)
            reply_text = message_text(stored)
            assert name in reply_text, stored
            assert "tool_search" not in reply_text or "chained:" in reply_text, stored

            second = send_message(
                client,
                f'TOOL_CHAIN: tool_search :: {{"query": "query knowledge"}} -> mcp__owui__query_knowledge_files :: {{"query": "mooring lines", "knowledge_ids": ["{knowledge_id}"]}}',
                model=agent_model,
                chat_id=first.chat_id,
                parent_id=first.assistant_message_id,
            )
            stored = wait_for_reply(client, second)
            assert "north pier" in message_text(stored), stored


def test_an_attached_chat_file_is_readable_through_the_bridge(
    bridge_admin, agent_model, bridge_instance
):
    """File context is off for the agent model, so the attachment stays
    tool-shaped: tool_search finds the file family and view_file reads it."""
    with bridge_admin.client() as client:
        secret = f"the lamp burns all night {uuid.uuid4().hex[:6]}"
        uploaded = client.post(
            "/api/v1/files/",
            params={"process": "true", "process_in_background": "false"},
            files={"file": ("keeper.txt", secret.encode(), "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        file_id = uploaded.json()["id"]
        turn = send_message(
            client,
            'TOOL_CHAIN: tool_search :: {"query": "view file"} -> mcp__owui__view_file :: {"file_id": "'
            + file_id
            + '"}',
            model=agent_model,
            chat_files=[{"type": "file", "id": file_id, "name": "keeper.txt"}],
        )
        stored = wait_for_reply(client, turn)
        assert secret in message_text(stored), stored


def test_image_generation_bridges_and_lands_in_the_message(
    bridge_admin, agent_model, bridge_instance, listener
):
    """generate_image runs through the bridge to the configured engine and the
    generated file lands on the assistant message the way the frontend
    renders it."""
    with bridge_admin.client() as client:
        settings = serve_automatic1111(listener)
        save_image_settings(client, **{**settings, "ENABLE_IMAGE_GENERATION": True})
        turn = send_message(
            client,
            'TOOL: mcp__owui__generate_image :: {"prompt": "a lighthouse at dusk"}',
            model=agent_model,
            features={"image_generation": True},
        )
        stored = wait_for_reply(client, turn)
        files = [item for item in stored.get("files") or []] or [
            item for item in stored.get("output") or [] if isinstance(item, dict) and item.get("type") == "file"
        ]
        assert files, f"no generated image reached the message: {stored}"
        assert listener.received, "the image engine never got the generation call"


def test_execute_code_runs_on_the_jupyter_kernel_through_the_bridge(
    bridge_admin, agent_model, bridge_instance, kernel
):
    """execute_code reaches the configured jupyter kernel over the bridge."""
    with bridge_admin.client() as client:
        current = client.get("/api/v1/configs/code_execution").json()
        client.post(
            "/api/v1/configs/code_execution",
            json={
                **current,
                "ENABLE_CODE_INTERPRETER": True,
                "CODE_INTERPRETER_ENGINE": "jupyter",
                "CODE_INTERPRETER_JUPYTER_URL": kernel.base_url,
                "CODE_INTERPRETER_JUPYTER_AUTH": "",
            },
        ).raise_for_status()
        turn = send_message(
            client,
            'TOOL: mcp__owui__execute_code :: {"code": "print(21 * 2)"}',
            model=agent_model,
            features={"code_interpreter": True},
        )
        stored = wait_for_reply(client, turn)
        assert "42" in message_text(stored), stored
        assert kernel.executed_cells(), f"the kernel never ran a cell: {kernel.executed_cells()}"


# ---------------------------------------------------------------------------
# The Tools registry and the MCP relay over the bridge.
# ---------------------------------------------------------------------------


def test_a_workspace_tool_runs_over_the_bridge(bridge_admin, agent_model, bridge_instance):
    """A Tools registry entry the chat selected runs through the bridge with
    its arguments, and the tool's answer reaches the model."""
    with bridge_admin.client() as client:
        created = client.post(
            "/api/v1/tools/create",
            json={
                "id": f"tool_{uuid.uuid4().hex[:8]}",
                "name": "Bridge shout",
                "content": SHOUT_TOOL,
                "meta": {"description": "Bridge shout"},
                "access_grants": [EVERYONE_READS],
            },
        )
        assert created.status_code == 200, created.text
        tool_id = created.json()["id"]
        try:
            turn = send_message(
                client,
                'TOOL: mcp__owui__shout :: {"text": "ahoy"}',
                model=agent_model,
                tool_ids=[tool_id],
            )
            stored = wait_for_reply(client, turn)
            assert "AHOY" in message_text(stored), stored
        finally:
            client.delete(f"/api/v1/tools/id/{tool_id}/delete")


def test_the_mcp_relay_executes_a_configured_tool_server(
    bridge_admin, agent_model, bridge_instance, preserve
):
    """A configured MCP connection is relayed: the backend dials the server,
    the sidecar reaches it only through the bridge, discovered by tool_search."""
    with serving_echo_mcp() as url, bridge_admin.client() as client:
        server_id = f"pier_{uuid.uuid4().hex[:8]}"
        saved = client.post(TOOL_SERVERS[1], json={"TOOL_SERVER_CONNECTIONS": [_mcp_connection(url, server_id)]})
        assert saved.status_code == 200, saved.text
        turn = send_message(
            client,
            'TOOL_CHAIN: tool_search :: {"query": "echo tool server"} -> mcp__owui__'
            + server_id
            + '_echo :: {"text": "the lamp is lit"}',
            model=agent_model,
            tool_ids=[f"server:mcp:{server_id}"],
        )
        stored = wait_for_reply(client, turn)
        assert "the lamp is lit" in message_text(stored), stored


def test_a_tool_the_user_cannot_read_never_bridges(bridge_admin, agent_model, bridge_instance):
    """Ownership scoping: a workspace tool without access grants is absent
    from the bridge's surface, and the scripted call for it fails closed."""
    person = create_user(
        bridge_instance,
        name="Bridge Caller",
        email=f"bridge-caller-{uuid.uuid4().hex[:6]}@example.com",
    )
    with bridge_admin.client() as client:
        created = client.post(
            "/api/v1/tools/create",
            json={
                "id": f"tool_{uuid.uuid4().hex[:8]}",
                "name": "Whisper",
                "content": FOREIGN_TOOL,
                "meta": {"description": "Whisper"},
            },
        )
        assert created.status_code == 200, created.text
        tool_id = created.json()["id"]
        try:
            with person.client() as person_client:
                turn = send_message(
                    person_client,
                    "TOOL: mcp__owui__whisper :: {}",
                    model=agent_model,
                    tool_ids=[tool_id],
                )
                stored = wait_for_reply(person_client, turn)
                text = message_text(stored)
                assert "the secret" not in text, stored
                assert "was not offered" in text or "not bridged" in text, stored
        finally:
            client.delete(f"/api/v1/tools/id/{tool_id}/delete")


# ---------------------------------------------------------------------------
# The sync-tool offload guard.
# ---------------------------------------------------------------------------


def test_a_sync_tool_that_calls_back_to_this_server_completes(
    bridge_admin, agent_model, bridge_instance
):
    """A plain-Python workspace tool blocking on a loopback call back to this
    very server must not wedge the single uvicorn worker: the tool runs on a
    worker thread and its result reaches the model (trunk ran it on the loop
    and deadlocked)."""
    with bridge_admin.client() as client:
        created = client.post(
            "/api/v1/tools/create",
            json={
                "id": f"tool_{uuid.uuid4().hex[:8]}",
                "name": "Bridge probe",
                "content": BLOCKING_PROBE_TOOL.replace("__BASE__", bridge_instance.base_url),
                "meta": {"description": "Bridge probe"},
                "access_grants": [EVERYONE_READS],
            },
        )
        assert created.status_code == 200, created.text
        tool_id = created.json()["id"]
        try:
            started = time.monotonic()
            turn = send_message(
                client,
                "TOOL: mcp__owui__probe :: {}",
                model=agent_model,
                tool_ids=[tool_id],
            )
            stored = wait_for_reply(client, turn, timeout=45)
            elapsed = time.monotonic() - started
            assert "probe:200" in message_text(stored), stored
            assert elapsed < 40, f"the blocking self-call wedged the worker: {elapsed:.1f}s"
        finally:
            client.delete(f"/api/v1/tools/id/{tool_id}/delete")


# ---------------------------------------------------------------------------
# The signed binding and its denial path.
# ---------------------------------------------------------------------------


def test_the_bridge_refuses_calls_without_a_usable_binding(bridge_admin, agent_model, bridge_instance):
    """No token, a foreign signature, and an expired token all answer 401 —
    a compromised sidecar holds one binding, not an identity."""
    url = _bridge_url(bridge_instance)
    with httpx.Client(timeout=30) as client:
        no_token = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert no_token.status_code == 401, no_token.text

        garbage = client.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer not-a-token"},
        )
        assert garbage.status_code == 401, garbage.text

        foreign = client.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer " + _sign({"user_id": "x", "exp": 9999999999}, "other-secret")},
        )
        assert foreign.status_code == 401, foreign.text

        expired = client.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": "Bearer "
                + _sign({"user_id": "u", "chat_id": "c", "exp": 1}, os.environ.get("WEBUI_SECRET_KEY", "test"))
            },
        )
        assert expired.status_code == 401, expired.text


def test_the_bridge_answers_a_signed_call_directly(bridge_admin, agent_model, bridge_instance):
    """A correctly signed binding lists the bridged surface and executes one
    tool: the same contract the sidecar's MCP connection uses, asserted at
    the wire level."""
    from harness.instance import WEBUI_SECRET_KEY as INSTANCE_SECRET

    claims = {
        "chat_id": "",
        "user_id": bridge_admin.id,
        "message_id": "",
        "session_id": "",
        "model_id": "",
        "tool_ids": [],
        "files": [],
        "exp": int(time.time()) + 600,
    }
    headers = {"Authorization": "Bearer " + _sign(claims, INSTANCE_SECRET)}
    with httpx.Client(timeout=60, base_url=bridge_instance.base_url, headers=headers) as raw:
            listing = raw.post("/loopback/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert listing.status_code == 200, listing.text
            tools = listing.json()["result"]["tools"]
            names = {tool["name"] for tool in tools}
            assert "search_knowledge_bases" in names, names
            assert "list_knowledge_bases" in names, names
            # The chat-feature gate rides the binding: nothing carries the
            # web/memory/image/code features in this binding, so those stay off.
            assert "search_web" not in names, names
            assert "ask_user" not in names, names

            search = raw.post(
                "/loopback/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "search_knowledge_bases", "arguments": {"query": "harbour"}},
                },
            )
            assert search.status_code == 200, search.text
            result = search.json()["result"]
            assert result.get("isError") is False, result
            assert result["content"], result
