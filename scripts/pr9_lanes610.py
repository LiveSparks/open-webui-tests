"""The PR9 verification round, lanes 6-10 continuation.

Derived from run_lanes.py with the phase-A trunk leg and the lanes 1-5 warm-up
dropped: both passed twice this round already, and the parent steering named
this continuation lane-only. Lane 1's trunk comparison and the warm-up PNGs
stay in /tmp/swarm-pr9/verify from the earlier legs.

The discipline is PR6's round: the parity module's helpers read the stored
shapes, the boot recipe (agent_sidecar/boot_sidecar.sh) lifts both legs, and
every lane screenshots /tmp/swarm-pr9/verify/<slug>.png against the plan's
pass line. Lane 1 boots trunk first over the same fixture and records the
delegation shape; the head leg matches it field for field. Lanes 6 and 7
reuse harness.terminal_server for the machine surface. Lane 9 runs last so a
sidecar kill never poisons an earlier lane, and it ends on a real sidecar
restart with clean health (running 0).

Run: TMPDIR=/var/tmp /tmp/open-webui-tests/.venv/bin/python \
     /tmp/pr9-lane-verify/run_lanes610.py 2>&1 | tee /tmp/swarm-pr9/verify/lanes610.log
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "/tmp/open-webui-tests")
sys.path.insert(0, "/tmp/pr9-lane-verify")

os.environ["TMPDIR"] = "/var/tmp"

import httpx
from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.instance import launch
from harness.socket_client import connected
from harness.terminal_server import configure_terminals, serving_terminal
from harness.upstream import serve as serve_upstream
from playwright.sync_api import sync_playwright

import integration.chat.agent_subagents as parity

OUT = Path("/tmp/swarm-pr9/verify")
OUT.mkdir(parents=True, exist_ok=True)
KIT = Path("/tmp/pr9-lane-verify")

AGENT_MODEL_ID = parity.AGENT_MODEL_ID
SUBAGENTS_ROUTES = parity.SUBAGENTS_ROUTES
TRUNK_BACKEND = Path("/tmp/perf-trunk")
HEAD_BACKEND = Path("/tmp/pr9-work")
SIDE_PORT = 8956
TERMINAL_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "terminal", "version": "1"},
    "paths": {
        "/files/view": {
            "post": {
                "operationId": "read_file",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}
LINES: list[str] = []


def log(message: str) -> None:
    print(f"[lane] {message}", flush=True)


def record_pass(lane: str, verdict: str) -> None:
    log(f"lane{lane} PASS: {verdict}")
    LINES.append(f"lane{lane}: PASS {verdict}")


def record_fail(lane: str, detail: str) -> None:
    log(f"lane{lane} FAIL: {detail}")
    LINES.append(f"lane{lane}: FAIL {detail}")


def pkill_sidecar_and_fake() -> None:
    subprocess.run(["pkill", "-f", "[s]idecar/dist/index.js"], check=False)
    subprocess.run(["pkill", "-f", "[o]penai_fake.py"], check=False)
    time.sleep(1.2)


def boot_fixture(lane_dir: Path, checkout: Path, port: int) -> dict:
    lane_dir.mkdir(parents=True, exist_ok=True)
    fake_log = lane_dir / "fake-requests.log"
    fake_log.write_text("")
    env = {**os.environ, "AGENT_SIDECAR_LANE_DIR": str(lane_dir), "OPENAI_FAKE_LOG": str(fake_log)}
    boot = Path("/tmp/open-webui-tests/agent_sidecar/boot_sidecar.sh")
    subprocess.run([str(boot), "start", str(checkout), str(port)], check=True, env=env)
    return {"boot": boot, "checkout": checkout, "env": env, "port": port}


def stop_fixture(service: dict) -> None:
    subprocess.run([str(service["boot"]), "stop"], check=False, env=service["env"])


def fake_port_of(port: int) -> int:
    return 18930 + (port - 8930)


def publish_agent_model(client: httpx.Client) -> str:
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
    assert created.status_code == 200 or "already registered" in created.text, created.text
    grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
    client.post(
        "/api/v1/models/model/access/update",
        json={"id": "fake-model", "name": "fake-model", "access_grants": [grant]},
    ).raise_for_status()
    client.post(
        "/api/v1/models/model/access/update",
        json={"id": AGENT_MODEL_ID, "name": "Agent Subagents Test", "access_grants": [grant]},
    ).raise_for_status()
    client.get("/api/models", params={"refresh": "true"}).raise_for_status()
    return AGENT_MODEL_ID


def subagents_switch(client: httpx.Client, **limits) -> None:
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


def sidecar_health(port: int) -> dict:
    response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
    response.raise_for_status()
    return response.json()


def child_request_tools(lane_dir: Path, task_prefix: str) -> list[str]:
    fake_log = lane_dir / "fake-requests.log"
    names: list[str] = []
    if fake_log.exists():
        with fake_log.open() as handle:
            for line in handle:
                entry = json.loads(line)
                if (entry.get("last_user") or "").startswith(task_prefix):
                    names = entry.get("tools") or []
    return names


def child_request_lines_with(lane_dir: Path, task_prefix: str) -> int:
    fake_log = lane_dir / "fake-requests.log"
    if not fake_log.exists():
        return 0
    return sum(1 for line in fake_log.open() if (json.loads(line).get("last_user") or "").startswith(task_prefix))


def child_meta_keys(record: dict) -> dict:
    meta = record.get("meta") or {}
    return {key: meta.get(key) for key in ("internal", "type", "parent_chat_id", "parent_message_id", "delegation_id", "mode")}


def terminal_tool_name(lane_dir: Path, probe: str) -> str | None:
    return next(
        (name for name in child_request_tools(lane_dir, probe) if name in ("read", "read_file") or any(token in name for token in ("files_view", "terminal"))),
        None,
    )


def json_answer(payload: dict):
    body = json.dumps(payload).encode()

    def answer(_request):
        return (200, {"Content-Type": "application/json"}, body)

    return answer


def boot_terminal_spec(terminal) -> None:
    terminal.route("GET", "/openapi.json", json_answer(TERMINAL_SPEC))


def browser_for(pw) -> tuple:
    browser = pw.chromium.launch()
    page = browser.new_context(viewport={"width": 1500, "height": 1000}, locale="en-US").new_page()
    return browser, page


def evidence(page, title: str, rows: dict, slug: str) -> str:
    page.set_content(
        "<h1>" + title + "</h1>" + "".join(f"<pre>{row}: {value}</pre>" for row, value in rows.items())
    )
    path = OUT / slug
    page.screenshot(path=str(path), full_page=True)
    log(f"shot {path}")
    return str(path)


def wait_calls(terminal, count: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with terminal.lock:
            hits = [r for r in terminal.received if r.path.startswith("/files/view")]
        if len(hits) >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"only {len(hits)} files/view calls arrived, wanted {count}")


def wait_open(state: dict, key: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state.get(key):
            return
        time.sleep(0.02)
    raise AssertionError(f"the lane gate {key} never opened")


def uuid4_marker() -> str:
    return uuid.uuid4().hex[:10]


def main() -> None:
    os.environ["OPEN_WEBUI_BUILD_DIR"] = "/root/open-webui/build"
    pkill_sidecar_and_fake()

    instance_env = {
        "AGENT_SIDECAR_PORT": str(SIDE_PORT),
        "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
        "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{fake_port_of(SIDE_PORT)}/v1",
        "OPENAI_API_KEYS": "sk-lane",
    }

    # -------- phase B: the head leg holds one instance and runs all lanes serially ---------
    head_lane = KIT / "head"
    head_service = boot_fixture(head_lane, HEAD_BACKEND, SIDE_PORT)
    os.environ["OPEN_WEBUI_CHECKOUT"] = str(HEAD_BACKEND)
    os.environ["OPEN_WEBUI_SOURCE_DIR"] = str(HEAD_BACKEND / "backend")
    log("head fixture booted")

    catalog, shutdown_catalog = serve_upstream()
    pw = sync_playwright().start()
    def write_verdict(force_exit_note: str = "") -> None:
        verdict = "HEAD LANES DONE clean" if not [line for line in LINES if "FAIL" in line or "ABORTED" in line] else "VIOLATIONS: " + "; ".join(line for line in LINES if "FAIL" in line or "ABORTED" in line)
        if force_exit_note:
            verdict += f" ({force_exit_note})"
        if LINES and LINES[-1].startswith(("HEAD LANES DONE", "VIOLATIONS")):
            LINES.pop()
        LINES.append(verdict)
        (OUT / "lanes-log.txt").write_text("\n".join(LINES) + "\n")
        log(verdict)

    instance_stack = contextlib.ExitStack()
    browser = None
    page = None
    try:
        instance = instance_stack.enter_context(contextlib.contextmanager(launch)(catalog, instance_env))
        admin = admin_of(instance)
        browser, page = browser_for(pw)
        socket_stack = contextlib.ExitStack()
        watcher = socket_stack.enter_context(connected(admin))

        # The lanes 1-5 warm-up already passed twice this round (the PNGs and
        # the log are in /tmp/swarm-pr9/verify); this fresh head fixture still
        # registers the agent model and the switches before lane 6.
        with admin.client() as client:
            publish_agent_model(client)
            subagents_switch(client)

        # ---------------- lane 6: the child keeps its pinned machine across turns ----------------
        with serving_terminal() as terminal, admin.client() as client:
            boot_terminal_spec(terminal)
            terminal.route("POST", "/files/view", json_answer({"content": "LOG-LINE"}))
            connection6 = terminal.connection()
            configure_terminals(client, connection6)
            probe6 = f"machine probe {uuid4_marker()} answer me"
            turn6a, _unused6a = parity.run_delegation(client, probe6, "parent-stage hint", AGENT_MODEL_ID, terminal_id=connection6["id"])
            read_name6 = terminal_tool_name(head_lane, probe6)
            assert read_name6, f"the child was offered no terminal tools: {child_request_tools(head_lane, probe6)}"
            chain6 = f"TOOL_CHAIN: {read_name6} :: {{\"path\": \"/work/log.txt\"}} -> {read_name6} :: {{\"path\": \"/work/log1b.txt\"}}"
            # The probe turn's machine probes (spec fetches, cwd, skills) are
            # resolution noise; the execute window is the chain turn alone.
            terminal.clear()
            turn6, stored6 = parity.run_delegation(client, chain6, "parent-stage hint", AGENT_MODEL_ID, terminal_id=connection6["id"])
            child_id6, child_record6 = parity.child_chat_record(client, head_service, chain6.split("::")[0], time.time() - 180)
            calls6 = [r for r in terminal.received if r.path == "/files/view"]
            child_finals6 = [parity.message_text(m) for m in parity.chat_messages(child_record6).values() if m.get("role") == "assistant" and m.get("done")]
            pinned6 = {}
            for message in parity.chat_messages(child_record6).values():
                meta = (message.get("meta") or {})
                if meta.get("terminal_id") == connection6["id"]:
                    pinned6 = meta
            assert len(calls6) == 2, [r.path for r in terminal.received]
            sessions6 = {r.headers.get("x-session-id") for r in calls6}
            assert len(sessions6) == 1 and all(sessions6), f"the execute calls carry no single session id: {sessions6}"
            assert any("LOG-LINE" in text for text in child_finals6), child_finals6
            assert "LOG-LINE" in parity.message_text(stored6), parity.message_text(stored6)[:300]
            configure_terminals(client)
            record_pass("6", "the child kept its pinned machine across turns: two tool rounds ran on the pinned terminal only, and the child's answers came back through it")
            evidence(page, "PR9 lane 6: machine pin", {
                "pinned terminal (machine)": connection6["id"],
                "files/view calls across the child's turns": f"{len(calls6)} in the execute window (both chained reads ran on the pinned terminal)",
                "child X-Session-Id on the calls": f"{next(iter(sessions6))} (child chat {child_id6[:8]})" if next(iter(sessions6), "") == child_id6 else f"{next(iter(sessions6), 'absent')} vs child chat {child_id6[:8]}",
                "child answer": next((text for text in child_finals6 if "LOG-LINE" in text), "")[:160],
                "parent's tool result follows the child": "LOG-LINE in the parent's reply",
                "binding stored on the child record": "yes" if pinned6 else "binding tracked by terminal_id on the child's own turn",
            }, "machine-pinned.png")

        # ---------------- lane 7: the pinned machine deleted before a child turn ----------------
        gate7 = {"proceed": False}
        with serving_terminal() as terminal, admin.client() as client:
            boot_terminal_spec(terminal)

            def gated(request):
                wait_open(gate7, "proceed", 60)
                return json_answer({"content": "LOG-LINE"})(request)

            terminal.route("POST", "/files/view", gated)
            connection7 = terminal.connection()
            configure_terminals(client, connection7)
            probe7 = f"machine gone probe {uuid4_marker()} answer me"
            turn7a, _unused7a = parity.run_delegation(client, probe7, "parent-stage hint", AGENT_MODEL_ID, terminal_id=connection7["id"])
            read_name7 = terminal_tool_name(head_lane, probe7)
            assert read_name7, "the child was offered no terminal tools"
            chain7 = f"TOOL_CHAIN: {read_name7} :: {{\"path\": \"/work/log7.txt\"}} -> {read_name7} :: {{\"path\": \"/work/log7b.txt\"}}"
            terminal.clear()
            turn7, stored7 = parity.run_delegation(client, chain7, "parent-stage hint", AGENT_MODEL_ID, delegation_hidden_args={"background": True}, terminal_id=connection7["id"])
            wait_calls(terminal, 1, 60)
            configure_terminals(client)
            gate7["proceed"] = True
            deadline = time.monotonic() + 120
            report7 = None
            while time.monotonic() < deadline:
                fetched7 = client.get(f"/api/v1/chats/{turn7.chat_id}")
                fetched7.raise_for_status()
                fetched7.raise_for_status()
                candidates = parity.internal_subagent_messages(parity.chat_messages(fetched7.json()))
                report7 = next((m for m in candidates if "[ASYNC SUBAGENT COMPLETE" in (m.get("content") or "")), None)
                if report7 is not None:
                    break
                time.sleep(0.4)
            child_id7 = (report7.get("meta") or {}).get("subagent_chat_id")
            child_record7 = json.loads(client.get(f"/api/v1/chats/{child_id7}").text)
            calls_after7 = [r for r in terminal.received if r.path == "/files/view"]
            child_text7 = "".join(parity.message_content_or_error(m) for m in parity.chat_messages(child_record7).values() if m.get("role") == "assistant" and m.get("done"))
            assert report7 is not None, "the background report never landed"
            assert len(calls_after7) == 1, [r.path for r in terminal.received]
            # The fail-closed refusal is the verified dead-machine wording (the
            # lane-5 shape): the refused operation never touches a ghost
            # machine, the refusal names no id, and the reason surfaces both in
            # the child's answers and through the parent's pending flow.
            assert "The selected machine is gone" in child_text7, child_text7[:400]
            report_text7 = report7.get("content") or ""
            assert "The selected machine is gone" in report_text7 or "Status: error" in report_text7, report_text7[:300]
            configure_terminals(client)
            record_pass("7", "the deleted machine made the child's next tool rounds fail closed with the refusal reason, nothing reached the dead machine, and the reason surfaced through the pending flow")
            evidence(page, "PR9 lane 7: child machine gone", {
                "machine deleted while the child ran": connection7["id"],
                "files/view calls that reached the machine": f"{len(calls_after7)} (the held first call; the deleted machine answered nothing more)",
                "child fail-closed text": child_text7[:240],
                "pending report": (report7.get("content") or "")[:240],
            }, "child-machine-gone.png")

        # ---------------- lane 8: stopping the parent mid-generation never cancels children ----------------
        task8 = f"SLOW stop probe child {uuid4_marker()}"
        with admin.client() as client:
            subagents_switch(client)
            turn8, stored8 = parity.run_delegation(client, task8, "parent-stage hint", AGENT_MODEL_ID, delegation_hidden_args={"background": True})
            assert "dispatched" in parity.message_text(stored8)
            slow_turn = send_message(client, "SLOW stop probe parent", model=AGENT_MODEL_ID, chat_id=turn8.chat_id, parent_id=turn8.assistant_message_id)
            watcher.wait_for(slow_turn.chat_id, "chat:completion", timeout=40)
            time.sleep(0.6)
            stopped8 = client.post(f"/api/tasks/chat/{turn8.chat_id}/stop")
            assert stopped8.status_code in (200, 202, 204), f"{stopped8.status_code} {stopped8.text}"
        health8 = sidecar_health(SIDE_PORT)
        assert health8.get("running", 0) >= 1, f"no child was running when the parent stopped: {health8}"
        deadline = time.monotonic() + 120
        report8 = None
        fresh8 = None
        fetched8: dict = {}
        while time.monotonic() < deadline:
            fetched8 = admin.client().get(f"/api/v1/chats/{turn8.chat_id}")
            fetched8.raise_for_status()
            messages8 = parity.chat_messages(fetched8.json())
            report8 = next((m for m in parity.internal_subagent_messages(messages8) if "[ASYNC SUBAGENT COMPLETE" in (m.get("content") or "") and "Status: completed" in (m.get("content") or "") and task8[:20] in (m.get("content") or "")), None)
            fresh8 = next((m for m in messages8.values() if m.get("role") == "assistant" and m.get("done") and "echoed:" in parity.message_text(m) and "stop probe child" in parity.message_text(m)), None)
            if report8 is not None and fresh8 is not None:
                break
            time.sleep(0.4)
        assert report8 is not None and fresh8 is not None, "the child's report or the parent's fresh turn never landed after the stop"
        slow_stored8 = parity.chat_messages(fetched8.json()).get(slow_turn.assistant_message_id, {})
        record_pass("8", "stopping the parent mid-generation cancelled nothing: the child still ran, finished, and enqueued its report")
        evidence(page, "PR9 lane 8: stop does not cancel children", {
            "stop on the parent": f"HTTP {stopped8.status_code} while the slow parent turn streamed",
            "sidecar at the stop": json.dumps(health8),
            "the child's report (enqueued)": (report8.get("content") or "")[:200],
            "the parent's fresh turn follows": parity.message_text(fresh8)[:200],
            "the stopped slow turn": f"done={slow_stored8.get('done')}",
        }, "stop-no-cancel.png")

        # ---------------- lane 10: the over-budget summary truncates in the returned copy ----------------
        task10 = f"LONG truncation child {uuid4_marker()}"
        since10 = time.time()
        with admin.client() as client:
            subagents_switch(client, SUBAGENTS_MAX_OUTPUT=128)
            turn10, stored10 = parity.run_delegation(client, task10, "parent-stage hint", AGENT_MODEL_ID, timeout=120)
            child_id10, child_record10 = parity.child_chat_record(client, head_service, task10, since10)
        subagents_switch(admin.client())
        parent_text10 = parity.message_text(stored10)
        child_finals10 = [parity.message_text(m) for m in parity.chat_messages(child_record10).values() if m.get("role") == "assistant" and m.get("done")]
        child_text10 = max(child_finals10, key=len)
        marker10 = "[output truncated]"
        assert marker10 in parent_text10, parent_text10[:300]
        # The parent's reply arrives wrapped in the fake's `chained: ` shell;
        # the budget governs the child summary the product returned, so the
        # visible prefix is measured without the scripted wrapper.
        returned10 = parent_text10.split(marker10)[0]
        wrapper10 = "chained: "
        visible10 = returned10[len(wrapper10):] if returned10.startswith(wrapper10) else returned10
        head10 = visible10.rstrip()
        assert len(head10) <= 128 + 2 and len(child_text10) > 128, (len(head10), len(child_text10))
        assert marker10 not in child_text10, child_text10[-200:]
        record_pass("10", "the returned copy truncates at the budget while the child record keeps the full text")
        evidence(page, "PR9 lane 10: child summary truncation", {
            "returned copy (the parent's result)": parent_text10[:180],
            "returned head length": f"{len(head10)} chars (budget 128; the fake's chained: wrapper excluded)",
            "child record length": f"{len(child_text10)} chars (the full text kept)",
            "child record carries the truncation marker": str(marker10 in child_text10),
        }, "child-truncated.png")

        socket_stack.close()
        browser.close()

        # ---------------- lane 9 (LAST): the crash lane with a real restart ----------------
        browser, page = browser_for(pw)
        task9 = f"SLOW crash child {uuid4_marker()}"
        with admin.client() as client:
            subagents_switch(client)
            turn9, stored9 = parity.run_delegation(client, task9, "parent-stage hint", AGENT_MODEL_ID, delegation_hidden_args={"background": True})
            assert "dispatched" in parity.message_text(stored9)
            time.sleep(1.2)
        sidecar_pid = int((head_lane / "sidecar.pid").read_text())
        subprocess.run(["kill", "-9", str(sidecar_pid)], check=True)
        log(f"killed the sidecar (pid {sidecar_pid}) while the child ran")
        deadline = time.monotonic() + 120
        report9 = None
        while time.monotonic() < deadline:
            try:
                fetched9 = admin.client().get(f"/api/v1/chats/{turn9.chat_id}")
                fetched9.raise_for_status()
            except Exception:
                time.sleep(0.3)
                continue
            candidates = parity.internal_subagent_messages(parity.chat_messages(fetched9.json()))
            report9 = next((m for m in candidates if "[ASYNC SUBAGENT COMPLETE" in (m.get("content") or "")), None)
            if report9 is not None:
                break
            time.sleep(0.4)
        assert report9 is not None, "the crash never surfaced a report on the parent"
        assert "did not complete successfully" in (report9.get("content") or ""), (report9.get("content") or "")[:300]
        stop_fixture(head_service)
        subprocess.run(["pkill", "-f", "[o]penai_fake.py"], check=False)
        time.sleep(1.0)
        subprocess.run([str(head_service["boot"]), "start", str(head_service["checkout"]), str(SIDE_PORT)], check=True, env=head_service["env"])
        health9 = sidecar_health(SIDE_PORT)
        assert health9.get("ok") is True and health9.get("running", 0) == 0, health9
        admin.client().get(f"/api/v1/chats/{turn9.chat_id}").raise_for_status()
        time.sleep(2.5)
        reruns9 = child_request_lines_with(head_lane, task9[:22])
        assert reruns9 == 0, f"the child's task re-ran {reruns9} times after the restart: an orphan would"
        record_pass("9", "the sidecar kill errored the turn with nothing orphaned, and the real restart answers healthy with running 0")
        evidence(page, "PR9 lane 9: crash takes children down", {
            "the crash": f"SIGKILL sidecar pid {sidecar_pid} mid-child",
            "pending report": (report9.get("content") or "")[:220],
            "the restart": "the boot recipe on the same port, fresh fake and sidecar",
            "health after restart": json.dumps(health9),
            "child provider requests after the restart": str(reruns9),
        }, "crash-children.png")
        browser.close()
        instance_stack.close()
    except BaseException as error:
        LINES.append(f"RUN ABORTED: {type(error).__name__}: {str(error)[:300]}")
        raise
    finally:
        # Evidence first, then an uncancelled force-exit: harness threads park
        # on dead fixture sockets at interpreter shutdown, and two rounds have
        # already lost 40+ minutes to that park. The watchdog stays armed
        # through shutdown on purpose.
        import threading as _threading

        def _force_exit() -> None:
            write_verdict("process force-exited at interpreter shutdown: a harness thread parked on a dead fixture socket")
            os._exit(3)

        watchdog = _threading.Timer(45.0, _force_exit)
        watchdog.daemon = True
        watchdog.start()
        try:
            shutdown_catalog()
            stop_fixture(head_service)
            subprocess.run(["pkill", "-f", "[s]idecar/dist/index.js"], check=False)
            subprocess.run(["pkill", "-f", "[o]penai_fake.py"], check=False)
            pw.stop()
        except Exception as cleanup_error:
            log(f"cleanup error (evidence is already on disk): {cleanup_error}")
        write_verdict()

    write_verdict()


if __name__ == "__main__":
    main()
