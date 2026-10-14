"""Journey: an app that accepts a mention only once it has stored it.

With oix's `background=False`, the app confirms receipt only after its handler has saved the
mention, inside an eight-second budget; Open WebUI waits up to ten seconds for that confirmation.
A handler that fails to store it makes the delivery fail, and one that outlasts the budget leaves
the outcome unknown; the app's error reply to the mention tells the channel which of the two
happened, in the app's own words plus a warning that an unconfirmed delivery may have landed.

Discriminates: passes on dev c7193760b with oix 0.8. Mutation `app_channel_reply_handler` treating
a 504 like any other failure turns test_a_mention_the_app_cannot_store_in_time_is_unconfirmed red.
Retargeted for dev b704d0613, where the reason is posted as the app's reply; the same mutation
(dropping the unconfirmed warning) turns it red there.
"""

from __future__ import annotations

import time

import pytest

from harness.apps import (
    installed,
    invite,
    mention_text,
    new_channel,
    post,
    serving_reference_app,
    wait_for_message,
)
from harness.socket_client import connected

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]


@pytest.fixture(scope="module")
def admitting_app(apps_instance):
    with serving_reference_app(name="Admitting App", admission=True) as app:
        yield app


@pytest.fixture
def setup(apps_admin, admitting_app):
    with apps_admin.client() as client:
        app = installed(client, admitting_app)
        channel_id = new_channel(client)
        invite(client, channel_id, app["id"])
    return app, channel_id


def _mention(actor, channel_id, app, text):
    with actor.client() as client:
        return post(client, channel_id, mention_text(app, text))


def _error_reply(actor, channel_id, app, mention, detail, timeout=20.0) -> dict:
    with actor.client() as client:
        content = f"App error:\n\n```text\n{detail}\n```"
        error = wait_for_message(client, channel_id, content, timeout=timeout)
    assert error["user"]["id"] == app["id"]
    assert error["reply_to_id"] == mention["id"]
    return error


def test_a_stored_mention_is_accepted_silently(apps_admin, admitting_app, setup):
    app, channel_id = setup
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel_id, app, "remember this")
        stored = admitting_app.wait_for("admitted", app_id=app["id"], channel_id=channel_id)
        time.sleep(1)
        assert [e for e in socket.app_events if e["message_id"] == posted["id"]] == []
    assert stored["data"]["message_id"] == posted["id"]


def test_a_mention_the_app_cannot_store_fails_delivery(apps_admin, setup):
    app, channel_id = setup
    with connected(apps_admin) as socket:
        posted = _mention(apps_admin, channel_id, app, "refuse")
        _error_reply(apps_admin, channel_id, app, posted, "App handler failed")
        time.sleep(1)
        assert [e for e in socket.app_events if e["message_id"] == posted["id"]] == []


def test_a_mention_the_app_cannot_store_in_time_is_unconfirmed(apps_admin, admitting_app, setup):
    app, channel_id = setup
    started = time.monotonic()
    posted = _mention(apps_admin, channel_id, app, "stall")
    detail = (
        "Event acceptance timed out; outcome may be unknown\n"
        "Delivery was not confirmed. The app may have accepted the message; check before retrying."
    )
    _error_reply(apps_admin, channel_id, app, posted, detail, timeout=30)
    waited = time.monotonic() - started
    assert waited < 12, "the app gave up at its own eight-second budget"
    assert admitting_app.records("admitted", app_id=app["id"]) == []
