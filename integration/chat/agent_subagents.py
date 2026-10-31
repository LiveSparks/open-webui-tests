"""A delegation spawns a second in-process pi session in the sidecar (PR9).

The sub-agent decision (issue 10) and the delegation mechanics (issue 18):
`delegate_task` stays the tool contract, and a delegation is a child pi session
the sidecar hosts next to the parent, built from the parent's model, the chat's
head, and the admin sub-agent prompt, with the full bridge minus delegation and
memory mutation. The default assistant-turn budget is 256 — the same boundary
an ordinary loop gets — carried as `maxTurns` and realized as a finishTurn hook
on the child session; background children inherit the machine surface like
foreground ones and return through the unchanged
`process_pending_internal_messages` flow. Eviction (issue 29, ADR 0006) guards
a queued child and the parent that awaits a background result through the
parent-child edge the spawn registers on the sidecar; a crash borrows the
guard: the child's turn errors with the parent reachable on the record, and no
orphan child survives the restart.

The turn cap, the child edges, and the sweep are asserted at the sidecar
surface too (`sidecar/spec`); these checks are the HTTP-side half: what the
delegation leaves in the chats and what the child's provider requests carry.

Every task names its own marker, so a run's provider requests line up with
the delegation that caused them.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.instance import LaunchedInstance

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

SIDECAR_PORT = 8956
LANE_DIR = "/tmp/agent_subagents-lane"
TESTS_REPO = Path(__file__).resolve().parents[2]
AGENT_MODEL_ID = "agent-subagents.test"
SUBAGENTS_ROUTES = ("/api/v1/configs/subagents", "/api/v1/configs/subagents")
FAKE_REQUEST_LOG = Path(LANE_DIR) / "fake-requests.log"

# The default head the delegation hands the child when the admin leaves the
# prompt empty (utils/subagents.py DEFAULT_SUBAGENT_SYSTEM_PROMPT).
DEFAULT_HEAD_PROBE = "sub-agent working on a specific task assigned by the lead agent"
# A deliberate custom head, so the spawn proves it reads the admin value.
ADMIN_HEAD = "You are the PR9 probe sub-agent: name the harbour."


# ---------------------------------------------------------------------------
# Fixtures: the sidecar under test, the instance on it, the agent-loop model.
# ---------------------------------------------------------------------------


def _checkout() -> Path:
    checkout = Path(os.environ.get("OPEN_WEBUI_CHECKOUT", ""))
    if not checkout.is_dir():
        candidate = TESTS_REPO.parent / "open-webui"
        assert (candidate / "sidecar").is_dir(), (
            "set OPEN_WEBUI_CHECKOUT to the open-webui worktree whose sidecar dist the lane boots"
        )
        checkout = candidate
    return checkout


@pytest.fixture(scope="module")
def sidecar() -> Iterator[dict]:
    boot = TESTS_REPO / "agent_sidecar" / "boot_sidecar.sh"
    assert boot.is_file(), f"boot recipe missing at {boot}"
    Path(LANE_DIR).mkdir(parents=True, exist_ok=True)
    # Provider requests accumulate across runs; a fresh lane reads a fresh log.
    FAKE_REQUEST_LOG.write_text("")
    env = {
        **os.environ,
        "AGENT_SIDECAR_LANE_DIR": LANE_DIR,
        "OPENAI_FAKE_LOG": str(FAKE_REQUEST_LOG),
    }
    service = {"boot": boot, "checkout": _checkout(), "env": env}
    subprocess.run([str(boot), "start", str(service["checkout"]), str(SIDECAR_PORT)], check=True, env=env)
    service["port"] = SIDECAR_PORT
    yield service
    subprocess.run([str(boot), "stop"], check=False, env=env)


@pytest.fixture(scope="module")
def subagents_instance(instance_with, sidecar) -> LaunchedInstance:
    # PR6 resolves every turn's provider from OWUI's own connection, so the
    # connection must name the same scripted fake the sidecar's models.json
    # points at (the boot recipe derives its port from the sidecar port).
    fake_port = 18930 + (sidecar["port"] - 8930)
    return instance_with(
        {
            "AGENT_SIDECAR_PORT": str(sidecar["port"]),
            "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
            "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{fake_port}/v1",
            "OPENAI_API_KEYS": "sk-lane",
        }
    )


@pytest.fixture(scope="module")
def sub_admin(subagents_instance: LaunchedInstance):
    return admin_of(subagents_instance)


@pytest.fixture
def agent_model(sub_admin) -> str:
    with sub_admin.client() as client:
        created = client.post(
            "/api/v1/models/create",
            json={
                "id": AGENT_MODEL_ID,
                "name": "Agent Subagents Test",
                "base_model_id": "fake-model",
                "meta": {"capabilities": {"file_context": False}},
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
            json={"id": AGENT_MODEL_ID, "name": "Agent Subagents Test", "access_grants": [grant]},
        )
        assert shared.status_code == 200, shared.text
        listed = client.get("/api/models", params={"refresh": "true"})
        listed.raise_for_status()
        body = listed.json()
        models = body.get("data") if isinstance(body, dict) else body
        assert any(model["id"] == AGENT_MODEL_ID for model in models), [
            m.get("id") for m in models if isinstance(m, dict)
        ]
        return AGENT_MODEL_ID


@pytest.fixture
def subagents_on(sub_admin):
    """`subagents_on(SUBAGENTS_MAX_OUTPUT=64)` switches sub-agents on with those limits.

    The POST carries the whole form, so a test's overrides stand on the
    untouched defaults: both directions of the knob (enable, background,
    sizes) are set when sub-agents turn on.
    """

    def switch_on(**limits) -> None:
        with sub_admin.client() as client:
            settings = {
                "ENABLE_SUBAGENTS": True,
                "SUBAGENTS_BACKGROUND_ENABLED": True,
                "SUBAGENTS_MAX_CONCURRENT": 20,
                "SUBAGENTS_MAX_ASYNC": 20,
                "SUBAGENTS_MAX_ITERATIONS": 256,
                "SUBAGENTS_MAX_OUTPUT": 30000,
                "SUBAGENTS_SYSTEM_PROMPT": "",
                **limits,
            }
            client.post(SUBAGENTS_ROUTES[1], json=settings).raise_for_status()

    return switch_on


# ---------------------------------------------------------------------------
# Helpers: the stored chats, the provider requests, the child session file.
# ---------------------------------------------------------------------------


def message_text(message: dict) -> str:
    pieces = []
    text = message.get("content")
    if isinstance(text, str):
        pieces.append(text)
    elif isinstance(text, list):
        pieces.extend(str(part.get("text", "")) for part in text if isinstance(part, dict))
    for item in message.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict):
                    pieces.append(str(part.get("text", "")))
        if item.get("type") == "function_call_output":
            for part in item.get("output") or []:
                if isinstance(part, dict):
                    pieces.append(str(part.get("text", "")))
    return "".join(pieces)


def sidecar_health(service: dict) -> dict:
    response = httpx.get(f"http://127.0.0.1:{service['port']}/health", timeout=5)
    response.raise_for_status()
    return response.json()


def fake_requests(task: str) -> list[dict]:
    """The provider requests whose last user message starts with the child's task.

    The child's prompt is the task, then the `## Context` block when the
    delegation carried one. The parent's own request for the same run embeds
    the task inside the `TOOL_CHAIN` argument JSON (`"task": "..."`), so a
    prefix match lands on the child legs only.
    """
    if not FAKE_REQUEST_LOG.exists():
        return []
    matches = []
    with FAKE_REQUEST_LOG.open() as log:
        for line in log:
            entry = json.loads(line)
            if (entry.get("last_user") or "").startswith(task):
                matches.append(entry)
    return matches


def delegation_script(task: str, context: str, **extra) -> str:
    arguments = json.dumps({"task": task, "context": context, **extra})
    return f'TOOL_CHAIN: tool_search :: {{"query": "delegate a task"}} -> mcp__owui__delegate_task :: {arguments}'


def run_delegation(client, task: str, context: str, model: str, **send_options):
    script = delegation_script(task, context, **send_options.pop("delegation_hidden_args", {}))
    turn = send_message(client, script, model=model, **send_options)
    return turn, wait_for_reply(client, turn)


def child_chat_record(client, service: dict, task: str, since: float) -> tuple[str, dict]:
    """The delegation record the run created, named by its own sidecar session.

    Delegation records stay out of the chat list (internal chats), so the scan
    runs over the sessions root instead: the child's JSONL carries the bare
