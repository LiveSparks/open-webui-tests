"""The day-one prompt head reaches pi's turn unchanged (PR8, issue 2).

The must-tier surfaces that ride the prompt head — the voice-mode prompt, the
spoken-transcript merge, memory injections, web search results, skills, and
the terminal AGENTS.md — are assembled in `process_chat_payload` in Python and
handed to the sidecar wholesale (ADR 0005): the head lands as the session's
system prompt and the new turn lands as the prompt. The scripted provider
fake hosts every leg, so each scenario asserts the roundtrip it scripted.

Every scenario but the last two assembles the same head at trunk `dev` and at
the PR head on the same fixture; `scripts/pr8_prompt_head_parity.py` runs
both legs and diffs the captures. Two contract changes are documented, so
they are diffed against the trunk lack instead of trunk equality:

- the spoken-transcript merge: pi owns history, so the merge rides the turn
  as an injection item sourced from OWUI's DB instead of the full-history
  pass whose result the dispatch drops (docs/research/voice-inlet-outlet.md);
- the terminal AGENTS.md: trunk inserts it as a user message before the
  first user turn, and the dispatch only prompts the new turn, so at the PR
  head it joins the head instead (drops nothing at trunk, but never reaches
  pi).

The trunk leg pins both absences so the diff reads as deliberate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.chat_history import seed_chat
from harness.instance import LaunchedInstance
from harness.knowledge_bases import add_text_file, knowledge_base
from harness.listener import json_answer, listening
from harness.search_apis import search_apis_env, serving_search_apis
from harness.skill_files import create as create_skill
from harness.terminal_server import TERMINAL_SERVERS_CONFIG, configure_terminals, serving_terminal
from harness.web_retrieval import save_web_settings, serve_search_results, web_settings_restored

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

ROLE = os.environ.get("PR8_PARITY_ROLE", "head")
assert ROLE in ("head", "trunk"), f"PR8_PARITY_ROLE must be head or trunk, got {ROLE!r}"

AGENT_MODEL_ID = "agent-loop.test"
CONTEXT_MODEL_ID = "agent-loop-context.test"  # same loop, file context on, for the RAG scenarios
SIDECAR_PORT = 5008  # the boot recipe derives the fake's port from this one
FAKE_PORT = 18930 + (SIDECAR_PORT - 8930)  # 15008 — the PR8 lane fake port
LANE_DIR = f"/tmp/agent_prompt_head-{ROLE}"
TESTS_REPO = Path(__file__).resolve().parents[2]

HISTORICAL_VOICE_TRANSCRIPT = "[Historical voice assistant transcript; not current task status]"
VOICE_MODE_PROBE = "Everything you say will be spoken aloud."
MEMORY_PROBE = "keeps quiet harbour moorings {token}"
SKILL_BODY = "Skill probe: greet every turn with the harbour wordboats."
AGENTS_MD = "AGENTS.md probe: answer with the wordbang."
SPOKEN_TRANSCRIPT = "The tide turns at six, sailor. SPOKEN {token}"
HARBOUR_PAGE = b"<html><head><title>Ferry times</title></head><body><p>The ferry leaves at noon.</p></body></html>"
KB_TEXT = "The kodeprobe grows in the harbour's north wall crevice."
WRITING_GRANT = {"principal_type": "user", "principal_id": "*", "permission": "read"}


def _lane_env() -> dict:
    return {**os.environ, "AGENT_SIDECAR_LANE_DIR": LANE_DIR, "PR8_PARITY_ROLE": ROLE}


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
    subprocess.run(["pkill", "-f", "[o]penai_fake.*15008"], check=False)
    time.sleep(0.5)
    _boot_sidecar()
    yield {"port": SIDECAR_PORT}
    subprocess.run([str(boot), "stop"], check=False, env=_lane_env())


@pytest.fixture(scope="module")
def apis() -> Iterator[dict]:
    with serving_search_apis() as serving:
        yield serving


@pytest.fixture(scope="module")
def head_instance(instance_with, apis, sidecar) -> LaunchedInstance:
    fake_port = 18930 + (sidecar["port"] - 8930)
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": f"{AGENT_MODEL_ID},{CONTEXT_MODEL_ID}",
            "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{fake_port}/v1",
            "OPENAI_API_KEYS": "sk-lane",
            # the web search scenario fetches its page locally
            "ENABLE_LOCAL_WEB_FETCH": "true",
            **search_apis_env(apis),
        }
    )


@pytest.fixture(scope="module")
def admin(head_instance: LaunchedInstance):
    return admin_of(head_instance)


def _agent_model(client: httpx.Client) -> str:
    """The agent-loop presets every account may chat with on the leg's fake."""
    client.post("/api/v1/models/model/access/update", json={"id": "fake-model", "name": "fake-model", "access_grants": [WRITING_GRANT]}).raise_for_status()
    for model_id, name, caps in (
        (AGENT_MODEL_ID, "Agent Loop Test", {"capabilities": {"file_context": False}}),
        (CONTEXT_MODEL_ID, "Agent Loop Context Test", {"capabilities": {}}),
    ):
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": model_id,
                "name": name,
                "base_model_id": "fake-model",
                "meta": caps,
                "params": {},
            },
        )
        assert created.status_code == 200 or "already registered" in created.text, created.text
        client.post(
            "/api/v1/models/model/access/update",
            json={"id": model_id, "name": name, "access_grants": [WRITING_GRANT]},
        ).raise_for_status()
    client.get("/api/models", params={"refresh": "true"}).raise_for_status()
    return AGENT_MODEL_ID


