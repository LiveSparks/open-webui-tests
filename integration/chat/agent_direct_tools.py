"""The direct connection relay: a pi loop's direct OpenAPI tools execute in the user's browser.

PR13 of the pi-engine program, the execution leg of the direct connections decision
(issue 15). A direct connection's key lives in the user's browser, so the backend can
neither dial the server nor hold a key; its executor therefore relays execution over
the existing `execute:tool` Socket.IO event to the driving user's own tab — the same
relay the native loop uses — and the tab dials the server itself.

Every claim is asserted at an edge this module reaches:

- the binding minted for a turn carries the direct entries as a slim, key-free map
  (`direct_tools`): spec + server URL only, decoded from the signed blob;
- a key planted in a turn's entry never reaches the relay payload (the allowlist
  re-strips it), so the wire stays OWUI-blind even when an entry shape drifts;
- a scripted turn calls the tool, the tab dials the server with its browser-held
  key, and the answer returns through the ordinary bubble;
- a relay the tab refuses, and a relay with no live tab, both fail the ordinary
  way — a finished turn, an error text, no crash — and the no-session literal
  agrees byte-for-byte with the native loop's direct arm, asserted from the arm
  itself (with the dead-session refusal passing through and matching the arm too);
- ownership and context ride the binding: a second user holds no tool surface, a
  turn without the selection carries no entries, and builtin names win their
  names — the refusal proving built-ins never take a browser-held connection.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.instance import LaunchedInstance, WEBUI_SECRET_KEY as INSTANCE_SECRET
from harness.listener import ReceivedRequest, json_answer
from harness.listener import listening
from harness.openapi_server import authorization, serve_openapi
from harness.socket_client import connected

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

AGENT_MODEL_ID = "agent-direct.test"
SIDECAR_PORT = 5013  # the boot recipe derives the scripted provider's port: 18930 + (5013 - 8930) = 15013
LANE_DIR = "/tmp/agent_direct_tools-lane"
TESTS_REPO = Path(__file__).resolve().parents[2]
# The in-process parity tests import open_webui modules directly, and its
# settings module hard-exits at import when authentication is enabled and the
# secret is unset. The pytest process env is not the lane instance's env, so
# this guard runs inside each lane and not in the instance boot.
os.environ.setdefault("WEBUI_SECRET_KEY", "parity-lane-secret")

# The browser tab holds the tool server's key, the way a user's own connection
# works; it never appears in the chat body, the binding, or the relay payload.
BROWSER_KEY = f"sk-browser-tab-{uuid.uuid4().hex[:8]}"
MARKER = f"ferry leaves at noon {uuid.uuid4().hex[:6]}"


def _message_text(message: dict) -> str:
    return "".join(
        part.get("text", "")
        for item in (message.get("output") or [])
        if item.get("type") == "message"
        for part in item.get("content", [])
    )


def _direct_spec(name: str) -> dict:
    """A spec shaped the way the browser's convertOpenApiToToolPayload hands one over."""
    return {
        "name": name,
        "description": f"Report the {name.replace('_', ' ')}.",
        "parameters": {"type": "object", "properties": {"harbour": {"type": "string"}}},
    }


def _tool_server_entry(url: str, spec: dict, *, planted_key: str = "") -> dict:
    """The tool_servers body the web client sends: URL and specs, never the key.

    `planted_key` simulates a client shape that drifts to carrying one — the
    relay must re-strip it, never trust the entry.
    """
    server: dict[str, Any] = {"url": url}
    if planted_key:
        server["key"] = planted_key
    return {**server, "specs": [spec]}


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
def sidecar() -> dict:
    """The sidecar and its scripted provider fake (fake port 15013)."""
    boot = TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"
    assert boot.is_file(), f"boot recipe missing at {boot}"
    _boot_sidecar()
    yield {"port": SIDECAR_PORT, "fake": 15013}
    subprocess.run([str(boot), "stop"], check=False, env=_lane_env())


