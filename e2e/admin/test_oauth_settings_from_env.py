"""Regression: the admin panel showed OAuth sign-up off although the environment turned it on.

open-webui fix `e398ba350` (#26928). A boot with `ENABLE_OAUTH_PERSISTENT_CONFIG` off stored
every SSO setting anyway, and once the operator turned persistence on those stale rows won over
the environment, so the authentication page kept showing (and the server kept using) the old
value. The checkout is booted once with persistence off, then again with it on and OAuth sign-up
switched on in the environment, and the admin opens the authentication settings.

Twin of integration/auth/test_oauth_settings_from_env.py.

Discriminates: passes on dev ef67cc3fa, fails with the persistence check removed from
`Config.seed_defaults` (the OAuth Signup switch shows off).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from harness.actors import admin_of
from harness.prepared_data import booted_again

pytestmark = [
    pytest.mark.regression,
    pytest.mark.requires_browser,
    pytest.mark.requires_source,
    pytest.mark.slow,
]


def oauth_signup_switch(page_for, launched):
    if not launched.serves_frontend:
        pytest.skip("no built frontend (set OPEN_WEBUI_BUILD_DIR)")
    page = page_for(admin_of(launched), path="/admin/settings/authentication")
    return page.get_by_role("switch", name="OAuth Signup")


def test_the_panel_shows_oauth_signup_from_the_environment(page_for, instance_with, tmp_path):
    launched = booted_again(
        instance_with,
        tmp_path / "data",
        first={"ENABLE_OAUTH_PERSISTENT_CONFIG": "false", "ENABLE_OAUTH_SIGNUP": "false"},
        second={"ENABLE_OAUTH_PERSISTENT_CONFIG": "true", "ENABLE_OAUTH_SIGNUP": "true"},
    )

    switch = oauth_signup_switch(page_for, launched)

    expect(switch).to_be_enabled()
    expect(switch).to_be_checked()


def test_while_persistence_is_off_the_panel_shows_the_environment_read_only(
    page_for, instance_with, tmp_path
):
    """Nearby: with persistence off the switch mirrors the environment and cannot be changed."""
    launched = booted_again(
        instance_with,
        tmp_path / "data",
        first={"ENABLE_OAUTH_PERSISTENT_CONFIG": "false", "ENABLE_OAUTH_SIGNUP": "false"},
        second={"ENABLE_OAUTH_PERSISTENT_CONFIG": "false", "ENABLE_OAUTH_SIGNUP": "true"},
    )

    switch = oauth_signup_switch(page_for, launched)

    expect(switch).to_be_checked()
    expect(switch).to_be_disabled()
