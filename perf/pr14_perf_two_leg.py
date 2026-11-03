"""The PR14 two-leg perf probe: the interleaved scripted-provider script, trunk vs head.

Runs the PR3-era interleaved parity script against a clean trunk checkout and
the requested PR head, both legs booted in the same runner process before any
turn is timed, so the trunk-vs-head deltas are hardware-comparable. Metric:
first-token latency (the turn's send dispatch to the first `response:completion`
delta event) and streamed tokens per second (deltas over the streaming window).
Rule: the PR3 rule — head's first-token regression > 300 ms, or head's
throughput below 90% of trunk's — which stands as the PR14 10% drift gate.

Run: .venv/bin/python pr14_perf_two_leg.py <kit-dir> <head-checkout> <trunk-checkout>
"""
from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True

KIT = Path(sys.argv[1]).resolve()
HEAD_ROOT = Path(sys.argv[2]).resolve()
TRUNK_ROOT = Path(sys.argv[3]).resolve()
OUT = KIT / "perf"
WARMUP_TURNS = 1          # per leg, excluded from the samples
ROUNDS = 5                # ten turns per side per round -> 50 turns per leg
TURNS_PER_SIDE = 10
PIECES = 10               # scripted deltas per reply
CHUNK_DELAY = 0.02        # seconds between provider pieces (identical both legs)
TURN_TIMEOUT = 60.0

QUIET_TASKS = {
    "ENABLE_TITLE_GENERATION": "false",
    "ENABLE_TAGS_GENERATION": "false",
    "ENABLE_FOLLOW_UP_GENERATION": "false",
    "ENABLE_SEARCH_QUERY_GENERATION": "false",
    "ENABLE_RETRIEVAL_QUERY_GENERATION": "false",
}
PROMPTS = [
    "counter turn {i}: give me the scripted reply, nothing else",
    "counter turn {i}: same shape, then stop",
    "counter turn {i}: the reply text is known, read it out",
    "counter turn {i}: confirm the count and end the turn",
    "counter turn {i}: echo the numbered pieces verbatim",
]

os.environ.setdefault("TMPDIR", os.environ.get("RUNNER_TEMP", "/tmp"))

# receipt stamps: the fake records no timing itself, so the probe
# stamps the arrival of every OWUI outbound request at the fake;
# the stamp list is per leg (two legs share one probe process)
class _TimedList(list):
    def __init__(self, receipts: list[tuple[float, str]]):
        self.receipts = receipts
        super().__init__()

    def append(self, entry):
        self.receipts.append((time.monotonic(), entry.path))
        return list.append(self, entry)

    def clear(self):
        self.receipts.clear()
        return list.clear(self)

frames_of = lambda entry: (entry or {}).get("data") or {}
"""The socket `events` envelope: {chat_id, message_id, data: event_data}.
event_data = {type: 'chat:completion' or 'response:completion' or ...},
data: ...} — the inner data is the streaming payload, so a delta is
filed at TWO frames from the envelope."""
def payload_of(entry: dict) -> dict:
    event = frames_of(entry)
    return event.get("data") if isinstance(event.get("data"), dict) else {}
def kind_of(entry: dict) -> str:
    return frames_of(entry).get("type") or ""

from harness.actors import admin_of
from harness.chat import send_message
from harness.instance import launch
from harness.socket_client import connected
from harness.upstream import MOCK_MODEL_ID, serve as serve_upstream, text

def head_sha(root: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
    )
    return out.stdout.strip() or "unknown"

class Leg:
    """One booted leg: its own provider, instance, admin, socket tab and chat chain."""

    def __init__(self, name: str, root: Path, backend: Path, stack: contextlib.ExitStack):
        self.name = name
        self.root = root
        os.environ["OPEN_WEBUI_CHECKOUT"] = str(root)
        os.environ["OPEN_WEBUI_SOURCE_DIR"] = str(backend)
        self.upstream, shutdown = serve_upstream()
        self.receipts: list = []
        self.upstream.requests = _TimedList(self.receipts)
        # push expects exception-triple callbacks; a zero-arg
        # shutdown registers via callback
        stack.callback(shutdown)
        self.instance = stack.enter_context(
            contextlib.contextmanager(launch)(self.upstream, QUIET_TASKS)
        )
        self.admin = admin_of(self.instance)
        # the mock model is invisible until the admin's visibility publish lands
        grant = {"principal_type": "user", "principal_id": "*", "permission": "read"}
        with self.admin.client() as client:
            client.get("/api/models").raise_for_status()
            shared = client.post(
                "/api/v1/models/model/access/update",
                json={"id": MOCK_MODEL_ID, "name": MOCK_MODEL_ID, "access_grants": [grant]},
            )
        assert shared.status_code == 200, f"{name}: publishing the model failed: {shared.text}"
        self.socket = stack.enter_context(connected(self.admin))
        self.chat_id = None
        self.parent_id = None
        self.booted = time.monotonic()