@pytest.fixture
def agent_model(admin) -> str:
    with admin.client() as client:
        return _agent_model(client)


def scenario_token(name: str) -> str:
    return f"pr8-{name}-{uuid.uuid4().hex[:8]}"


def server_tail(instance: LaunchedInstance) -> str:
    try:
        text = instance.log_path.read_text()
        Path("/tmp/swarm-pr8/owner").mkdir(parents=True, exist_ok=True)
        Path("/tmp/swarm-pr8/owner/instance.log").write_text(text)
        return f"(full copy at /tmp/swarm-pr8/owner/instance.log; last lines: {text[-600:]})"
    except Exception as error:  # pragma: no cover - the log outlives the scenario normally
        return f"(instance log unreadable: {error})"


# ---------------------------------------------------------------------------
# Capture: what the sidecar session hosts for a scripted chat.
# ---------------------------------------------------------------------------


def session_file(lane_dir: str, chat_id: str) -> Path:
    sessions = Path(lane_dir) / "agent_sessions" / chat_id
    files = sorted(sessions.glob("*.jsonl"))
    assert files, f"no sidecar session jsonl for chat {chat_id} under {sessions}"
    return files[-1]


def session_entries(lane_dir: str, chat_id: str) -> list[dict]:
    return [json.loads(line) for line in session_file(lane_dir, chat_id).read_text().splitlines() if line.strip()]


def head_preamble(lane_dir: str, chat_id: str) -> str:
    """The session's system prompt — the head OWUI handed off wholesale."""
    preambles = [
        (entry.get("message", {}).get("sections") or {}).get("preamble") or ""
        for entry in session_entries(lane_dir, chat_id)
        if entry.get("type") == "message" and (entry.get("message") or {}).get("role") == "system"
    ]
    return preambles[-1] if preambles else ""


def turn_texts(lane_dir: str, chat_id: str) -> list[str]:
    """Every user turn the session hosted, in order."""
    turns = []
    for entry in session_entries(lane_dir, chat_id):
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
        turns.append(content if isinstance(content, str) else json.dumps(content))
    return turns


def last_turn(lane_dir: str, chat_id: str) -> str:
    turns = turn_texts(lane_dir, chat_id)
    assert turns, f"chat {chat_id} hosted no user turns"
    return turns[-1]


def capture(lane_dir: str, name: str, chat_id: str, reply: dict | None) -> None:
    """Record the scenario's assembled head and turns for the parity diff."""
    captures = Path(lane_dir) / "captures.json"
    recorded = json.loads(captures.read_text()) if captures.is_file() else {}
    recorded[name] = {
        "preamble": head_preamble(lane_dir, chat_id),
        "turns": turn_texts(lane_dir, chat_id),
        "reply": message_text(reply) if reply else "",
    }
    captures.write_text(json.dumps(recorded, indent=2))