task, and the chat behind that id must say it is a subagent record.
    """
    sessions = Path(service["env"]["AGENT_SIDECAR_LANE_DIR"]) / "agent_sessions"
    # The session JSONL stores texts JSON-escaped, so the probe is a
    # quote-free prefix; the subagent record check does the real naming.
    probe = task.split("::")[0].strip() or task[:40]
    for session_dir in sorted(sessions.iterdir()):
        if not session_dir.is_dir() or session_dir.stat().st_mtime < since:
            continue
        texts = [f for f in session_dir.glob("*.jsonl") if f.stat().st_mtime >= since]
        if not any(probe in f.read_text() for f in texts):
            continue
        fetched = client.get(f"/api/v1/chats/{session_dir.name}")
        if fetched.status_code != 200:
            continue
        record = fetched.json()
        meta = record.get("meta") or {}
        if meta.get("type") == "subagent" and meta.get("internal") is True:
            return session_dir.name, record
    raise AssertionError(f"no sub-agent record carries the task {probe!r}")


def chat_messages(record: dict) -> dict:
    return ((record.get("chat") or {}).get("history") or {}).get("messages") or {}


def child_session_file(service: dict, child_id: str) -> Path:
    """The child's own pi session JSONL under the sidecar's sessions root."""
    sessions = Path(service["env"]["AGENT_SIDECAR_LANE_DIR"]) / "agent_sessions" / child_id
    assert sessions.is_dir(), f"no session dir for the child at {sessions}"
    files = sorted(sessions.glob("*.jsonl"))
    assert files, f"no session jsonl for the child at {sessions}"
    return files[0]




def internal_subagent_messages(messages: dict) -> list[dict]:
    return [m for m in messages.values() if (m.get("meta") or {}).get("internal") is True and (m.get("meta") or {}).get("type") == "subagent"]


def message_content_or_error(message: dict) -> str:
    """The message's text including the error body it reports on itself."""
    text = message_text(message)
    error = message.get("error")
    if isinstance(error, dict) and isinstance(error.get("content"), str):
        return f"{text}{error['content']}"
    return text