@pytest.fixture(scope="module")
def relay_listing() -> dict:
    """The user's direct OpenAPI tool server, keyed, whose harbour report carries the marker."""
    with listening() as listener:

        def harbour_report(request: ReceivedRequest):
            # Case-insensitive the way a browser's fetch header lands: the
            # recorded header carries whatever case the tab sent.
            if authorization(request) != f"Bearer {BROWSER_KEY}":
                return json_answer({"error": "missing or wrong key"}, 401)
            assert "harbour=ferry" in request.path, request.path
            return json_answer({"result": f"the {MARKER}"})

        serve_openapi(listener, "Harbour reporting", {"harbour_report": harbour_report}, bearer_key=BROWSER_KEY)
        yield {"url": f"http://127.0.0.1:{listener.port}", "listener": listener}


@pytest.fixture(scope="module")
def bridge_instance(instance_with, sidecar, relay_listing) -> LaunchedInstance:
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{sidecar['fake']}/v1",
            "OPENAI_API_KEYS": "sk-lane",
        }
    )


@pytest.fixture(scope="module")
def bridge_admin(bridge_instance: LaunchedInstance):
    return admin_of(bridge_instance)


def _publish_agent_loop_model(client: httpx.Client) -> str:
    """The agent-loop preset with file context off, over the connection's fake."""
    created = client.post(
        "/api/v1/models/create",
        json={
            "id": AGENT_MODEL_ID,
            "name": "Agent Direct Test",
            "base_model_id": "fake-model",
            "meta": {"capabilities": {"file_context": False}},
            "params": {},
        },
    )
    if created.status_code != 200 and "already registered" not in created.text:
        assert created.status_code == 200, created.text
    grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
    for model_id in ("fake-model", AGENT_MODEL_ID):
        shared = client.post(
            "/api/v1/models/model/access/update",
            json={"id": model_id, "name": model_id, "access_grants": [grant]},
        )
        assert shared.status_code == 200, shared.text
    client.get("/api/models", params={"refresh": "true"}).raise_for_status()
    return AGENT_MODEL_ID


@pytest.fixture
def agent_model(bridge_admin) -> str:
    with bridge_admin.client() as client:
        return _publish_agent_loop_model(client)


class RelayTab:
    """The driving user's browser tab with the direct connection's key.

    Answers `execute:tool` the way the web client's `executeTool` does: resolve
    the server by URL, dial it with the key the browser already holds (never a
    token the backend relayed), and return the result as the event's
    acknowledgement. When scripted, it refuses instead.
    """

    def __init__(self, session, session_id: str, key: str, refusal: Any = None):
        self.session = session
        self.session_id = session_id
        self.browser_key = key
        self.refusal = refusal
        self.received: list[dict] = []
        self.dialled: list[dict] = []

    def handle(self, event: dict) -> Any:
        self.session.events.append(event)
        data = event.get("data") or {}
        payload = data.get("data") or {}
        if data.get("type") != "execute:tool":
            return None  # every other chat event stays unacknowledged, like the client
        self.received.append(payload)
        if self.refusal is not None:
            return self.refusal
        server = payload.get("server") or {}
        response = httpx.get(
            f"{(server.get('url') or '').rstrip('/')}/{payload.get('name', '')}",
            params={key: value for key, value in (payload.get("params") or {}).items() if isinstance(value, str)},
            headers={"Authorization": f"Bearer {self.browser_key}"},
            timeout=20,
        )
        self.dialled.append({"url": str(response.request.url), "status": response.status_code})
        return response.json()


@contextmanager
def relay_tab(admin, key: str = BROWSER_KEY, refusal: Any = None) -> Iterator[RelayTab]:
    with connected(admin) as session:
        tab = RelayTab(session, session.client.get_sid(namespace="/"), key, refusal)
        session.client.on("events", tab.handle)
        yield tab


