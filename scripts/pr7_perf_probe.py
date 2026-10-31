"""The PR7 perf probe: 200 scripted result frames, interleaved trunk vs head.

Re-authored after rejection. The rejected probe timed
run_lanes.translate_execute_code (ci_parity.to_code_interpreter_item) on BOTH
legs — a self-comparison that measured nothing. This probe is the
function-level-with-real-code shape:

  shape = "function-level-real-code"

  trunk leg: /tmp/perf-trunk/backend (a5074414e-lineage, legacy renderer path)
             real handler:  open_webui.utils.middleware.process_tool_result
             (middleware.py; every native tool-result frame goes through it
             after trunk's execute_code builtin returns the frame as JSON).
  head leg:  /tmp/pr7-work/backend (66aee56cf)
             real handler:  open_webui.routers.agent._to_code_interpreter_item
             (agent.py; the PR7 translator that maps the bridged execute_code
             result onto the native open_webui:code_interpreter item).

The two legs are different functions from two different checkouts; no parity
helper and no run_lanes translation surface is timed. Each leg runs in its
own process with that checkout's backend first on sys.path (both trees export
the `open_webui` package, so two processes are required). Both consume the
same 200 scripted result frames — run_lanes.scripted_frame shapes, every
fifth frame carrying a base64 image data URI. Trunk is baselined first, then
the frames interleave. Metric: translation overhead per result frame in ms
(head median minus trunk median); budget: 10 ms/frame.

Run: TMPDIR=/var/tmp /tmp/open-webui-tests/.venv/bin/python \
     /tmp/open-webui-tests/scripts/pr7_perf_probe.py
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

os.environ["TMPDIR"] = "/var/tmp"

OUT = Path("/tmp/swarm-pr7/owner")
TRUNK = Path("/tmp/perf-trunk")
HEAD = Path("/tmp/pr7-work")
FRAMES = 200
WARMUP = 20
BUDGET_MS = 10.0


def head_sha(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def scripted_frame(leg: str, i: int) -> tuple[str, dict]:
    """Identical shapes to run_lanes.scripted_frame; every fifth frame is an
    base64-image frame."""
    code = f"print('frame-{leg}-{i}')"
    stdout = f"frame-{leg}-{i}\n"
    if i % 5 == 0:
        stdout += "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    return code, {"stdout": stdout, "stderr": "", "result": "None"}


# ---------------------------------------------------------------------------
# Leg worker: times the real handler of one checkout over stdin protocols.
# ---------------------------------------------------------------------------

WORKER_SRC = r'''
import asyncio, json, os, sys, time

os.environ["TMPDIR"] = "/var/tmp"
BACKEND, LEG = sys.argv[1], sys.argv[2]
sys.dont_write_bytecode = True
sys.path.insert(0, BACKEND)          # this leg's checkout is the open_webui tree

sys.path.insert(0, "/tmp/open-webui-tests/scripts")
import pr7_perf_probe as driver      # scripted_frame shapes only

if LEG == "trunk":
    from open_webui.utils.middleware import process_tool_result
    import open_webui.utils.middleware as _m
    HANDLER = "open_webui.utils.middleware.process_tool_result"
    HANDLER_PATH = _m.__file__

    class _FakeRequest:
        pass

    _USER = {
        "id": "perf-user",
        "name": "Perf Probe",
        "role": "admin",
        "email": "perf@probe.local",
    }
    _loop = asyncio.new_event_loop()

    def build(i):
        _code, result = driver.scripted_frame("trunk", i)
        # trunk's execute_code builtin hands middleware the frame as a JSON
        # string; that is exactly what process_tool_result receives per frame.
        payload = json.dumps(result, ensure_ascii=False)  # outside timing
        meta = {"chat_id": "perf-chat", "message_id": f"m{i}"}
        return payload, meta

    def run(i):
        payload, meta = build(i)
        t0 = time.perf_counter_ns()
        result, files, embeds = _loop.run_until_complete(
            process_tool_result(
                _FakeRequest(), "execute_code", payload, "function", False,
                meta, _USER,
            )
        )
        dt = (time.perf_counter_ns() - t0) / 1_000_000
        assert isinstance(result, str) and not files and not embeds
        return dt
else:
    from open_webui.routers.agent import _to_code_interpreter_item
    import open_webui.routers.agent as _a
    HANDLER = "open_webui.routers.agent._to_code_interpreter_item"
    HANDLER_PATH = _a.__file__

    def build(i):
        code, result = driver.scripted_frame("head", i)
        return code, result

    def run(i):
        code, result = build(i)
        t0 = time.perf_counter_ns()
        item = _to_code_interpreter_item(code, result)
        dt = (time.perf_counter_ns() - t0) / 1_000_000
        want_img = [{"type": "png", "base64": "iVBORw0KGgoAAAANSUhEUg=="}]
        assert item["type"] == "open_webui:code_interpreter"
        assert item["stderr"] == ""
        assert item["images"] == (want_img if result["stdout"].count("data:image") else [])
        assert item["stdout"] == result["stdout"]
        return dt

print(f"h {HANDLER} {HANDLER_PATH}", flush=True)
print("ready", flush=True)
for line in sys.stdin:
    op, arg = line.split()
    if op == "warm":
        run(int(arg))
        print("ok", flush=True)
    elif op == "run":
        print(f"s {run(int(arg))}", flush=True)
    else:
        print(f"e bad op {op}", flush=True)
'''


class LegWorker:
    def __init__(self, leg: str, checkout: Path):
        home = Path("/tmp/pr7-perf-probe") / leg
        home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "TMPDIR": "/var/tmp",
                "DATA_DIR": str(home),
                "DATABASE_URL": f"sqlite:///{home / 'webui.db'}",
                "WEBUI_SECRET_KEY": "pr7-perf-probe",
            }
        )
        self.leg = leg
        self.proc = subprocess.Popen(
            [sys.executable, "-c", WORKER_SRC, str(checkout / "backend"), leg],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        info = self._nextline("h ")
        self.handler, self.handler_path = info[2:].split(" ", 1)
        assert self._nextline() == "ready"
        self.samples: list[float] = []

    def _nextline(self, prefix: str = "") -> str:
        """Read protocol lines, skipping handler logging (alembic et al.)."""
        while True:
            line = self.proc.stdout.readline().strip()
            assert line, f"worker {self.leg}: stdout closed before {prefix!r}"
            if line.startswith(prefix):
                return line

    def warm(self, i: int) -> None:
        self.proc.stdin.write(f"warm {i}\n")
        self.proc.stdin.flush()
        assert self._nextline("ok") == "ok"

    def run(self, i: int) -> float:
        self.proc.stdin.write(f"run {i}\n")
        self.proc.stdin.flush()
        line = self._nextline("s ")
        ms = float(line[2:])
        self.samples.append(ms)
        return ms

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait(timeout=30)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    trunk = LegWorker("trunk", TRUNK)
    head = LegWorker("head", HEAD)
    try:
        # Trunk baseline first: trunk is warmed and measured before head is
        # touched; the head leg only warms after trunk's first samples exist.
        for i in range(WARMUP):
            trunk.warm(i)
        head_samples = []
        for i in range(WARMUP):
            head.warm(i)
        # Interleaved measured frames; the starting leg alternates per round.
        trunk_samples = []
        for i in range(FRAMES):
            order = ("head", "trunk") if i % 2 else ("trunk", "head")
            for leg in order:
                ms = (trunk if leg == "trunk" else head).run(i)
                if leg == "head":
                    head_samples.append(ms)
        with open("/tmp/pr7-perf-probe/interleave.json", "w") as f:
            json.dump({"trunk": trunk.samples, "head": head.samples}, f)
        median_trunk = statistics.median(trunk.samples)
        median_head = statistics.median(head_samples)
        delta = round(median_head - median_trunk, 3)
        verdict = "PASS" if delta <= BUDGET_MS else "FAIL"
        report = {
            "probe": "pr7_perf_probe",
            "shape": "function-level-real-code",
            "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trunk_head_sha": head_sha(TRUNK),
            "head_head_sha": head_sha(HEAD),
            "frames": FRAMES,
            "warmup_frames": WARMUP,
            "legs": {
                "trunk": {
                    "checkout": "/tmp/perf-trunk/backend",
                    "invoked": trunk.handler,
                    "file": trunk.handler_path,
                },
                "head": {
                    "checkout": "/tmp/pr7-work/backend",
                    "invoked": head.handler,
                    "file": head.handler_path,
                },
            },
            "trunk_median_ms": round(median_trunk, 4),
            "head_median_ms": round(median_head, 4),
            "median_delta_ms": delta,
            "budget_ms": BUDGET_MS,
            "verdict": verdict,
        }
        (OUT / "pr7-perf.json").write_text(json.dumps(report, indent=2) + "\n")
        with (OUT / "pr7-perf.log").open("w") as log:
            log.write(f"pr7 perf probe: {report}\n")
            log.write(
                f"trunk: median={median_trunk:.4f}ms n={len(trunk.samples)}\n"
                f"head:  median={median_head:.4f}ms n={len(head_samples)}\n"
                f"delta={delta:+.3f}ms (budget {BUDGET_MS}ms) -> {verdict}\n"
            )
        print(
            f"pr7 perf: trunk {median_trunk:.4f}ms, head {median_head:.4f}ms, "
            f"delta {delta:+.3f}ms -> {verdict}"
        )
    finally:
        trunk.close()
        head.close()


if __name__ == "__main__":
    main()
