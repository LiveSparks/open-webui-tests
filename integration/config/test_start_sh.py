"""Guard: backend/start.sh, the image's entry point, brings ordinary containers up.

Regression, open-webui#24560: commit 070ab2650 put start.sh under `set -euo pipefail` and tested
`${WEB_LOADER_ENGINE,,}` without a default, so every container that did not set
WEB_LOADER_ENGINE (most of them) died with "WEB_LOADER_ENGINE: unbound variable" before a line of
Python ran. When start.sh exits early there is no stack trace, no HTTP error and no app log, the
container just dies, so the boot paths around it are pinned too: the secret key it generates or
reuses (sessions survive a restart only if it is reused), the key length check, the host, port and
worker defaults and the arguments that replace them.

Each boot runs the real, unmodified script from a scratch directory laid out like the image's
`/app/backend`: the script next to the checkout's `open_webui` package, `python3` on PATH being
this interpreter. The default host and port are checked inside a network namespace of their own
(`unshare -rn`), where port 8080 on every interface is free by construction.

Discriminates: passes on bbfa876af, fails with WEB_LOADER_ENGINE dropped from the defaulting line
(every container that leaves it unset exits with "unbound variable" and never answers); generating
the key file on every start fails the restart test.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
import jwt
import pytest

from harness.instance import free_port, isolated_env, resolve_backend

pytestmark = [pytest.mark.api, pytest.mark.requires_source, pytest.mark.slow]

ACCOUNT = {"name": "Admin", "email": "admin@example.com", "password": "adminpassword123"}
KEY_FILE = ".webui_secret_key"
# What a test runner's own environment could inject; a container sets only what it sets.
STRIPPED_ENV = (
    "WEBUI_SECRET_KEY",
    "WEBUI_JWT_SECRET_KEY",
    "WEBUI_SECRET_KEY_FILE",
    "WEBUI_SECRET_KEY_LENGTH",
    "WEB_LOADER_ENGINE",
    "HOST",
    "PORT",
    "UVICORN_WORKERS",
    "SPACE_ID",
    "USE_OLLAMA_DOCKER",
    "USE_CUDA_DOCKER",
)
BOOT_SECONDS = 300
ENV_KEY = "a-key-from-the-environment-0123456789"

# Runs inside `unshare -rn`: loopback up, the script started, its listening sockets read back.
NAMESPACE_DRIVER = """
import json, subprocess, sys, time, urllib.request

workdir, log_path = sys.argv[1], sys.argv[2]
subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
with open(log_path, "w") as log:
    server = subprocess.Popen(["bash", "start.sh"], cwd=workdir, stdout=log, stderr=log)
healthy = False
deadline = time.monotonic() + 240
while time.monotonic() < deadline and server.poll() is None and not healthy:
    try:
        healthy = urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3).status == 200
    except OSError:
        time.sleep(0.5)
listening = [
    line.split()[1]
    for line in open("/proc/net/tcp").read().splitlines()[1:]
    if line.split()[3] == "0A"
]
server.terminate()
server.wait(timeout=30)
print(json.dumps({"healthy": healthy, "listening": listening}))
"""


@dataclass
class Container:
    """A scratch `/app/backend`: start.sh, the checkout's package and the data it keeps."""

    workdir: Path
    env: dict[str, str]

    def key_file(self, name: str = KEY_FILE) -> Path:
        return self.workdir / name


@dataclass
class Boot:
    process: subprocess.Popen
    base_url: str
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def client(self, token: str | None = None) -> httpx.Client:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.base_url, headers=headers, timeout=60.0)


