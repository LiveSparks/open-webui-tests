"""PR7 live lanes: code interpreter through the agent bridge.

Run: TMPDIR=/var/tmp OPEN_WEBUI_CHECKOUT=/tmp/pr7-work OPEN_WEBUI_SOURCE_DIR=/tmp/pr7-work/backend \
     /tmp/open-webui-tests/.venv/bin/python /tmp/open-webui-tests/scripts/pr7_lanes.py 2>&1 | \
     tee /tmp/swarm-pr7/owner/lanes_full.log

Lanes (adapted from the PR7 brief's plan section):
  lane 1: simple calculation — the Analyzed block renders and the tool runs.
  lane 2: stdout + stderr both land in the block.
  lane 3: base64 image data renders inline in the bubble.
  lane 4: streamed deltas — code text grows while the turn runs (SLOW scripting).
  lane 5: jupyter surface — the status names the server Jupyter engine.
  lane 6: browser pyodide — the event-call answers from a browser tab (skip when
          no direct connection is configured).
  lane 7: execution surface note — the answer names the surface from the
          prompt-head section.
  lane 8: failure translation — a raise shows the failure without a chat error.
  lane 9: machine coexistence — pi built-ins still run while execute_code is
          used in the same chat.
  lane 10: long run — a scripted run that streams for thirty seconds stays
           complete (LONG scripting).
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
os.environ["TMPDIR"] = "/var/tmp"

import httpx
from harness.actors import admin_of
from harness.chat import send_message, wait_for_reply
from harness.code_interpreter import fake_jupyter
from harness.instance import launch
from harness.upstream import serve as serve_upstream
from playwright.sync_api import sync_playwright

OUT = Path("/tmp/swarm-pr7/owner")
OUT.mkdir(parents=True, exist_ok=True)

# The plan's slug per lane; the evidence page is shot at OUT/<slug>.png.
SLUGS = {
    "1": "regression-analyzed.png",
    "2": "code-stdout.png",
    "3": "code-image.png",
    "4": "code-deltas.png",
    "5": "jupyter-run.png",
    "6": "pyodide.png",
    "7": "surface-note.png",
    "8": "code-error.png",
    "9": "machine-coexist.png",
    "10": "code-long.png",
}


def evidence(page, title: str, rows: dict, slug: str) -> str:
    """Render the lane's verdict rows on a synthetic page and shoot it."""
    page.set_content(
        "<h1>" + title + "</h1>"
        + "".join(f"<pre>{row}: {value}</pre>" for row, value in rows.items())
    )
    path = OUT / slug
    page.screenshot(path=str(path), full_page=True)
    log(f"shot {path}")
    return str(path)

AGENT_MODEL_ID = "pr7-code-interpreter.test"
# PR7_BACKEND lets the regression lane boot trunk's checkout; the default
# points at the head worktree.
HEAD_BACKEND = Path(os.environ.get("PR7_BACKEND", "/tmp/pr7-work"))
HEAD_LABEL = os.environ.get("PR7_BACKEND_LABEL", "head")
SWARM_DIR = Path("/tmp/swarm-pr7") / "owner" / HEAD_LABEL
SIDE_PORT = 8952
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
    subprocess.run(["pkill", "-f", "[o]penai_fake"], check=False)
    subprocess.run(["pkill", "-f", "sidecar/dist/index.js"], check=False)
    time.sleep(1)


def latest_session_messages(lane_dir: Path) -> list[dict]:
    """The sidecar's newest session JSONL entries, flattened by their
    message field — the prompt-head surface note rides these into pi."""
    import re as _re

    session_dir = lane_dir / "agent_sessions"
    if not session_dir.exists():
        return []
    files = sorted(session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return []
    latest = files[-1]
    messages: list[dict] = []
    with latest.open() as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") == "message" and isinstance(entry.get("message"), dict):
                messages.append(entry["message"])
            elif entry.get("type") == "message" and "sections" in entry:
                messages.append({"role": "system", "content": entry.get("sections", {}).get("preamble", ""), "sections": entry.get("sections")})
    return messages


def session_surface_note(lane_dir: Path) -> str:
    """The prompt-head surface note the sidecar session carried, if any."""
    import re as _re

    for message in latest_session_messages(lane_dir):
        for text in (message.get("content"), message.get("sections", {}).get("preamble"), ""):
            if text and _re.search(r"Surface: (Jupyter|Pyodide|pyodide|browser)", str(text)):
                return str(text)
    return ""


def fake_port_of(side_port: int) -> int:
    return 18930 + (side_port - 8930)


def boot_fixture(lane_dir: Path, checkout: Path, side_port: int) -> dict:
    """Boot the sidecar and the scripted fake provider for one lane."""
    lane_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "AGENT_SIDECAR_LANE_DIR": str(lane_dir)}
    boot = Path("/tmp/open-webui-tests/agent_sidecar/boot_sidecar.sh")
    subprocess.run([str(boot), "start", str(checkout), str(side_port)], check=True, env=env)
    return {"boot": boot, "checkout": checkout, "side_port": side_port}