def normalize(text: str) -> str:
    """Fold what differs between legs anyway: lane paths, listener ports, and
    the per-leg identifiers (turn tokens, skill ids, knowledge base ids)."""
    text = text.replace(LANE_DIR, "<lane>")
    text = re.sub(r"http://127\.0\.0\.1:\d+", "<listener>", text)
    text = re.sub(r"pr8head-[0-9a-f]{6}", "<skill>", text)
    text = re.sub(r"pr8-[a-z-]+-[0-9a-f]{8}", "<token>", text)
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text)
    return re.sub(r"<cwd>\n.*\n</cwd>", "<cwd/>", text, flags=re.DOTALL)


def message_text(message: dict) -> str:
    return "".join(
        part.get("text", "")
        for item in (message.get("output") or [])
        if item.get("type") == "message"
        for part in item.get("content", [])
    )


# ---------------------------------------------------------------------------
# Scenario A: the voice-mode prompt rides the head.
# ---------------------------------------------------------------------------


def test_voice_mode_prompt_rides_the_head(admin, agent_model):
    token = scenario_token("voice")
    with admin.client() as client:
        turn = send_message(client, f"ECHO: {token}", model=agent_model, features={"voice": True})
        stored = wait_for_reply(client, turn)
    assert token in message_text(stored), stored
    preamble = head_preamble(LANE_DIR, turn.chat_id)
    assert VOICE_MODE_PROBE in preamble, f"the voice-mode prompt never rode the head ({ROLE}): {preamble[:400]}"
    capture(LANE_DIR, "voice-prompt", turn.chat_id, stored)


# ---------------------------------------------------------------------------
# Scenario B: memory injections ride the head.
# ---------------------------------------------------------------------------


def test_memory_injection_rides_the_head(admin, agent_model):
    token = scenario_token("memory")
    with admin.client() as client:
        added = client.post("/api/v1/memories/add", json={"content": MEMORY_PROBE.format(token=token), "type": "user"})
        assert added.status_code == 200, added.text
        turn = send_message(client, f"ECHO: {token}", model=agent_model, features={"memory": True})
        stored = wait_for_reply(client, turn)
    assert token in message_text(stored), stored
    preamble = head_preamble(LANE_DIR, turn.chat_id)
    assert token in preamble, f"the stored memory never rode the head ({ROLE}): {preamble[:400]}"
    assert "[User Memory]" in preamble, f"the memory block lost its trunk shape ({ROLE}): {preamble[:400]}"
    capture(LANE_DIR, "memory-injection", turn.chat_id, stored)


# ---------------------------------------------------------------------------
# Scenario C: the forced web search results ride the turn (legacy function
# calling, the pre-loop path; the nativeFC bridge leg is PR4's).
# ---------------------------------------------------------------------------


def test_web_search_results_ride_the_turn(admin, agent_model):
    """The pre-loop forced search (legacy function calling): the hits become the
    RAG context that rides the turn, exactly as trunk assembles it."""
    with listening() as listener:
        listener.route("GET", "/ferry", (200, {"Content-Type": "text/html"}, HARBOUR_PAGE))
        page = f"{listener.base_url}/ferry"
        settings = serve_search_results(listener, [page])
        with admin.client() as client, web_settings_restored(client):
            # the result snippet embeds on its own, the way the kit's web search
            # tests drive it; no page load and no collection query in between
            save_web_settings(client, BYPASS_WEB_SEARCH_WEB_LOADER=True, **settings)
            turn = send_message(
                client,
                "what about the ferry harbour",
                model=CONTEXT_MODEL_ID,
                features={"web_search": True},
                params={"function_calling": "legacy"},
            )
            stored = wait_for_reply(client, turn, timeout=60.0)
    turn_text = normalize("\n".join(turn_texts(LANE_DIR, turn.chat_id)))
    assert "snippet of <listener>" in turn_text, (
        f"the engine hit never rode the turn ({ROLE}): {turn_text[-600:]}; "
        f"status: {json.dumps((stored or {}).get('statusHistory'))[:400]}; "
        f"stored: {json.dumps(stored, default=str)[:600]}"
    )
    capture(LANE_DIR, "web-search-results", turn.chat_id, stored)