@pytest.fixture
def container(tmp_path: Path) -> Container:
    if shutil.which("bash") is None:
        pytest.skip("start.sh needs bash")
    backend = resolve_backend()
    if backend is None:
        pytest.skip("open-webui backend source not found (set OPEN_WEBUI_SOURCE_DIR)")
    start_sh = backend / "start.sh"
    assert start_sh.is_file(), f"no start.sh at {start_sh}; retarget at the image's entry point"

    workdir = tmp_path / "backend"
    bin_dir = tmp_path / "bin"
    for directory in (workdir, bin_dir, tmp_path / "data", tmp_path / "static"):
        directory.mkdir()
    shutil.copy(start_sh, workdir / "start.sh")
    (workdir / "open_webui").symlink_to(backend / "open_webui", target_is_directory=True)
    # a wrapper, not a symlink: the interpreter finds its virtualenv from its own path
    python3 = bin_dir / "python3"
    python3.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python3.chmod(0o755)

    env = isolated_env(
        {
            "DATA_DIR": str(tmp_path / "data"),
            "STATIC_DIR": str(tmp_path / "static"),
            "FRONTEND_BUILD_DIR": str(tmp_path / "build"),
            "OFFLINE_MODE": "true",
            "ENABLE_OLLAMA_API": "false",
            "ENABLE_OPENAI_API": "false",
            "PYTHONUNBUFFERED": "1",
        }
    )
    for name in STRIPPED_ENV:
        env.pop(name, None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return Container(workdir, env)


def _run_start_sh(container: Container, env: dict[str, str], *arguments: str):
    """The script exited before serving: its exit code and output."""
    return subprocess.run(
        ["bash", "start.sh", *arguments],
        cwd=container.workdir,
        env={**container.env, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _answers_health(base_url: str) -> bool:
    try:
        return httpx.get(f"{base_url}/health", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


@contextmanager
def booted(
    container: Container, env: dict[str, str] | None = None, *arguments: str
) -> Iterator[Boot]:
    """The script started with `env` and `arguments`, until `/health` answers and the block ends."""
    port = free_port()
    settings = {"HOST": "127.0.0.1", "PORT": str(port), **(env or {})}
    log_path = container.workdir.parent / f"start-{port}.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            ["bash", "start.sh", *arguments],
            cwd=container.workdir,
            env={**container.env, **settings},
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    boot = Boot(process, f"http://{settings['HOST']}:{port}", log_path)
    try:
        deadline = time.monotonic() + BOOT_SECONDS
        while not _answers_health(boot.base_url):
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(
                    f"start.sh never brought the server up (exit {process.poll()}):\n"
                    f"{boot.log()[-4000:]}"
                )
            time.sleep(0.5)
        yield boot
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


def _sign_up(boot: Boot) -> str:
    with boot.client() as client:
        signed_up = client.post("/api/v1/auths/signup", json=ACCOUNT)
    assert signed_up.status_code == 200, signed_up.text
    return signed_up.json()["token"]


def _signed_with(token: str, key: str) -> bool:
    try:
        jwt.decode(token, key, algorithms=["HS256"])
    except jwt.InvalidSignatureError:
        return False
    return True


def _launch_arguments(pid: int) -> list[str]:
    """The server's command line, as `ps` shows it to an operator."""
    return Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]


def _worker_processes(pid: int) -> list[int]:
    workers = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            parent = int(stat.read_text().rsplit(") ", 1)[1].split()[1])
            command = (stat.parent / "cmdline").read_bytes()
        except OSError:
            continue
        if parent == pid and b"spawn_main" in command:
            workers.append(int(stat.parent.name))
    return workers


def _eventually(read, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    value = read()
    while not value and time.monotonic() < deadline:
        time.sleep(0.5)
        value = read()
    return value


# regression: an unset WEB_LOADER_ENGINE killed the container (#24560)


@pytest.mark.regression
def test_an_unset_web_loader_engine_still_brings_the_server_up(container):
    with booted(container) as boot:
        log = boot.log()

    assert "unbound variable" not in log, (
        "start.sh reads an optional variable without a default under `set -u`, which kills "
        f"every container that does not set it (#24560): {log[-2000:]}"
    )


# secret key


def test_a_generated_key_signs_sessions_and_survives_a_restart(container):
    with booted(container) as boot:
        token = _sign_up(boot)
    generated = container.key_file().read_text(encoding="utf-8").strip()
    assert generated, "start.sh wrote an empty key file"
    assert _signed_with(token, generated), "the server does not sign sessions with the key file"

    with booted(container) as restarted, restarted.client(token) as client:
        session = client.get("/api/v1/auths/")

    assert container.key_file().read_text(encoding="utf-8").strip() == generated, (
        "the restart replaced the key file, which signs every user out"
    )
    assert session.status_code == 200, f"a session did not survive a restart: {session.text}"


def test_a_key_in_the_environment_skips_the_file(container):
    with booted(container, {"WEBUI_SECRET_KEY": ENV_KEY}) as boot:
        token = _sign_up(boot)

    assert _signed_with(token, ENV_KEY)
    assert not container.key_file().exists(), "start.sh wrote a key file although the env set one"


def test_the_key_file_path_may_contain_spaces(container):
    """A bind-mounted secret under a path like /run/my secrets/key."""
    with booted(container, {"WEBUI_SECRET_KEY_FILE": "my secret key"}) as boot:
        token = _sign_up(boot)

    assert _signed_with(token, container.key_file("my secret key").read_text().strip())


@pytest.mark.parametrize("length", ["abc", "0", "-4", " ", "12.5"])
def test_a_nonsense_key_length_stops_the_container_with_a_named_error(container, length):
    """Better a named error than a zero-length key signing every JWT the instance issues."""
    stopped = _run_start_sh(container, {"WEBUI_SECRET_KEY_LENGTH": length})

    assert stopped.returncode != 0, f"start.sh accepted WEBUI_SECRET_KEY_LENGTH={length!r}"
    assert "positive integer" in stopped.stderr, stopped.stderr
    assert not container.key_file().exists()


def test_an_empty_key_length_falls_back_to_the_default(container):
    """A compose variable that never got a value arrives set but blank."""
    with booted(container, {"WEBUI_SECRET_KEY_LENGTH": ""}) as boot:
        token = _sign_up(boot)

    generated = container.key_file().read_text(encoding="utf-8").strip()
    assert generated, "start.sh wrote an empty key file"
    assert _signed_with(token, generated)


# host, port and uvicorn arguments


def test_host_and_port_default_to_every_interface_on_8080(container, tmp_path):
    if shutil.which("unshare") is None or shutil.which("ip") is None:
        pytest.skip("the default port is checked in a network namespace: needs unshare and ip")
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True)
    if probe.returncode != 0:
        pytest.skip(f"unprivileged network namespaces are unavailable: {probe.stderr.strip()}")
    driver = tmp_path / "namespace_driver.py"
    driver.write_text(NAMESPACE_DRIVER, encoding="utf-8")
    log_path = tmp_path / "namespace.log"

    ran = subprocess.run(
        ["unshare", "-rn", sys.executable, str(driver), str(container.workdir), str(log_path)],
        env={**container.env, "WEBUI_SECRET_KEY": "x"},
        capture_output=True,
        text=True,
        timeout=BOOT_SECONDS,
    )

    assert ran.returncode == 0, ran.stderr[-2000:]
    seen = json.loads(ran.stdout.strip().splitlines()[-1])
    assert seen["healthy"], f"nothing answered on port 8080:\n{log_path.read_text()[-3000:]}"
    assert "00000000:1F90" in seen["listening"], (
        f"the server does not listen on 0.0.0.0:8080: {seen['listening']}"
    )


def test_host_and_port_honour_the_environment(container):
    with booted(container, {"WEBUI_SECRET_KEY": "x"}) as boot:
        assert boot.base_url.startswith("http://127.0.0.1:")
        assert _answers_health(boot.base_url)


def test_the_default_launch_runs_the_configured_worker_count(container):
    # each worker migrates on import; the docs have one process migrate before several start
    with booted(container, {"WEBUI_SECRET_KEY": "x"}):
        pass
    workers_env = {"WEBUI_SECRET_KEY": "x", "UVICORN_WORKERS": "2", "ENABLE_DB_MIGRATIONS": "false"}
    with booted(container, workers_env) as boot:
        workers = _eventually(lambda: len(_worker_processes(boot.process.pid)) == 2)
        launched_with = _launch_arguments(boot.process.pid)

    assert "--workers" in launched_with, launched_with
    assert launched_with[launched_with.index("--workers") + 1] == "2"
    assert workers, "UVICORN_WORKERS=2 did not start two worker processes"


def test_arguments_given_to_the_script_replace_the_defaults(container):
    """`docker run ... start.sh --reload` must not also get --workers."""
    env = {"WEBUI_SECRET_KEY": "x"}
    with booted(container, env, "--header", "X-Launched-By:start-sh") as boot:
        launched_with = _launch_arguments(boot.process.pid)
        answered = httpx.get(f"{boot.base_url}/health", timeout=10.0)

    assert answered.headers.get("x-launched-by") == "start-sh", "the script's arguments were lost"
    assert launched_with[-2:] == ["--header", "X-Launched-By:start-sh"]
    assert "--workers" not in launched_with, (
        f"the script's arguments came with the default --workers as well: {launched_with}"
    )