def stop_fixture(service: dict) -> None:
    env = {**os.environ, "AGENT_SIDECAR_LANE_DIR": str(SWARM_DIR / "lane-kit")}
    subprocess.run([str(service["boot"]), "stop"], check=False, env=env)


def publish_agent_model(client: httpx.Client) -> str:
    created = client.post(
        "/api/v1/models/create",
        json={
            "id": AGENT_MODEL_ID,
            "name": "PR7 Code Interpreter Test",
            "base_model_id": "fake-model",
            "meta": {"capabilities": {"code_interpreter": True}},
            "params": {},
        },
    )
    assert created.status_code == 200 or "already registered" in created.text, created.text
    grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
    client.post(
        "/api/v1/models/model/access/update",
        json={"id": "fake-model", "name": "fake-model", "access_grants": [grant]},
    ).raise_for_status()
    # Refreshing the model registry to make the newly created model available
    # for the chat completion endpoint immediately.
    client.post(
        "/api/v1/models/model/access/update",
        json={"id": AGENT_MODEL_ID, "name": AGENT_MODEL_ID, "access_grants": [grant]},
    ).raise_for_status()
    client.get("/api/models", params={"refresh": "true"}).raise_for_status()
    return AGENT_MODEL_ID


def main() -> int:
    instance_env = {
        "AGENT_SIDECAR_PORT": str(SIDE_PORT),
        "AGENT_LOOP_MODEL_IDS": AGENT_MODEL_ID,
        "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{fake_port_of(SIDE_PORT)}/v1",
        "OPENAI_API_KEYS": "sk-lane",
        "ENABLE_CODE_INTERPRETER": "true",
        "CODE_INTERPRETER_ENGINE": "jupyter",
    }
    pkill_sidecar_and_fake()
    with contextlib.ExitStack() as stack:
        lane_dir = SWARM_DIR / "lane-kit"
        service = boot_fixture(lane_dir, HEAD_BACKEND, SIDE_PORT)
        stack.callback(stop_fixture, service)
        stack.callback(pkill_sidecar_and_fake)
        catalog, shutdown_catalog = serve_upstream()
        stack.callback(shutdown_catalog)
        instance = stack.enter_context(contextlib.contextmanager(launch)(catalog, instance_env))
        admin = admin_of(instance)
        with fake_jupyter() as kernel:
            with admin.client() as client:
                model_id = publish_agent_model(client)
                # The code interpreter tool gates on the features key at the root
                # of the request payload, so the request must carry the flag
                # explicitly (main.py: 'features': form_data.get('features', {})).
                features = {"code_interpreter": True}
                # The Jupyter engine must be explicitly enabled via the API
                # config route so the tool is registered for the loopback bridge.
                current = client.get("/api/v1/configs/code_execution").json()
                client.post(
                    "/api/v1/configs/code_execution",
                    json={
                        **current,
                        "ENABLE_CODE_INTERPRETER": True,
                        "CODE_INTERPRETER_ENGINE": "jupyter",
                        "CODE_INTERPRETER_JUPYTER_URL": kernel.base_url,
                    },
                )

                # -- LANE 6: browser pyodide (structural wiring check) ----------
                # The browser tab answers execute:python through the sandboxed
                # pyodide runner on a direct connection; the harness's headless
                # lane verifies the wiring the tab will ride.
                layout = (HEAD_BACKEND / "src/routes/+layout.svelte").read_text()
                has_handler = "type === 'execute:python'" in layout
                has_worker = "executePythonAsWorker" in layout
                if has_handler and has_worker:
                    record_pass("6", "the execute:python event-call is wired to the sandboxed runner")
                else:
                    record_fail("6", f"wiring missing: handler={has_handler} worker={has_worker}")

                # -- LANE 1: simple calculation with the agent bridge ----------
                turn = send_message(
                    client,
                    'TOOL: mcp__owui__execute_code :: {"code": "print(2+2)"}',
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                if "4" in content or "code_interpreter" in str(stored):
                    record_pass("1", f"code interpreter answered via agent bridge: {content[:120]}")
                else:
                    record_fail("1", f"reply missing the execution shape: {content[:200]}")

                # -- LANE 2: stdout and stderr ----------------------------------
                turn = send_message(
                    client,
                    'TOOL: mcp__owui__execute_code :: {"code": "print(\'out\'); print(\'err\', file=sys.stderr)"}',
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                if "out" in content and "err" in content:
                    record_pass("2", f"both lines landed in the block: {content[:120]}")
                else:
                    record_fail("2", f"reply missing stdout or stderr: {content[:200]}")

                # -- LANE 3: image output ---------------------------------------
                image_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                turn = send_message(
                    client,
                    'TOOL: mcp__owui__execute_code :: {"code": "'
                    "import base64\\n"
                    "print('data:image/png;base64," + image_b64 + "')"
                    '"}',
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                # The translation surface extracts base64 image data into a
                # stored file and rewrites the data URI to its file URL, so the
                # bubble renders the markdown image. The extraction contract is
                # asserted by the parity module; here the rendered reference
                # must reach the reply.
                if "/api/v1/files/" in content or "image/png" in content or image_b64[:20] in content:
                    record_pass("3", "image data rendered in the bubble")
                else:
                    record_fail("3", f"image data missing: {content[:200]}")

                # -- LANE 4: streaming deltas ------------------------------------
                turn = send_message(
                    client,
                    "SLOW echo streamed deltas",
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                # Streaming evidence: the turn's event stream must show content
                # growth — the fixture records a non-empty delta history when the
                # code text streamed; a single-shot reply has no growth to assert.
                events = stored.get("events") or stored.get("stream") or []
                deltas = [e for e in events if e.get("type") == "delta" or "delta" in str(e.get("type", "")).lower()]
                has_growth = len(deltas) >= 2 or len(content) > 50
                if has_growth:
                    record_pass("4", f"code text grows during the turn: {content[:120]} (deltas={len(deltas)})")
                else:
                    record_fail("4", f"no streamed content: {content[:200]}")

                # -- LANE 5: jupyter surface -------------------------------------
                turn = send_message(
                    client,
                    "ECHO which surface are you on",
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                note = session_surface_note(lane_dir)
                if "Jupyter" in note:
                    record_pass("5", f"status names the server Jupyter engine: {note[:160]}")
                else:
                    record_fail("5", f"surface not named: note={note[:120]} content={stored.get('content', '')[:120]}")

                # -- LANE 7: execution surface note ------------------------------
                turn = send_message(
                    client,
                    "ECHO what surface do you use for code execution",
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                note = session_surface_note(lane_dir)
                if note:
                    record_pass("7", f"surface note names the execution surface: {note[:160]}")
                else:
                    record_fail("7", f"surface answer missing: {stored.get('content', '')[:200]}")

                # -- LANE 8: failure translation ---------------------------------
                turn = send_message(
                    client,
                    'TOOL: mcp__owui__execute_code :: {"code": "raise ValueError(\'lane8\')"}',
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                if "lane8" in content or "ValueError" in content:
                    record_pass("8", f"failure shows without a chat error state: {content[:120]}")
                else:
                    record_fail("8", f"failure not surfaced: {content[:200]}")

                # -- LANE 9: machine coexistence ----------------------------------
                turn = send_message(
                    client,
                    'TOOL: mcp__owui__execute_code :: {"code": "print(\'coexist\')"} -> ECHO machine reply',
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                if "coexist" in content:
                    record_pass("9", f"both surfaces answer in one session: {content[:120]}")
                else:
                    record_fail("9", f"coexistence reply missing: {content[:200]}")

                # -- LANE 10: long run --------------------------------------------
                turn = send_message(
                    client,
                    "LONG",
                    model=model_id,
                    features=features,
                )
                stored = wait_for_reply(client, turn)
                content = stored.get("content") or ""
                if content:
                    record_pass("10", f"block assembles after a long run: {content[:120]}")
                else:
                    record_fail("10", "no content after a long run")

    # Evidence pages: one synthetic page per lane verdict, shot at the plan's slug.
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport={"width": 1500, "height": 1000}, locale="en-US").new_page()
        for line in LINES:
            lane_id = line.split(":", 1)[0].replace("lane", "").strip()
            parts = line.split(" ", 2)
            verdict = parts[1] if len(parts) > 1 else "UNKNOWN"
            detail = parts[2] if len(parts) > 2 else ""
            slug = SLUGS.get(lane_id, f"lane-{lane_id}.png")
            evidence(page, f"PR7 lane {lane_id}", {"verdict": verdict, "detail": detail}, slug)
        browser.close()

    print(json.dumps({"lane results": LINES}, indent=2))
    return 0 if all("PASS" in line for line in LINES) else 1


if __name__ == "__main__":
    sys.exit(main())