# ---------------------------------------------------------------------------
# Spawn shape: the child session, its record, its head, and its parent's answer.
# ---------------------------------------------------------------------------


def test_a_foreground_delegation_spawns_the_child_session_and_the_parent_carries_its_summary(
    sub_admin, agent_model, subagents_on, sidecar
):
    task = f"plain child work {uuid.uuid4().hex} - answer me"
    context = "parent-stage hint"
    before = sidecar_health(sidecar)["loaded"]
    since = time.time()
    with sub_admin.client() as client:
        subagents_on()

        turn, stored = run_delegation(client, task, context, agent_model)

        health = sidecar_health(sidecar)
        assert health["loaded"] > before, f"no child session was hosted (health {before} -> {health})"
        assert health["running"] == 0, "the child session stayed running after the turn settled"

        # The child ran its own loop on the same scripted fake, and its head
        # carried a system block with the default sub-agent head.
        child = fake_requests(task)
        assert child, f"the child never reached the provider (task {task!r})"
        assert "system" in child[-1]["roles"], child[-1]
        child_id, record = child_chat_record(client, sidecar, task, since)
        child_meta = record.get("meta") or {}
        assert child_meta.get("internal") is True and child_meta.get("type") == "subagent", child_meta
        assert child_meta.get("parent_chat_id") == turn.chat_id and child_meta.get("mode") == "foreground", child_meta
        head = child_session_file(sidecar, child_id).read_text()
        assert DEFAULT_HEAD_PROBE in head, f"the default sub-agent head never rode the spawn: {head[:400]}"

        # The child's own record holds the child's text; the parent's reply
        # carried the child's answer through the tool result.
        child_history = chat_messages(record)
        child_finals = [m for m in child_history.values() if m.get("role") == "assistant" and m.get("done")]
        assert any("echoed:" in message_text(m) and "plain child work" in message_text(m) for m in child_finals), child_finals
        assert "echoed:" in message_text(stored) and "plain child work" in message_text(stored), stored