# ---------------------------------------------------------------------------
# Scenario D: skills ride the head.
# ---------------------------------------------------------------------------


def test_skill_rides_the_head(admin, agent_model):
    token = scenario_token("skills")
    with admin.client() as client:
        skill = create_skill(
            admin,
            {"SKILL.md": f"---\nname: harbourprobe\n---\n{SKILL_BODY}"},
            skill_id=f"pr8head-{uuid.uuid4().hex[:6]}",
        )
        turn = send_message(client, f"ECHO: {token} <${skill['id']}|harbour skill>", model=agent_model)
        stored = wait_for_reply(client, turn)
    preamble = head_preamble(LANE_DIR, turn.chat_id)
    assert f'<skill id="{skill["id"]}"' in preamble, f"the skill never rode the head ({ROLE}): {preamble[:400]}"
    assert token in message_text(stored), stored
    capture(LANE_DIR, "skills-head", turn.chat_id, stored)


# ---------------------------------------------------------------------------
# Scenario E: the terminal AGENTS.md rides the head. Diffed against the trunk
# lack, not trunk equality — the dispatch drops trunk's user insert.
# ---------------------------------------------------------------------------

AGENTS_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "terminal", "version": "1"},
    "paths": {
        "/execute": {"post": {"operationId": "run_command", "responses": {"200": {"description": "ok"}}}}
    },
}


def test_terminal_agents_md_rides_the_head(admin, agent_model, preserve, head_instance):
    token = scenario_token("agentsmd")
    preserve(TERMINAL_SERVERS_CONFIG)
    with serving_terminal() as server:
        server.route("GET", "/openapi.json", json_answer(AGENTS_SPEC))
        server.route("GET", "/files/cwd", json_answer({"cwd": "/home/ada", "home": "/home/ada"}))
        server.route("GET", "/files/read", json_answer({"content": AGENTS_MD + "\n" + token}))
        connection = server.connection()
        with admin.client() as client:
            configure_terminals(client, connection)
            turn = send_message(
                client,
                f"ECHO: {token}",
                model=os.environ.get("PR8_PROBE_MODEL") or agent_model,
                terminal_id=connection["id"],
            )
            stored = wait_for_reply(client, turn, timeout=45.0)
            terminal_requests = [request.path for request in server.received]
    stored_state = message_text(stored) or json.dumps((stored or {}).get("content"))
    session_hit = Path(LANE_DIR, "agent_sessions", turn.chat_id).glob("*.jsonl")
    assert any(session_hit), (
        f"the turn never dispatched to the sidecar; reply: {stored_state[:400]}; "
        f"terminal requests: {terminal_requests}; server tail: {server_tail(head_instance)}"
    )
    preamble = head_preamble(LANE_DIR, turn.chat_id)
    turn_text = last_turn(LANE_DIR, turn.chat_id)
    if ROLE == "head":
        assert token in preamble, f"the machine's AGENTS.md never rode the head: {preamble[:500]}; reply: {stored_state[:300]}"
    else:
        assert token not in head_preamble(LANE_DIR, turn.chat_id), "trunk must not carry the AGENTS.md head ride"
        assert AGENTS_MD not in preamble
    assert AGENTS_MD not in turn_text, f"the AGENTS.md must ride the head, not the turn: {turn_text[:500]}"
    assert token in message_text(stored), stored
    capture(LANE_DIR, "terminal-agents-md", turn.chat_id, stored)


# ---------------------------------------------------------------------------
# Scenario F: the spoken-transcript merge rides the turn as an injection item.
# Diffed against the trunk lack — the full-history merge never reached the
# dispatch at trunk, and the trunk leg pins that.
# ---------------------------------------------------------------------------


