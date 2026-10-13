"""Regression: Open WebUI reaches Redis through Sentinels that require a password.

Issue open-webui/open-webui#32240 (first reported as #19401). With `REDIS_SENTINEL_HOSTS` set, the
host name in `REDIS_URL` is the Sentinel service name and its password the one Redis asks for.
Sentinels set up with `requirepass` ask for the same password, but Open WebUI gave it only to the
master connection and asked the Sentinels for the master without it, so every lookup failed with
an authentication error and Open WebUI never reached Redis.

The master and the Sentinel here both require the password. The instance boots in the test body,
so a boot that fails on Redis fails the test. Signing out writes the revoked token to Redis, and
the next request with that token is refused only when the instance can read it back.

Issue #31918 (fix 0309c9ad1, PR #32234): Sentinel service names are case-sensitive, but Open WebUI
lowercased the one in `REDIS_URL`, so a master named `myMaster` was asked for as `mymaster` and
every Redis call failed with "No master found". The Sentinel here watches `myMaster` for that
reason; the lowercase name stays covered in test_redis_sentinel_failover.py.

Discriminates: fails on dev 0309c9ad1 (signing out answers 500, Redis is never reached), and
passes on that dev with the password from `REDIS_URL` also given to the Sentinels. Passes on dev
5032e834e and fails with 0309c9ad1 reverted (signing out answers 500).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest
import redis

from harness.actors import create_user
from harness.instance import free_port

pytestmark = [
    pytest.mark.regression,
    pytest.mark.api,
    pytest.mark.requires_source,
    pytest.mark.slow,
]

SERVICE = "myMaster"
PASSWORD = "harbour-pass"


def _wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.25)
    return condition()


def _answers(port: int) -> bool:
    client = redis.Redis(port=port, password=PASSWORD, socket_connect_timeout=1)
    try:
        return bool(client.ping())
    except redis.exceptions.RedisError:
        return False
    finally:
        client.close()


def _start(command: list[str], port: int) -> subprocess.Popen:
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if not _wait_until(lambda: _answers(port), timeout=10):
        process.terminate()
        pytest.fail(f"{command[0]} did not start on port {port}")
    return process


@pytest.fixture(scope="module")
def protected_sentinel() -> Iterator[tuple[int, int]]:
    """A master and a Sentinel watching it, both requiring `PASSWORD`; yields their ports."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("needs a redis-server binary on PATH")
    workdir = Path(tempfile.mkdtemp(prefix="owui-sentinel-auth-"))
    master_port, sentinel_port = free_port(), free_port()
    config = workdir / "sentinel.conf"
    config.write_text(
        f"port {sentinel_port}\nbind 127.0.0.1\ndir {workdir}\nrequirepass {PASSWORD}\n"
        f"sentinel monitor {SERVICE} 127.0.0.1 {master_port} 1\n"
        f"sentinel auth-pass {SERVICE} {PASSWORD}\n"
        f"sentinel down-after-milliseconds {SERVICE} 600000\n"
    )
    master = _start(
        [
            binary,
            "--port",
            str(master_port),
            "--bind",
            "127.0.0.1",
            "--save",
            "",
            "--dir",
            str(workdir),
            "--requirepass",
            PASSWORD,
        ],
        master_port,
    )
    sentinel = _start([binary, str(config), "--sentinel"], sentinel_port)
    try:
        yield master_port, sentinel_port
    finally:
        for process in (sentinel, master):
            process.terminate()
            process.wait(timeout=10)
        shutil.rmtree(workdir, ignore_errors=True)


def test_a_signed_out_token_is_refused_through_password_protected_sentinels(
    instance_with, protected_sentinel
):
    master_port, sentinel_port = protected_sentinel
    instance = instance_with(
        {
            "REDIS_URL": f"redis://:{PASSWORD}@{SERVICE}:6379/0",
            "REDIS_SENTINEL_HOSTS": "127.0.0.1",
            "REDIS_SENTINEL_PORT": str(sentinel_port),
        }
    )
    account = create_user(instance)
    with account.client() as client:
        signed_out = client.post("/api/v1/auths/signout")
    assert signed_out.status_code == 200, (
        f"signing out failed ({signed_out.status_code}): the instance could not reach Redis "
        "through the password-protected Sentinel"
    )

    with instance.client(account.token) as client:
        assert client.get("/api/v1/auths/").status_code == 401, (
            "the signed-out token was accepted again: the revocation never reached Redis "
            "through the password-protected Sentinel"
        )
    master = redis.Redis(port=master_port, password=PASSWORD)
    try:
        assert master.dbsize() > 0, "Open WebUI wrote nothing to the master"
    finally:
        master.close()