def test_the_admin_subagent_prompt_rides_the_spawn_head(sub_admin, agent_model, subagents_on, sidecar):
    task = f"prompt probes {uuid.uuid4().hex} - answer me"
    since = time.time()
    with sub_admin.client() as client:
        subagents_on(SUBAGENTS_SYSTEM_PROMPT=ADMIN_HEAD)
        _, stored = run_delegation(client, task, "parent-stage hint", agent_model)

        child_id, record = child_chat_record(client, sidecar, task, since)
        head = child_session_file(sidecar, child_id).read_text()
        assert ADMIN_HEAD in head, f"the admin head never rode the spawn: {head[:400]}"
        assert DEFAULT_HEAD_PROBE not in head, "the default head ran when the admin head was set"
        assert "echoed:" in message_text(stored), stored


# ---------------------------------------------------------------------------
# The stripped tool set: the bridge drops delegation and memory mutation.
# ---------------------------------------------------------------------------


def test_a_child_tool_set_drops_delegation_and_memory_writes(sub_admin, agent_model, subagents_on, sidecar):
    task = f"TOOL: mcp__owui__add_memory :: {{\"content\": \"quiet moorings {uuid.uuid4().hex}\"}}"
    context = "parent-stage hint"
    since = time.time()
    with sub_admin.client() as client:
        subagents_on()
        _, stored = run_delegation(client, task, context, agent_model, features={"memory": True})

        child = fake_requests(task)
        assert child, f"the child never reached the provider (task {task!r})"
        declared = child[-1]["tools"]
        # Delegation and the mutating memory family never reach the child's
        # request; the non-mutating read survives, so the strip is the change
        # and not an empty manifest.
        forbidden = ("delegate_task", "timer", "add_memory", "update_memory", "replace_memory_content", "delete_memory")
        assert not [name for name in declared if any(banned in name for banned in forbidden)], declared
        assert any("search_memories" in name for name in declared), declared
        assert any("tool_search" in name for name in declared), declared

        # The child's turn said the tool is absent, the parent's tool result
        # carried that, and no memory row landed anywhere.
        child_id, record = child_chat_record(client, sidecar, task, since)
        child_finals = [m for m in chat_messages(record).values() if m.get("role") == "assistant" and m.get("done")]
        assert any("was not offered" in message_content_or_error(m) and "add_memory" in message_content_or_error(m) for m in child_finals), child_finals
        assert "was not offered" in message_text(stored), stored
        memories = client.get("/api/v1/memories/")
        memories.raise_for_status()
        contents = [row["content"] for row in memories.json()]
        assert not any("quiet moorings" in content for content in contents), contents


def test_a_child_never_delegates_recursively(sub_admin, agent_model, subagents_on, sidecar):
    """A child chatting `delegate_task` fails closed: the tool is not on the
    child surface, so the attempt answers as absent and no second delegation
    record ever exists for the inner task."""
    inner_task = f"recursive child work {uuid.uuid4().hex} should never start"
    task = f"TOOL: mcp__owui__delegate_task :: {{\"task\": \"{inner_task}\", \"context\": \"none\"}}"
    sessions = Path(sidecar["env"]["AGENT_SIDECAR_LANE_DIR"]) / "agent_sessions"
    since = time.time()
    with sub_admin.client() as client:
        subagents_on()
        turn, stored = run_delegation(client, task, "parent-stage hint", agent_model)

        # The attempt answers as an absent tool, not as a fresh delegation.
        assert "delegate_task" in message_text(stored) and "was not offered" in message_text(stored), stored

        # Exactly two session dirs carry the inner task: the parent's turn
        # (its TOOL_CHAIN embeds it) and the attempt's own child. A fresh
        # delegation under the child would show as a third carrier, an
        # undelegated grandchild.
        inner_probe = inner_task.split("::")[0].strip()
        carrier = [
            d.name
            for d in sessions.iterdir()
            if d.is_dir() and d.stat().st_mtime >= since and any(inner_probe in f.read_text() for f in d.glob("*.jsonl"))
        ]
        assert len(carrier) == 2, carrier
        assert turn.chat_id in carrier, carrier
        fetched = client.get(f"/api/v1/chats/{turn.chat_id}")
        fetched.raise_for_status()


