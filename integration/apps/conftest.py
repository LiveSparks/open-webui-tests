"""One instance with Apps and Channels on, and the reference app it talks to, for this package.

Every test installs the app afresh (`installed_app`), so each gets its own installation id; the
app's record file is shared, so tests read it filtered by `app_id`.
"""

from __future__ import annotations

from typing import Callable, Generator

import pytest

from harness.actors import Actor, admin_of, create_user
from harness.apps import APPS_ENV, ReferenceApp, installed, serving_reference_app
from harness.instance import LaunchedInstance


@pytest.fixture(scope="package")
def apps_instance(package_instance_with) -> LaunchedInstance:
    return package_instance_with(APPS_ENV)


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
    with serving_reference_app([apps_instance.base_url]) as app:
        yield app


@pytest.fixture
def installed_app(apps_admin: Actor, reference_app: ReferenceApp) -> dict:
    """A fresh installation of the reference app with every scope, usable by everyone."""
    with apps_admin.client() as client:
        return installed(client, reference_app)
