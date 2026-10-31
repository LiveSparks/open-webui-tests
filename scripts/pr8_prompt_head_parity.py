"""The PR8 unit lane: run the prompt-head parity module at trunk and at the PR head.

The prompt head OWUI assembles for the must-tier scenarios must be the same
head at both legs on the same fixture, so the runner boots each leg through
the one boot recipe (`agent_sidecar/boot_sidecar.sh`, sidecar 5008, fake
15008 — the PR8 lane ports), runs `integration/chat/agent_prompt_head.py`
once per checkout, and diffs the captures:

- voice-prompt, memory-injection, web-search-results, skills-head,
  attached-knowledge: equal after lane-path normalization — the head is
  unchanged, trunk shape held (the parity bar).
- terminal-agents-md and spoken-transcript-merge: documented contract
  changes — the rides exist at the head and are pinned absent at trunk.

Run through the serial wrapper:

    bash /root/open-webui/.orchestrate/pi-engine/locks/serial-run.sh \
      /tmp/open-webui-tests/.venv/bin/python \
      /var/tmp/pr8-tests/scripts/pr8_prompt_head_parity.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for the normalize helper

KIT = Path("/var/tmp/pr8-tests")
SHARED_VENV = Path("/tmp/open-webui-tests/.venv/bin/python")
SWARM = Path("/tmp/swarm-pr8/owner")
FAKE_PORT = 15008
EQUALITY_SCENARIOS = ("voice-prompt", "memory-injection", "web-search-results", "skills-head", "attached-knowledge")
RIDE_SCENARIOS = {"terminal-agents-md": "AGENTS.md probe", "spoken-transcript-merge": "SPOKEN pr8-voicehistory"}
LANE = {role: Path(f"/tmp/agent_prompt_head-{role}") for role in ("head", "trunk")}


def log(message: str) -> None:
    print(f"[pr8-parity] {message}", flush=True)


def pkill_fake() -> None:
    subprocess.run(["pkill", "-f", "[o]penai_fake.*15008"], check=False)
    time.sleep(0.6)


def run_leg(role: str, checkout: Path, backend: Path) -> None:
    lane = LANE[role]
    lane.mkdir(parents=True, exist_ok=True)
    (lane / "captures.json").unlink(missing_ok=True)
    env = {
        **os.environ,
        "TMPDIR": "/var/tmp",
        "OPEN_WEBUI_CHECKOUT": str(checkout),
        "OPEN_WEBUI_SOURCE_DIR": str(backend),
        "PR8_PARITY_ROLE": role,
        "AGENT_SIDECAR_LANE_DIR": str(lane),
        "OPENAI_FAKE_LOG": str(lane / "fake-requests.log"),
        "OPENAI_FAKE_CHUNK_DELAY": "0",
    }
    started = time.monotonic()
    pytest_run = subprocess.run(
        [str(SHARED_VENV), "-m", "pytest", "integration/chat/agent_prompt_head.py", "-r", "sA", "--tb=short"],
        cwd=str(KIT),
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    (lane / "pytest.log").write_text(pytest_run.stdout + pytest_run.stderr)
    log(f"{role} leg pytest exit {pytest_run.returncode} in {time.monotonic() - started:.0f}s; log {lane}/pytest.log")
    if pytest_run.returncode != 0:
        print((pytest_run.stdout + pytest_run.stderr)[-4000:], flush=True)


def captures(role: str) -> dict:
    path = LANE[role] / "captures.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def normalized_preamble(captures_: dict, name: str, role: str) -> str:
    from integration.chat.agent_prompt_head import normalize

    return normalize(captures_[name]["preamble"])


def transcript_rides(entry: dict, needle: str = RIDE_SCENARIOS["spoken-transcript-merge"]) -> bool:
    return any(needle in turn for turn in entry.get("turns", []))


def diff_legs(head: dict, trunk: dict) -> list[str]:
    report: list[str] = []
    ok = True
    for role, leg in (("head", head), ("trunk", trunk)):
        if not leg:
            return [f"FAIL: the {role} leg captured nothing (pytest run failed; see its pytest.log)"]
    for name in EQUALITY_SCENARIOS:
        if name not in head or name not in trunk:
            ok = False
            report.append(f"FAIL: {name} missing from a leg's captures")
            continue
        if normalized_preamble(head, name, "head") == normalized_preamble(trunk, name, "trunk"):
            report.append(f"PASS: {name} — the assembled head equals trunk's on this fixture")
        else:
            ok = False
            report.append(f"FAIL: {name} — the assembled heads differ: diff dump below")
            report.append("  head preamble:")
            report.extend("      " + line for line in head[name]["preamble"].splitlines()[:80])
            report.append("  trunk preamble:")
            report.extend("      " + line for line in trunk[name]["preamble"].splitlines()[:80])
    head_agents = "terminal-agents-md" in head and RIDE_SCENARIOS["terminal-agents-md"] in head["terminal-agents-md"]["preamble"]
    trunk_agents = "terminal-agents-md" in trunk and RIDE_SCENARIOS["terminal-agents-md"] in trunk["terminal-agents-md"]["preamble"]
    if head_agents and not trunk_agents:
        report.append("PASS: terminal-agents-md — the machine's AGENTS.md joins the head, and stays off it at trunk")
    else:
        ok = False
        report.append(f"FAIL: terminal-agents-md — head ride={head_agents} trunk ride={trunk_agents}")
    spoken_needles = {
        "spoken-transcript-merge": "SPOKEN pr8-voicehistory",
        "spoken-answer-merge": "SPOKEN pr8-answer-spoken",
    }
    for name, needle in spoken_needles.items():
        if name not in head or name not in trunk:
            ok = False
            report.append(f"FAIL: {name} missing from a leg's captures")
            continue
        head_ride = transcript_rides(head[name], needle)
        trunk_ride = transcript_rides(trunk[name], needle)
        if head_ride and not trunk_ride:
            report.append(f"PASS: {name} — the merge rides the turn at the head and reads as a trunk lack")
        else:
            ok = False
            report.append(f"FAIL: {name} — head ride={head_ride} trunk ride={trunk_ride}")
    report.insert(0, f"prompt-head parity diff: {'PASS' if ok else 'FAIL'}")
    return report


def main() -> int:
    SWARM.mkdir(parents=True, exist_ok=True)
    log("head leg: boot + parity at the PR head")
    run_leg("head", Path(os.environ.get("OPEN_WEBUI_CHECKOUT", "/var/tmp/pr8-work")), Path(os.environ.get("OPEN_WEBUI_SOURCE_DIR", "/var/tmp/pr8-work/backend")))
    log("trunk leg: boot + parity at trunk dev")
    pkill_fake()
    trunk_checkout = Path(os.environ.get("PR8_TRUNK_CHECKOUT", "/var/tmp/pr8-trunk"))
    run_leg("trunk", trunk_checkout, trunk_checkout / "backend")
    pkill_fake()
    report = diff_legs(captures("head"), captures("trunk"))
    text = "\n".join(report) + "\n"
    (SWARM / "parity-diff.txt").write_text(
        "\n".join(report) + "\n--- trunk captures ---\n" + json.dumps(captures("trunk"), indent=2) + "\n--- head captures ---\n" + json.dumps(captures("head"), indent=2)
    )
    for role in ("head", "trunk"):
        src = LANE[role] / "pytest.log"
        if src.is_file():
            (SWARM / f"pytest-{role}.log").write_text(src.read_text()[-30000:])
    log(text)
    return 0 if text.startswith("prompt-head parity diff: PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
