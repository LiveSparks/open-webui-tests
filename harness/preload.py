"""Start scratch instances from a process that has already imported the heavy libraries.

Importing the backend takes seconds, most of it torch, transformers and the rest of the
retrieval stack. On Linux the first boot of a session also starts a preload server
(`preload_server.py`) that imports those libraries once; every later boot is forked from it,
so it only pays for Open WebUI's own import and startup. A forked instance gets the same
environment, working directory, log, signals and process group as a plain start, and the harness
stops and waits for it as before. An instance whose environment differs where a preloaded
library read it at import (`HF_HOME`, `TZ`, `PYTHONPATH`) starts the plain way.

What differs from a plain start: forked instances share one hash seed, Arrow's memory pool
runs without its purge thread, and the process shows the server as its parent and command
line. `OWUI_TEST_PRELOAD=0` starts every instance the plain way.
"""

from __future__ import annotations

import atexit
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import warnings
from pathlib import Path

ENABLED = sys.platform == "linux" and os.getenv("OWUI_TEST_PRELOAD", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)

# imported at the top of the backend's modules whatever the settings, so a plain start has them
# all too; langchain_text_splitters brings torch, transformers, spacy and pandas
MODULES = [
    "uvicorn",
    "numpy",
    "bs4",
    "pydantic",
    "requests",
    "redis",
    "aiohttp",
    "fastapi",
    "starlette.middleware.sessions",
    "sqlalchemy.ext.asyncio",
    "authlib.integrations.starlette_client",
    "starsessions.stores.redis",
    "mcp.shared.auth",
    "langchain_core.documents",
    "langchain_classic.retrievers",
    "langchain_text_splitters",
]

# names the backend sets for itself before it imports any of the modules above
OFFLINE_WRITES = {"HF_HUB_OFFLINE": "1"}

EXIT_ENV_CHANGED = 75

_lock = threading.Lock()
_server: _PreloadServer | None = None
_disabled = not ENABLED


class ForkedProcess:
    """The `subprocess.Popen` calls the harness makes, for an instance the preload server forked.

    The server is its parent and reports its exit status; signals go through a pidfd, so a
    reused pid is never hit.
    """

    def __init__(self, connection: socket.socket, pid: int, pidfd: int):
        self._connection = connection
        self._pidfd = pidfd
        self.pid = pid
        self.returncode: int | None = None
        self.args = ["preloaded", str(pid)]

    def poll(self) -> int | None:
        if self.returncode is None:
            self._connection.setblocking(False)
            try:
                self._read_status()
            except BlockingIOError:
                pass
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self._connection.settimeout(timeout)
            try:
                self._read_status()
            except TimeoutError:
                raise subprocess.TimeoutExpired(self.args, timeout) from None
        return self.returncode

    def send_signal(self, sig: int) -> None:
        if self.poll() is None:
            try:
                signal.pidfd_send_signal(self._pidfd, sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def _read_status(self) -> None:
        message = self._connection.recv(4096)
        self._connection.close()
        os.close(self._pidfd)
        if not message:  # the server died, and its death signal took the instance along
            self.returncode = -signal.SIGKILL
            _disable(f"the preload server exited; its log is {_server.log_path}")
            return
        self.returncode = os.waitstatus_to_exitcode(json.loads(message)["status"])
        if self.returncode == EXIT_ENV_CHANGED:
            _disable("an instance changed a setting a preloaded library had read")


class _PreloadServer:
    def __init__(self, backend: Path, boot_import: str, boot_serve: str, base_env: dict[str, str]):
        self.backend = backend
        self.scratch = Path(tempfile.mkdtemp(prefix="owui-preload-"))
        self.control_path = str(self.scratch / "control")
        self.log_path = self.scratch / "server.log"
        lifeline_read, self._lifeline = os.pipe()
        self._ready, ready_write = os.pipe()
        source = (Path(__file__).parent / "preload_server.py").read_text(encoding="utf-8")
        with open(self.log_path, "w", encoding="utf-8") as log_file:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    source,
                    str(backend),
                    self.control_path,
                    str(lifeline_read),
                    str(ready_write),
                    boot_import,
                    boot_serve,
                    json.dumps(MODULES),
                ],
                env=base_env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                pass_fds=(lifeline_read, ready_write),
            )
        os.close(lifeline_read)
        os.close(ready_write)
        self._state: dict | None = None

    def ready(self) -> bool:
        """Whether the server took up requests; waits for it to finish its imports."""
        if self._state is None:
            waiting = select.poll()
            waiting.register(self._ready, select.POLLIN)
            with os.fdopen(self._ready, encoding="utf-8") as ready:
                text = ready.read() if waiting.poll(300_000) else ""
            self._state = json.loads(text) if text else {"disabled": "it exited while importing"}
            if "disabled" in self._state:
                log = self.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                warnings.warn(f"instances start the plain way: {self._state['disabled']}\n{log}")
        return "ready" in self._state

    def fork(self, argv: list[str], env: dict[str, str], log_path: Path) -> ForkedProcess | None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.settimeout(60)
        connection.connect(self.control_path)
        import_env = dict(env)
        if env.get("OFFLINE_MODE", "false").lower() == "true":
            import_env.update(OFFLINE_WRITES)
        request = {
            "argv": argv,
            "env": env,
            "import_env": import_env,
            "cwd": os.getcwd(),
            "log": str(log_path),
        }
        connection.send(json.dumps(request).encode())
        message, fds, _, _ = socket.recv_fds(connection, 1 << 16, 1)
        reply = json.loads(message)
        if "refused" in reply:
            connection.close()
            return None
        os.set_inheritable(fds[0], False)
        return ForkedProcess(connection, reply["pid"], fds[0])

    def close(self) -> None:
        os.close(self._lifeline)
        if "ready" not in (self._state or {}):
            self.process.kill()  # no instance came from it
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
        keep_logs_in = os.getenv("OPEN_WEBUI_LOG_DIR")
        if keep_logs_in:
            Path(keep_logs_in).mkdir(parents=True, exist_ok=True)
            shutil.copy(self.log_path, Path(keep_logs_in) / "preload-server.log")
        shutil.rmtree(self.scratch, ignore_errors=True)


def _disable(reason: str) -> None:
    global _disabled
    if not _disabled:
        _disabled = True
        warnings.warn(f"instances start the plain way from now on: {reason}")


def _base_env(isolated: dict[str, str]) -> dict[str, str]:
    """What a plain start's environment holds when the backend imports the modules."""
    env = {**os.environ, **isolated, "PYTHONUNBUFFERED": "1", **OFFLINE_WRITES}
    env.pop("COVERAGE_PROCESS_START", None)  # each instance starts its own coverage
    return env


def fork(
    backend: Path,
    port: int,
    env: dict[str, str],
    log_path: Path,
    boot_import: str,
    boot_serve: str,
    isolated: dict[str, str],
) -> ForkedProcess | None:
    """The instance forked from the preload server, or None when it has to start the plain way.

    The first call starts the server and returns None, so that boot does not wait for it.
    """
    global _server
    with _lock:
        if _disabled or backend.parent.joinpath(".env").exists():
            return None
        if _server is None:
            _server = _PreloadServer(backend, boot_import, boot_serve, _base_env(isolated))
            atexit.register(_server.close)
            return None
        if _server.backend != backend or not _server.ready():
            return None
        try:
            return _server.fork([str(backend), str(port)], env, log_path)
        except (OSError, ValueError, LookupError) as error:
            _disable(f"asking the preload server failed: {error!r}")
            return None
