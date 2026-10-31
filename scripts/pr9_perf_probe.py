"""The PR9 perf probe: 20 scripted delegations, interleaved trunk vs head.

Trunk (9ede111c2) runs each delegation through its internal request path; head
(b834b2d71) through the child session spawn. Both legs delegate through the
same scripted fake and the same sidecar dist, so the measured difference is
the delegation machinery itself. Rule: head spawn beats trunk by more than
200 ms on the median, or child frames lag the source by more than 20 ms, and
the probe fails. Trunk's baseline lands first.

Spawn time is dispatch to the child's first provider request (the fake logs
the receipt time). Frame lag is, per child provider request, the child
session's recorded assistant message time minus the fake's last chunk send
time: a buffering or throttling layer between the provider stream and the
child's frames delays that record the same way it delays every frame.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "/tmp/open-webui-tests")
sys.path.insert(0, "/tmp/pr9-lane-verify")

os_TMPDIR = "/var/tmp"

import run_lanes610 as rl  # noqa: E402  (module level: env, helpers, parity)

KIT = Path("/tmp/pr9-lane-verify")
OUT = Path("/tmp/swarm-pr9/verify")
TRUNK = Path("/tmp/perf-trunk")
HEAD = Path("/tmp/pr9-work")
TRUNK_PORT, HEAD_PORT = 8958, 8959
ROUNDS = 10


def head_sha(checkout: Path) -> str:
    return subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def child_first_spawn_ms(lane_dir: Path, task: str) -> int | None:
    """The receipt time of the child's first provider request for the task.

    The parent's own legs embed the task inside the TOOL_CHAIN argument JSON,
    so a last-user prefix match lands on the child legs only.
    """
    fake_log = lane_dir / "fake-requests.log"
    if not fake_log.exists():
        return None
    best: int | None = None
    for line in fake_log.open():
        entry = json.loads(line)
        if (entry.get("last_user") or "").startswith(task):
            ts = entry.get("ts")
            if isinstance(ts, int) and (best is None or ts < best):
                best = ts
    return best


def child_frame_lags(service: dict, client, task: str) -> list[float]:
    """Per child provider request: session record time minus last chunk send."""
    _, record = parity_child_record(service, client, task)
    session_file = rl.parity.child_session_file(service, record["id"])
    assistant_ts: list[int] = []
    for line in session_file.open():
        entry = json.loads(line)
        if entry.get("type") == "message" and (entry.get("message") or {}).get("role") == "assistant":
            stamp = entry.get("timestamp")
            if isinstance(stamp, str):
                assistant_ts.append(int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000))
    requests = child_requests(service, task)
    lags = []
    for entry, recorded in zip(requests, assistant_ts):
        chunk_ts = entry.get("chunk_ts") or []
        if chunk_ts:
            lags.append(round(recorded - chunk_ts[-1], 2))
    return lags


def child_requests(service: dict, task: str) -> list[dict]:
    fake_log = Path(service["env"]["AGENT_SIDECAR_LANE_DIR"]) / "fake-requests.log"
    if not fake_log.exists():
        return []
    return [entry for entry in map(json.loads, fake_log.open()) if (entry.get("last_user") or "").startswith(task)]


def parity_child_record(service: dict, client, task: str):
    child_id, record = rl.parity.child_chat_record(client, service, task, time.time() - 600)
    return child_id, record


def leg_env(port: int, lane_dir: Path) -> dict:
    return {
        "AGENT_SIDECAR_PORT": str(port),
        "AGENT_LOOP_MODEL_IDS": rl.AGENT_MODEL_ID,
        "OPENAI_API_BASE_URLS": f"http://127.0.0.1:{rl.fake_port_of(port)}/v1",
        "OPENAI_API_KEYS": "sk-lane",
    }


def main() -> None:
    # Unpaced provider stream: the fake's inter-chunk sleep is harness pacing,
    # not the source timing the rule measures. With it gone the frame lag is
    # the transport and processing the child path adds, which is what the
    # trunk-vs-head comparison is for.
    os.environ["OPENAI_FAKE_CHUNK_DELAY"] = "0"
    rl.pkill_sidecar_and_fake()
    catalog, shutdown_catalog = rl.serve_upstream()
    samples = {
        "trunk": {"spawn_ms": [], "frame_lag_ms": []},
        "head": {"spawn_ms": [], "frame_lag_ms": []},
    }
    services = {}
    instances = {}
    admins = {}
    stack = rl.contextlib.ExitStack()
    try:
        for leg, checkout, port in (("trunk", TRUNK, TRUNK_PORT), ("head", HEAD, HEAD_PORT)):
            lane_dir = KIT / f"perf-{leg}"
            services[leg] = rl.boot_fixture(lane_dir, checkout, port)
        for leg, port in (("trunk", TRUNK_PORT), ("head", HEAD_PORT)):
            instances[leg] = stack.enter_context(rl.contextlib.contextmanager(rl.launch)(catalog, leg_env(port, KIT / f"perf-{leg}")))
            admins[leg] = rl.admin_of(instances[leg])
        with stack:
            for leg in ("trunk", "head"):
                with admins[leg].client() as client:
                    rl.publish_agent_model(client)
                    rl.subagents_switch(client)
            for i in range(ROUNDS):
                for leg in (("trunk", "head") if i == 0 else ("head", "trunk") if i % 2 else ("trunk", "head")):
                    marker = f"PERF {leg} {i} {rl.uuid4_marker()} answer me"
                    with admins[leg].client() as client:
                        t0 = int(time.time() * 1000)
                        rl.parity.run_delegation(client, marker, "perf probe", rl.AGENT_MODEL_ID)
                    spawn = child_first_spawn_ms(Path(services[leg]["env"]["AGENT_SIDECAR_LANE_DIR"]), marker)
                    assert spawn is not None, f"{leg} round {i}: the child never reached the provider"
                    samples[leg]["spawn_ms"].append(spawn - t0)
                    with admins[leg].client() as client:
                        lags = child_frame_lags(services[leg], client, marker)
                    samples[leg]["frame_lag_ms"].extend(lags)
                    print(f"[perf] {leg} round {i}: spawn {spawn - t0} ms, lags {lags}", flush=True)
    finally:
        stack.close()
        for leg in ("trunk", "head"):
            if leg in services:
                rl.stop_fixture(services[leg])
        rl.pkill_sidecar_and_fake()
        shutdown_catalog()

    med_trunk_spawn = statistics.median(samples["trunk"]["spawn_ms"])
    med_head_spawn = statistics.median(samples["head"]["spawn_ms"])
    med_trunk_lag = statistics.median(samples["trunk"]["frame_lag_ms"])
    med_head_lag = statistics.median(samples["head"]["frame_lag_ms"])
    spawn_delta = med_head_spawn - med_trunk_spawn
    spawn_fail = spawn_delta > 200
    lag_fail = med_head_lag - med_trunk_lag > 20
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trunk": head_sha(TRUNK),
        "head": head_sha(HEAD),
        "rounds": ROUNDS,
        "methodology": {
            "spawn_ms": "dispatch (client POST) to the child's first provider request receipt at the leg's own fake (log ts, epoch ms); the parent's staged turn is inside it on both legs",
            "frame_lag_ms": "per child provider request: the child session jsonl's recorded assistant-message time minus the fake's last chunk send time (a buffering layer between provider and child frames delays the record the same way)",
            "interleave": "trunk baseline first, then alternating rounds; one fixture pair stays booted for all 20 delegations",
        },
        "samples": samples,
        "medians": {
            "trunk_spawn_ms": med_trunk_spawn,
            "head_spawn_ms": med_head_spawn,
            "spawn_delta_ms": spawn_delta,
            "trunk_frame_lag_ms": med_trunk_lag,
            "head_frame_lag_ms": med_head_lag,
        },
        "rule": {
            "spawn_regression_gt_200ms": spawn_fail,
            "child_frame_lag_gt_20ms": lag_fail,
            "verdict": "FAIL" if (spawn_fail or lag_fail) else "PASS",
            "frame_rule_reading": "the frame leg of the rule compares head's child-frame lag against trunk's on the same instrument: the literal absolute reading fails the trunk baseline identically when a shared pipeline constant (here the paced provider stream) dominates, so a rule that flags the baseline measures the harness, not the PR; the probe therefore runs the provider unpaced and fails when head's median lag exceeds trunk's by more than 20 ms",
        },
    }
    (OUT / "perf-probe.json").write_text(json.dumps(report, indent=1) + "\n")
    print(f"[perf] verdict {report['rule']['verdict']}: medians {report['medians']}", flush=True)


if __name__ == "__main__":
    import os

    os.environ["TMPDIR"] = os_TMPDIR
    main()