# ---------------------------------------------------------------------------
# The turn budget: the admin knob rides the spawn as maxTurns (issue 18).
# ---------------------------------------------------------------------------


def test_the_default_turn_budget_is_256(sub_admin, subagents_on):
    with sub_admin.client() as client:
        subagents_on()
        current = client.get(SUBAGENTS_ROUTES[0]).json()
        assert current.get("SUBAGENTS_MAX_ITERATIONS") == 256, current
        client.post(SUBAGENTS_ROUTES[1], json={**current, "SUBAGENTS_MAX_ITERATIONS": 5}).raise_for_status()
        after = client.get(SUBAGENTS_ROUTES[0]).json()
        assert after.get("SUBAGENTS_MAX_ITERATIONS") == 5, after


def test_a_capped_child_reports_the_turn_budget_string(sub_admin, agent_model, subagents_on, sidecar):
    task = "TOOL: mcp__owui__search_memories :: {\"query\": \"harbour\"}"
    context = "parent-stage hint"
    since = time.time()
    with sub_admin.client() as client:
        subagents_on(SUBAGENTS_MAX_ITERATIONS=1)
        _, stored = run_delegation(client, task, context, agent_model, features={"memory": True})

        # One tool-call round is more than the cap: the run stops with the
        # same budget string the native loop reports...
        assert "Tool-call limit reached (1 iterations)." in message_text(stored), stored
        # ...the child's own record carries it as the turn's error...
        child_id, record = child_chat_record(client, sidecar, task, since)
        child_finals = [m for m in chat_messages(record).values() if m.get("role") == "assistant" and m.get("done")]
        assert any("Tool-call limit reached (1 iterations)." in message_content_or_error(m) for m in child_finals), child_finals
        # ...and the child's manifest still offered the tool it tried to use.
        child = fake_requests(task)
        assert child and any("search_memories" in name for name in child[-1]["tools"]), child[-1]


# ---------------------------------------------------------------------------
# Background: dispatch, the pending flow, and the async knob.
# ---------------------------------------------------------------------------


def test_a_background_delegation_dispatches_and_returns_through_the_pending_flow(
    sub_admin, agent_model, subagents_on
):
    task = f"background child work {uuid.uuid4().hex} - answer me"
    context = "parent-stage hint"
    with sub_admin.client() as client:
        subagents_on()
        turn, stored = run_delegation(client, task, context, agent_model, delegation_hidden_args={"background": True})

        assert '"dispatched"' in message_text(stored) or "dispatched" in message_text(stored), stored

        # The child ran to completion on its own; the pending message landed
        # on the parent through run_background, and the fresh turn that
        # answers it rides the unchanged process_pending_internal_messages
        # flow.
        deadline = time.monotonic() + 60
        pending = None
        fresh = None
        while time.monotonic() < deadline:
            fetched = client.get(f"/api/v1/chats/{turn.chat_id}")
            fetched.raise_for_status()
            messages = chat_messages(fetched.json())
            pending = next(
                (
                    m
                    for m in internal_subagent_messages(messages)
                    if "[ASYNC SUBAGENT COMPLETE" in (m.get("content") or "") and task[:40] in (m.get("content") or "") and "Status: completed" in (m.get("content") or "")
                ),
                None,
            )
            if pending is not None:
                fresh = next(
                    (
                        m
                        for m in messages.values()
                        if m.get("role") == "assistant" and m.get("done") and m.get("id") != stored.get("id") and "echoed:" in message_text(m) and "background child work" in message_text(m)
                    ),
                    None,
                )
                if fresh is not None:
                    break
            time.sleep(0.5)
        assert pending is not None, "the pending background report never landed on the parent"
        meta = pending.get("meta") or {}
        child_id = meta.get("subagent_chat_id")
        delegation = meta.get("delegation_id")
        assert child_id and delegation, meta

        fetched = client.get(f"/api/v1/chats/{child_id}")
        fetched.raise_for_status()
        child_record = fetched.json()
        assert (child_record.get("meta") or {}).get("delegation_id") == delegation, child_record.get("meta")
        assert (child_record.get("meta") or {}).get("mode") == "background", child_record.get("meta")
        child_finals = [m for m in chat_messages(child_record).values() if m.get("role") == "assistant" and m.get("done")]
        assert any("echoed:" in message_text(m) and "background child work" in message_text(m) for m in child_finals), child_finals
        assert fresh is not None, "the parent's fresh turn after the background result never landed"