def run_turn(leg: Leg, turn_index: int) -> dict:
    """One scripted turn: tied to the leg's running chat chain, timed at the ring."""
    prompt = PROMPTS[turn_index % len(PROMPTS)].format(i=turn_index)
    leg.upstream.reset()
    leg.upstream.queue(
        text([f"token {piece}." for piece in range(PIECES)], chunk_delay=CHUNK_DELAY)
    )
    # count every event that arrives after this turn's dispatch begins; the turns
    # run strictly serially, so attribution to in-flight turn is unambiguous
    cursor = len(leg.socket.events)
    t_send = time.monotonic()
    with leg.admin.client() as client:
        turn = send_message(client, prompt, chat_id=leg.chat_id, parent_id=leg.parent_id)
    sample: dict = {
        "turn": turn_index,
        "first_token_ms": None,
        "stream_ms": None,
        "tokens_per_s": None,
        "deltas": 0,
        "done": False,
    }
    stream_range: list[float] = []
    deadline = time.monotonic() + TURN_TIMEOUT
    while time.monotonic() < deadline:
        now = time.monotonic()
        events = leg.socket.events
        for entry in events[cursor:]:
            kind = kind_of(entry)
            payload = payload_of(entry)
            if kind == "response:completion" and str(payload.get("type", "")).endswith(".delta"):
                sample["deltas"] += 1
                if sample["first_token_ms"] is None:
                    sample["first_token_ms"] = round((now - t_send) * 1000, 2)
                stream_range.append(now)
            elif kind == "chat:completion" and payload.get("done") is True:
                sample["done"] = True
        cursor = len(events)
        if sample["done"]:
            break
        time.sleep(0.005)
    else:
        # turn timeout: the decisive triad before the assert
        kinds: dict[str, int] = {}
        for entry in leg.socket.events:
            key = kind_of(entry) or "<none>"
            kinds[key] = kinds.get(key, 0) + 1
        print(f"[probe abst] {leg.name} turn {turn_index}: kinds={kinds}", flush=True)
        for entry in leg.socket.events:
            if kind_of(entry) in ("response:completion", "chat:completion"):
                print(f"[probe abst] frame payload: {json.dumps(payload_of(entry))[:240]}", flush=True)
        leg.receipts_lines = "\n".join(
            f"  +{ts - leg.booted:5.1f}s {path}" for ts, path in leg.receipts
        ) if leg.receipts else "  (none)"
        print(f"[probe abst] {leg.name} turn {turn_index}: provider receipts\n{leg.receipts_lines}", flush=True)
        with leg.admin.client() as client:
            try:
                stored = client.get(f"/api/v1/chats/{turn.chat_id}").json() or {}
                last = ((stored.get("chat") or {}).get("history") or ({}))  # chat.history.messages keyed by id
                last = (last.get("messages") or {}).get(turn.assistant_message_id, {})
                print(f"[probe abst] {leg.name} turn {turn_index}: stored done={last.get('done')} "
                      f"content_len={len(last.get('content') or '')}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[probe abst] {leg.name} turn {turn_index}: stored read failed: {e}", flush=True)
    leg.chat_id = turn.chat_id
    leg.parent_id = turn.assistant_message_id
    if not sample["done"] or sample["first_token_ms"] is None:
        raise AssertionError(f"{leg.name} turn {turn_index}: the reply never streamed done: {sample}")
    if len(stream_range) >= 2:
        sample["stream_ms"] = round((stream_range[-1] - stream_range[0]) * 1000, 2)
        if sample["stream_ms"] > 0:
            sample["tokens_per_s"] = round(
                sample["deltas"] / (sample["stream_ms"] / 1000.0), 2
            )
    return sample