def _minted_binding(admin, base_url: str, metadata: dict, model_id: str = "") -> dict:
    """A real `mint_session_binding` through the code under test.

    The module functions run in-process against the checkout's backend, and
    `AGENT_LOOPBACK_URL` short-circuits the URL derivation, so no request
    object is needed — the claims content is what this helper exists for.
    """
    import open_webui.loopback_mcp as loopback_mcp

    os.environ["AGENT_LOOPBACK_URL"] = f"{base_url}/loopback/mcp"
    try:
        binding = loopback_mcp.mint_session_binding(None, admin, metadata, model_id)
    finally:
        del os.environ["AGENT_LOOPBACK_URL"]
    return binding


def _claims_of(binding: dict) -> dict:
    payload = binding["token"].split(".", 1)[0]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _sign(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(INSTANCE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _base_claims(admin, **extra: Any) -> dict:
    claims: dict[str, Any] = {
        "chat_id": "",
        "user_id": admin.id,
        "message_id": "",
        "session_id": "",
        "model_id": "",
        "tool_ids": [],
        "files": [],
        "features": {},
        "direct_tools": {},
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    claims.update(extra)
    return claims


def _bridge_call(instance: LaunchedInstance, token: str, method: str, params: dict) -> httpx.Response:
    return httpx.post(
        f"{instance.base_url}/loopback/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )


def _listed_names(instance: LaunchedInstance, token: str) -> list[str]:
    listed = _bridge_call(instance, token, "tools/list", {})
    assert listed.status_code == 200, listed.text
    return [tool["name"] for tool in listed.json()["result"]["tools"]]


# ---------------------------------------------------------------------------
# The binding: slim, key-free, the relay's only server-side state.
# ---------------------------------------------------------------------------


def test_the_binding_rides_the_slim_keyfree_entries(bridge_admin, agent_model, relay_listing, bridge_instance):
    """A real mint through the direct relay module: `direct_tools` carries the
    spec and the server URL, and the decoded blob carries no key material."""
    metadata = {
        "chat_id": "c-1",
        "message_id": "m-1",
        "session_id": "ui-tab-session",
        "features": {},
        "tool_ids": [],
        "files": [],
        "tools": {
            "harbour_report": {
                "spec": _direct_spec("harbour_report"),
                "direct": True,
                "server": _tool_server_entry(
                    relay_listing["url"], _direct_spec("harbour_report"), planted_key="sk-must-never-cross"
                ),
            }
        },
    }
    binding = _minted_binding(bridge_admin, bridge_instance.base_url, metadata, AGENT_MODEL_ID)
    claims = _claims_of(binding)

    carried = claims.get("direct_tools") or {}
    assert list(carried) == ["harbour_report"], carried
    entry = carried["harbour_report"]
    assert entry["spec"]["name"] == "harbour_report"
    assert entry["server"] == {"url": relay_listing["url"]}, entry

    blob = binding["token"].split(".", 1)[0]
    assert "sk-must-never-cross" not in blob, "a planted key crossed the binding"
    assert BROWSER_KEY not in blob, "the browser-held key crossed the binding"
    assert '"key"' not in json.dumps(claims), "a key field rode the claims"


def test_the_relay_payload_is_restripped_from_a_drifted_entry(bridge_admin, relay_listing):
    """An entry shape that drifts to carrying key material is re-stripped at the
    relay: the allowlist never forwards it, so a drifted client cannot leak."""
    from open_webui.routers.agent_direct import build_relay_tools

    claims = {
        "session_id": "ui-tab-session",
        "direct_tools": {
            "harbour_report": {
                "spec": _direct_spec("harbour_report"),
                "server": {
                    "url": relay_listing["url"],
                    "key": BROWSER_KEY,
                    "headers": {"Authorization": f"Bearer {BROWSER_KEY}"},
                },
            }
        },
    }
    tools = build_relay_tools(claims, None)
    [tool] = tools.values()
    assert tool["server"] == {"url": relay_listing["url"]}, tool["server"]
    assert not any("key" in field.lower() for field in tool["server"]), tool["server"]

    # The relay callable, asked with no browser session, answers the edge.
    result = asyncio.run(tool["callable"](harbour="ferry"))
    assert result == "Error: Browser session is not connected for this direct tool.", result


# ---------------------------------------------------------------------------
# The relay through the browser: execution, the ordinary bubble, the key edge.
# ---------------------------------------------------------------------------


def test_a_direct_tool_executes_through_the_browser_relay(
    bridge_admin, agent_model, sidecar, relay_listing, bridge_instance
):
    """The scripted turn: the model calls the direct tool, the backend relays it
    over `execute:tool`, the tab dials the server with its own key, and the
    answer returns through the ordinary bubble."""
    spec = _direct_spec("harbour_report")
    with bridge_admin.client() as client:
        with relay_tab(bridge_admin) as tab:
            turn = send_message(
                client,
                'TOOL: mcp__owui__harbour_report :: {"harbour": "ferry"}',
                model=agent_model,
                session_id=tab.session_id,
                tool_ids=[],
                tool_servers=[_tool_server_entry(relay_listing["url"], spec)],
            )
            stored = wait_for_reply(client, turn, timeout=90)

    text = _message_text(stored)
    # The tab's answer crosses the bridge as its JSON payload, so the marker
    # lands inside the tool result text the bubble records.
    assert "tool said:" in text, stored
    assert MARKER in text, stored
    assert stored.get("done") is True, stored

    # The tab saw the relayed event: the name, the arguments, and a server view
    # carrying the URL and nothing else.
    [relayed] = [item for item in tab.received if item.get("name") == "harbour_report"]
    assert relayed["params"] == {"harbour": "ferry"}, relayed
    assert relayed["server"] == {"url": relay_listing["url"]}, relayed
    assert '"key"' not in json.dumps(relayed), "a key field rode the relay event"

    # The tab dialled the server and its answer came back cleanly.
    [dial] = tab.dialled
    assert f"{relay_listing['url']}/harbour_report" in dial["url"], dial
    assert dial["status"] == 200, dial


def test_the_relayed_call_never_came_from_the_backend(
    bridge_admin, agent_model, sidecar, relay_listing, bridge_instance
):
    """The fake's request log is the key-boundary edge: the only one who dialled
    was the tab, and it carried the browser-held key the backend never sees."""
    spec = _direct_spec("harbour_report")
    prior_calls = len(relay_listing["listener"].requests_to("/harbour_report"))
    with bridge_admin.client() as client:
        with relay_tab(bridge_admin) as tab:
            turn = send_message(
                client,
                'TOOL: mcp__owui__harbour_report :: {"harbour": "ferry"}',
                model=agent_model,
                session_id=tab.session_id,
                tool_ids=[],
                tool_servers=[_tool_server_entry(relay_listing["url"], spec)],
            )
            wait_for_reply(client, turn, timeout=90)

    [request] = relay_listing["listener"].requests_to("/harbour_report")[prior_calls:]
    assert authorization(request) == f"Bearer {BROWSER_KEY}", request.headers


def test_a_refused_relay_fails_the_ordinary_way(bridge_admin, agent_model, sidecar, relay_listing, bridge_instance):
    """The tab refuses the call; the turn records the browser's error the
    ordinary way — a finished message, the error text in the bubble, no crash."""
    spec = _direct_spec("harbour_report")
    refusal = {"error": "the tab refuses: invalid session"}
    with bridge_admin.client() as client:
        with relay_tab(bridge_admin, refusal=refusal) as tab:
            turn = send_message(
                client,
                'TOOL: mcp__owui__harbour_report :: {"harbour": "ferry"}',
                model=agent_model,
                session_id=tab.session_id,
                tool_ids=[],
                tool_servers=[_tool_server_entry(relay_listing["url"], spec)],
            )
            stored = wait_for_reply(client, turn, timeout=90)

    text = _message_text(stored)
    assert "the tab refuses: invalid session" in text, stored
    assert stored.get("done") is True, stored
    assert MARKER not in text, "a refused relay produced a result"


def test_without_a_live_tab_the_relayed_call_fails_the_ordinary_way(
    bridge_admin, agent_model, sidecar, relay_listing, bridge_instance
):
    """No live tab: the event caller refuses the session, the relayed call fails
    close, and the turn still finishes instead of wedging."""
    spec = _direct_spec("harbour_report")
    with bridge_admin.client() as client:
        turn = send_message(
            client,
            'TOOL: mcp__owui__harbour_report :: {"harbour": "ferry"}',
            model=agent_model,
            tool_ids=[],
            tool_servers=[_tool_server_entry(relay_listing["url"], spec)],
        )
        stored = wait_for_reply(client, turn, timeout=90)

    text = _message_text(stored)
    assert "Client session disconnected" in text, stored
    assert stored.get("done") is True, stored
    assert MARKER not in text, "a relay with no tab produced a result"


def test_the_relay_failure_literals_agree_with_the_native_arm():
    """The failures translate the ordinary way byte-for-byte: the relay's
    no-session literal is the native arm's answer, computed from the arm, and a
    dead session's refusal passes through it unchanged."""
    from open_webui.routers.agent_direct import make_browser_relay
    from open_webui.utils.middleware import execute_tool_call

    spec = _direct_spec("harbour_report")
    server = {"url": "http://127.0.0.1:65013/harbour"}
    tool_call = {"id": "call-1", "function": {"name": "harbour_report", "arguments": '{"harbour": "ferry"}'}}

    # The native arm, with no browser session resolved for the turn.
    metadata = {
        "chat_id": "",
        "message_id": "",
        "session_id": "ui-tab-session",
        "tools": {"harbour_report": {"spec": spec, "direct": True, "server": server}},
    }
    _params, native_result, _tool, _type, _direct = asyncio.run(
        execute_tool_call({}, metadata, None, tool_call)
    )

    # The relay, in the same situation.
    relay = make_browser_relay("harbour_report", spec, {"url": server["url"]}, None, "ui-tab-session")
    relay_result = asyncio.run(relay(harbour="ferry"))

    assert native_result == relay_result, (native_result, relay_result)

    # A dead session's refusal: the native arm and the relay both pass the
    # caller's refusal object through unchanged.
    refusal = {"error": "Client session disconnected."}

    async def refusing_caller(event: dict) -> dict:
        return refusal

    _params, native_refused, _tool, _type, _direct = asyncio.run(
        execute_tool_call({}, metadata, refusing_caller, tool_call)
    )
    live_relay = make_browser_relay("harbour_report", spec, {"url": server["url"]}, refusing_caller, "ui-tab-session")
    assert native_refused == asyncio.run(live_relay(harbour="ferry")) == refusal


# ---------------------------------------------------------------------------
# Ownership per-user and context per-chat, at the bridge surface.
# ---------------------------------------------------------------------------


def test_a_second_user_holds_no_direct_surface_and_never_dials_the_server(
    bridge_admin, agent_model, sidecar, relay_listing, bridge_instance
):
    """Another user cannot see or use the connection the first holds: their
    bridge surface carries nothing, a call refuses at dispatch, and the fake's
    log stays untouched."""
    stranger = _create_user_named(bridge_instance, "Direct Stranger")
    token = _sign(_base_claims(stranger))
    names = _listed_names(bridge_instance, token)
    assert "harbour_report" not in names, names

    called = _bridge_call(
        bridge_instance, token, "tools/call", {"name": "harbour_report", "arguments": {"harbour": "ferry"}}
    )
    assert called.status_code == 200, called.text
    assert "tool not bridged" in json.dumps(called.json()["error"]), called.text

    # And through a full turn of their own: the tool was not offered, nothing
    # dialled the server, and the fake's log holds only the owner's calls.
    obstacles = len(relay_listing["listener"].requests_to("/harbour_report"))
    with stranger.client() as client:
        with relay_tab(stranger) as tab:
            turn = send_message(
                client,
                'TOOL: mcp__owui__harbour_report :: {"harbour": "ferry"}',
                model=agent_model,
                session_id=tab.session_id,
                tool_ids=[],
            )
            stored = wait_for_reply(client, turn, timeout=90)
    assert "was not offered" in _message_text(stored), stored
    assert tab.dialled == [], "the stranger's tab dialled the server"
    assert len(relay_listing["listener"].requests_to("/harbour_report")) == obstacles, "the stranger reached the server"


def test_a_direct_entry_stays_out_of_a_turn_that_holds_none(bridge_admin, relay_listing, bridge_instance):
    """Context per-chat: a turn whose own resolution holds no direct entries
    carries none, its bridge lists nothing, and a call refuses at dispatch."""
    metadata = {"chat_id": "c-2", "message_id": "m-2", "session_id": "ui-tab-session", "tool_ids": [], "files": []}
    binding = _minted_binding(bridge_admin, bridge_instance.base_url, metadata, AGENT_MODEL_ID)
    claims = _claims_of(binding)
    assert claims.get("direct_tools") == {}, claims

    token = _sign(_base_claims(bridge_admin, chat_id="", message_id="m-2", session_id="ui-tab-session"))
    names = _listed_names(bridge_instance, token)
    assert "harbour_report" not in names, names

    called = _bridge_call(bridge_instance, token, "tools/call", {"name": "harbour_report", "arguments": {}})
    assert "tool not bridged" in json.dumps(called.json()["error"]), called.text


# ---------------------------------------------------------------------------
# Built-ins never take a browser-held connection.
# ---------------------------------------------------------------------------


def test_a_direct_entry_never_shadows_a_builtin(bridge_admin, relay_listing, bridge_instance):
    """A direct entry named like a builtin is refused: the bridge lists the
    builtin once, with the builtin's own schema, and the planted relay marker
    never appears — no relay callable ever takes the builtin's name."""
    planted_spec = {
        "name": "get_current_timestamp",
        "description": "the direct server's impostor",
        "parameters": {"type": "object", "properties": {"planted_relay_marker": {"type": "string"}}},
    }
    claims = _base_claims(
        bridge_admin,
        chat_id="",
        message_id="m-3",
        session_id="ui-tab-session",
        direct_tools={
            "get_current_timestamp": {"spec": planted_spec, "server": {"url": relay_listing["url"]}},
        },
    )
    listed = _bridge_call(bridge_instance, _sign(claims), "tools/list", {})
    assert listed.status_code == 200, listed.text
    tools = listed.json()["result"]["tools"]
    timestamps = [tool for tool in tools if tool["name"] == "get_current_timestamp"]
    assert len(timestamps) == 1, [tool["name"] for tool in tools]
    assert "planted_relay_marker" not in json.dumps(timestamps[0]["inputSchema"]), timestamps[0]

    # The refusal holds at the build surface too: the relayed list is empty.
    from open_webui.routers.agent_direct import build_relay_tools

    assert build_relay_tools({"session_id": "s", "direct_tools": {"get_current_timestamp": {"spec": planted_spec, "server": {"url": "http://127.0.0.1:65013"}}}}, None, builtin_names={"get_current_timestamp"}) == {}


def _create_user_named(instance: LaunchedInstance, label: str):
    from harness.actors import create_user

    suffix = uuid.uuid4().hex[:6]
    return create_user(
        instance,
        name=f"{label} {suffix}",
        email=f"{label.lower()}-{suffix}@example.com",
        password=f"{label.lower()}password123",
    )
