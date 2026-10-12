"""A browser-served instance with Apps and Channels on, and the reference app it talks to.

The shared instance has no allowance for a local plain-HTTP app, which only an environment
setting gives, so this package boots one of its own.
"""

from __future__ import annotations

from typing import Callable, Generator

import pytest

from harness.actors import Actor, admin_of, create_user
from harness.apps import APPS_ENV, ReferenceApp, installed, serving_reference_app
from harness.instance import LaunchedInstance


@pytest.fixture(scope="package")
def apps_instance(package_instance_with) -> LaunchedInstance:
    launched = package_instance_with(APPS_ENV)
    if not launched.serves_frontend:
        pytest.skip(
            "no built frontend (run `npm run build` in the checkout or set OPEN_WEBUI_BUILD_DIR)"
        )
    return launched


@pytest.fixture(scope="package")
def apps_admin(apps_instance: LaunchedInstance) -> Actor:
    return admin_of(apps_instance)


@pytest.fixture
def make_apps_user(apps_instance: LaunchedInstance) -> Callable[..., Actor]:
    def factory(role: str = "user") -> Actor:
        return create_user(apps_instance, role=role)

    return factory


@pytest.fixture(scope="package")
def reference_app(apps_instance: LaunchedInstance) -> Generator[ReferenceApp, None, None]:
    with serving_reference_app() as app:
        yield app


@pytest.fixture
def installed_app(apps_admin: Actor, reference_app: ReferenceApp) -> dict:
    with apps_admin.client() as client:
        return installed(client, reference_app)