def main() -> None:
    started = datetime.now(timezone.utc).isoformat()
    samples: dict[str, list[dict]] = {"trunk": [], "head": []}
    phases: list[str] = []
    stack = contextlib.ExitStack()
    try:
        trunk = Leg("trunk", TRUNK_ROOT, TRUNK_ROOT / "backend", stack)
        head = Leg("head", HEAD_ROOT, HEAD_ROOT / "backend", stack)
        phases.append("both legs booted; provider scripting identical across legs")
        for leg in (trunk, head):
            for warm_index in range(WARMUP_TURNS):
                run_turn(leg, 999 + warm_index)  # the warmup turn is excluded
        phases.append("both legs warmed; timed rounds begin")

        for round_no in range(1, ROUNDS + 1):
            order = ["trunk", "head"] if round_no % 2 else ["head", "trunk"]
            legs = {"trunk": trunk, "head": head}
            for leg_name in order:
                leg = legs[leg_name]
                for turn_index in range(TURNS_PER_SIDE):
                    sample = run_turn(leg, turn_index)
                    sample["round"] = round_no
                    samples[leg_name].append(sample)
                    print(
                        f"[perf] round {round_no} {leg_name} turn {turn_index}: "
                        f"first {sample['first_token_ms']} ms, "
                        f"{sample['deltas']} deltas in {sample['stream_ms']} ms",
                        flush=True,
                    )
    finally:
        stack.close()

    med_trunk_first = statistics.median(s["first_token_ms"] for s in samples["trunk"])
    med_head_first = statistics.median(s["first_token_ms"] for s in samples["head"])
    med_trunk_tps = statistics.median((s["tokens_per_s"] or 0) for s in samples["trunk"])
    med_head_tps = statistics.median((s["tokens_per_s"] or 0) for s in samples["head"])
    first_delta = med_head_first - med_trunk_first
    tps_ratio = (med_head_tps / med_trunk_tps) if med_trunk_tps else 0.0
    fail_first = first_delta > 300
    fail_tps = tps_ratio < 0.90
    report = {
        "generated_at": started,
        "head": head_sha(HEAD_ROOT),
        "trunk": head_sha(TRUNK_ROOT),
        "turns_per_leg": ROUNDS * TURNS_PER_SIDE,
        "methodology": {
            "probe": "the scripted-provider parity script interleaved, ten turns per side per round with the starting leg swapped per round, both legs booted fully before the first timed turn; the chained plain-chat shape continues one chat per leg",
            "first_token_ms": "send dispatch to the first response:completion delta event (5 ms instrument poll, identical both legs); the provider's 90 ms REPLY_START_DELAY and the scripted chunk pacing are inside both legs equally",
            "tokens_per_s": "deltas / the streaming window (first to last delta event)",
            "phases": phases,
        },
        "samples": {name: {"n": len(runs), "runs": runs} for name, runs in samples.items()},
        "medians": {
            "trunk_first_token_ms": round(med_trunk_first, 2),
            "head_first_token_ms": round(med_head_first, 2),
            "first_token_delta_ms": round(first_delta, 2),
            "trunk_tokens_per_s": round(med_trunk_tps, 2),
            "head_tokens_per_s": round(med_head_tps, 2),
            "throughput_ratio": round(tps_ratio, 4),
        },
        "rule": {
            "pr3_first_token_regression_gt_300ms": fail_first,
            "pr14_throughput_drift_gt_10pct": fail_tps,
            "verdict": "FAIL" if (fail_first or fail_tps) else "PASS",
        },
        "hardware": {
            "runner": os.environ.get("RUNNER_OS", "unknown"),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": multiprocessing.cpu_count(),
        },
        "baseline_note": "this runner run calibrates the PR3-splice baseline on Actions hardware; the next probe legs compare against these medians rather than the 12 GB box's numbers",
    }
    (OUT / "perf-probe.json").write_text(json.dumps(report, indent=1) + "\n")
    print(
        f"[perf] verdict {report['rule']['verdict']}: medians {report['medians']}",
        flush=True,
    )
    if report["rule"]["verdict"] == "FAIL":
        raise SystemExit(1)

if __name__ == "__main__":
    main()
