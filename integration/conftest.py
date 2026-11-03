"""Fixtures for the httpx tests and for the ones that need a degraded instance.

The shared scratch instances, the scripted provider and the account fixtures live in
`harness/fixtures.py`, loaded for the whole suite from the root conftest. `degraded_instance`
is a further one on a fake Redis and a vector DB that refuses every connection.
"""

from __future__ import annotations

from typing import Generator

import httpx
import pytest

from harness import reset as shared_boot
from harness.actors import Actor
from harness.instance import LaunchedInstance, launch
from harness.upstream import MockUpstream
from integration.fake_redis import FakeRedis

# Modules that keep cached state on the session instance across function items go here with
# the reason, or mark themselves `shared_boot_off`; both only carry weight under
# `OWUI_SHARED_BOOT=1`, where the autouse reset below wipes state between items.
OPT_OUT = (
    # full_folder caches over 200 chats for the module
    "integration.models.test_chat_search_and_folder_paging",
)


def pytest_configure(config: pytest.Config) -> None:
    shared_boot.register_opt_out_marker(config)


@pytest.fixture(autouse=True)
def shared_boot_reset(request: pytest.FixtureRequest) -> Generator[None]:
    """The shared-boot mode's explicit reset between function items.

    Under `OWUI_SHARED_BOOT=1`, an item whose fixture closure reaches the session instance
    gets per-test state wiped through the server's own admin surface afterwards and the
    module's config snapshot restored - `harness/reset.py` carries the exact contract and
    the endpoints. A module without that reachable instance, on the opt-out list or marked
    `shared_boot_off` is passed through untouched; mode-off never reaches this body.
    """
    instance = shared_boot.target(request, OPT_OUT)
    if instance is None:
        yield
        return
    snapshot = shared_boot.snapshot_for(request.node.module.__name__, instance)
    yield
    shared_boot.run_after_item(instance, snapshot)


@pytest.fixture
def api_client(user: Actor) -> Generator[httpx.Client, None, None]:
    """The regular account's client on the shared instance."""
    with user.client() as client:
        yield client


@pytest.fixture(scope="session")
def fake_redis() -> Generator[FakeRedis, None, None]:
    redis = FakeRedis()
    yield redis
    redis.close()


@pytest.fixture(scope="session")
def degraded_instance(
    mock_upstream: MockUpstream, fake_redis: FakeRedis
) -> Generator[LaunchedInstance, None, None]:
    """A scratch instance on the fake Redis and a vector DB that refuses every connection."""
    yield from launch(
        mock_upstream,
        {"REDIS_URL": fake_redis.url, "VECTOR_DB": "qdrant", "QDRANT_URI": "http://127.0.0.1:9"},
    )