def test_spoken_transcript_merge_rides_the_turn(admin, agent_model):
    token = scenario_token("voicehistory")
    spoken = SPOKEN_TRANSCRIPT.format(token=token)
    speech = [{"item_id": "item_0", "transcript": spoken, "interrupted": False}]
    history = [
        {"role": "user", "content": "what about the tide"},
        {
            "role": "assistant",
            "content": spoken,
            "model": agent_model,
            "meta": {"voice": {"model": agent_model, "input_item_id": "item_in", "speech": speech}},
        },
    ]
    with admin.client() as client:
        chat_id, last_id = seed_chat(client, history, model=agent_model)
        turn = send_message(client, f"ECHO: {token}", model=agent_model, chat_id=chat_id, parent_id=last_id, history=history)
        stored = wait_for_reply(client, turn)
    turn_text = last_turn(LANE_DIR, turn.chat_id)
    if ROLE == "head":
        assert spoken in turn_text, f"the spoken transcript never rode the turn: {turn_text[:600]}"
        assert token in message_text(stored), stored
    else:
        assert spoken not in turn_text, f"trunk must not carry the merge: {turn_text[:600]}"
        assert spoken not in head_preamble(LANE_DIR, turn.chat_id)
    capture(LANE_DIR, "spoken-transcript-merge", turn.chat_id, stored)


# A spoken transcript the frontend saved on a chat answer (meta.voice.speech on
# an assistant the voice model did not speak in full) merges after the answer:
# the trunk merge keeps both the answer and the transcript items.

def test_the_spoken_transcript_of_a_chat_answer_follows_it(admin, agent_model):
    token = scenario_token("answer-spoken")
    spoken = SPOKEN_TRANSCRIPT.format(token=token)
    answer = f"tide noted {token}"
    speech = [{"item_id": "item_1", "transcript": spoken, "interrupted": False}]
    history = [
        {"role": "user", "content": "what about the tide"},
        {"role": "assistant", "content": answer, "model": agent_model, "meta": {"voice": {
            "model": "spoken-voice-model", "input_item_id": "item_in", "speech": speech,
        }}},
    ]
    with admin.client() as client:
        chat_id, last_id = seed_chat(client, history, model=agent_model)
        turn = send_message(client, f"ECHO: {token}", model=agent_model, chat_id=chat_id, parent_id=last_id, history=history)
        stored = wait_for_reply(client, turn, timeout=45.0)
    turn_text = last_turn(LANE_DIR, turn.chat_id)
    if ROLE == "head":
        assert f"{HISTORICAL_VOICE_TRANSCRIPT}\n{spoken}" in turn_text, f"the transcript never rode the turn: {turn_text[:600]}"
        assert token in message_text(stored), stored
    else:
        assert spoken not in turn_text, f"trunk must not carry the merge: {turn_text[:600]}"
    capture(LANE_DIR, "spoken-answer-merge", turn.chat_id, stored)


# ---------------------------------------------------------------------------
# Scenario G: the model's attached knowledge tags ride the head.
# ---------------------------------------------------------------------------


def test_attached_knowledge_tags_ride_the_head(admin, agent_model):
    """The attached knowledge tags join the head via the module's preset, then
    leave again, so no other scenario rides them."""
    token = scenario_token("knowledge")
    preset = {
        "id": agent_model,
        "name": "Agent Loop Test",
        "base_model_id": "fake-model",
        "meta": {"capabilities": {"file_context": False}},
        "params": {},
    }
    with admin.client() as client, knowledge_base(client, f"kode {token}") as knowledge_id:
        add_text_file(client, knowledge_id, "kode.md", KB_TEXT)
        with_knowledge = client.post("/api/v1/models/model/update", json={**preset, "meta": {
            "capabilities": {"file_context": False},
            "knowledge": [{"type": "collection", "id": knowledge_id, "name": f"kode {token}"}],
        }})
        assert with_knowledge.status_code == 200, with_knowledge.text
        client.get("/api/models", params={"refresh": "true"}).raise_for_status()
        try:
            turn = send_message(client, f"ECHO: {token}", model=agent_model)
            stored = wait_for_reply(client, turn, timeout=45.0)
        finally:
            client.post("/api/v1/models/model/update", json=preset).raise_for_status()
            client.get("/api/models", params={"refresh": "true"}).raise_for_status()
    preamble = head_preamble(LANE_DIR, turn.chat_id)
    stored_state = message_text(stored) or json.dumps((stored or {}).get("content"))
    assert "<knowledge type=" in preamble, f"the attached knowledge tags never rode the head: {preamble[:400]}; reply: {stored_state[:300]}"
    capture(LANE_DIR, "attached-knowledge", turn.chat_id, stored)