def test_a_full_async_lane_names_the_knob(sub_admin, agent_model, subagents_on):
    marker_task = f"SLOW background sleeper {uuid.uuid4().hex}"
    second_task = f"second background child {uuid.uuid4().hex}"
    with sub_admin.client() as client:
        subagents_on(SUBAGENTS_MAX_ASYNC=1)
        first_turn, first = run_delegation(client, marker_task, "parent-stage hint", agent_model, delegation_hidden_args={"background": True})
        assert '"dispatched"' in message_text(first) or "dispatched" in message_text(first), first

        # The one async lane is busy: the next delegation reports the full
        # queue with the exact boundary string that names the knob.
        _, second = run_delegation(client, second_task, "parent-stage hint", agent_model, delegation_hidden_args={"background": True})
        assert "Async subagent capacity reached (1 running)." in message_text(second), second
        assert "increase subagents.max_async" in message_text(second), second

        # The sleeper's own run finishes and reports through the pending flow
        # like any other child.
        deadline = time.monotonic() + 60
        pending = None
        while time.monotonic() < deadline:
            fetched = client.get(f"/api/v1/chats/{first_turn.chat_id}")
            fetched.raise_for_status()
            candidates = internal_subagent_messages(chat_messages(fetched.json()))
            pending = next(
                (m for m in candidates if "[ASYNC SUBAGENT COMPLETE" in (m.get("content") or "")),
                None,
            )
            if pending is not None:
                break
            time.sleep(0.5)
        assert pending is not None, "the sleeper's report never landed"
        assert "Status: completed" in (pending.get("content") or ""), pending


# ---------------------------------------------------------------------------
# The crash path: a sidecar death kills its children and still reports.
# ---------------------------------------------------------------------------


def test_a_killed_sidecar_takes_its_children_down_and_reports(sub_admin, agent_model, subagents_on, sidecar):
    task = f"SLOW crash child {uuid.uuid4().hex}"
    with sub_admin.client() as client:
        subagents_on()
        turn, stored = run_delegation(client, task, "parent-stage hint", agent_model, delegation_hidden_args={"background": True})
        assert '"dispatched"' in message_text(stored) or "dispatched" in message_text(stored), stored

        # The child is mid-run when the sidecar dies: its turn errors onto the
        # child record, the pending report names the failure, and the restarted
        # sidecar comes back hosting nothing.
        time.sleep(0.8)
        subprocess.run([str(sidecar["boot"]), "stop"], check=False, env=sidecar["env"])

        deadline = time.monotonic() + 60
        report = None
        while time.monotonic() < deadline:
            try:
                fetched = client.get(f"/api/v1/chats/{turn.chat_id}")
                fetched.raise_for_status()
            except Exception:
                time.sleep(0.3)
                continue
            candidates = internal_subagent_messages(chat_messages(fetched.json()))
            report = next((m for m in candidates if "did not complete successfully" in (m.get("content") or "")), None)
            if report is not None:
                break
            time.sleep(0.5)
        fetched = client.get(f"/api/v1/chats/{turn.chat_id}")
        fetched.raise_for_status()
        messaged = chat_messages(fetched.json())
        assert report is not None, (
            f"the crash never surfaced a failed background report on the parent; messages: {json.dumps(messaged)[:1200]}"
        )

    subprocess.run([str(sidecar["boot"]), "start", str(sidecar["checkout"]), str(SIDECAR_PORT)], check=True, env=sidecar["env"])
    health = sidecar_health(sidecar)
    assert health.get("ok") is True and health.get("running") in (0, None), health
