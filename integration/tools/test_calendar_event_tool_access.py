"""Regression: the chat tools that change or delete a calendar event skipped the access check.

Fix `52533c567` (#31537) makes `update_calendar_event` and `delete_calendar_event` check the event's
calendar the way the calendar API does: its owner and admins pass, anyone else needs a write grant
on the calendar. Before, whoever had created the event passed without any grant (a writer whose
access was withdrawn kept editing and deleting their old events), and the calendar's owner was
asked for a grant like everyone else (so they could not change an event a writer created).

Discriminates: passes on dev a5bc78300, fails with the fix reverted (a writer whose grant was
withdrawn still changes and deletes the event they created, and the owner is refused on an event
a writer created).
"""

from __future__ import annotations

import json

import pytest

from harness.access import grant
from harness.calendar_api import create_event
from harness.tool_calls import run_tool

pytestmark = [pytest.mark.regression, pytest.mark.api, pytest.mark.requires_source]

START_NS = 1_900_000_000 * 1_000_000_000  # a fixed day in 2030


def _write_grants(member) -> list[dict]:
    return [grant("user", member.id, "read"), grant("user", member.id, "write")]


def _call(actor, upstream, tool: str, **arguments):
    """The result the builtin tool returned when the model called it for `actor`."""
    with actor.client() as client:
        result = run_tool(client, upstream, tool, arguments)
    return json.loads(result)


def _shared_calendar(owner, writer) -> str:
    with owner.client() as client:
        created = client.post(
            "/api/v1/calendars/create",
            json={"name": "Team", "access_grants": _write_grants(writer)},
        )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _event_by(author, calendar_id: str) -> str:
    with author.client() as client:
        created = create_event(client, calendar_id, title="Planning", start_at=START_NS)
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _withdraw_grants(owner, calendar_id: str) -> None:
    with owner.client() as client:
        updated = client.post(
            f"/api/v1/calendars/{calendar_id}/access/update", json={"access_grants": []}
        )
    assert updated.status_code == 200, updated.text


def _stored_event(owner, event_id: str):
    with owner.client() as client:
        return client.get(f"/api/v1/calendars/events/{event_id}")


@pytest.fixture
def team(make_user):
    """(owner, writer, id of the owner's calendar shared with the writer)."""
    owner, writer = make_user(), make_user()
    return owner, writer, _shared_calendar(owner, writer)


def test_a_former_writer_cannot_change_the_event_they_created(team, upstream):
    owner, writer, calendar_id = team
    event_id = _event_by(writer, calendar_id)
    _withdraw_grants(owner, calendar_id)

    refused = _call(writer, upstream, "update_calendar_event", event_id=event_id, title="Hijacked")

    assert refused == {"error": "Access denied"}, refused
    assert _stored_event(owner, event_id).json()["title"] == "Planning"


def test_a_former_writer_cannot_delete_the_event_they_created(team, upstream):
    owner, writer, calendar_id = team
    event_id = _event_by(writer, calendar_id)
    _withdraw_grants(owner, calendar_id)

    refused = _call(writer, upstream, "delete_calendar_event", event_id=event_id)

    assert refused == {"error": "Access denied"}, refused
    assert _stored_event(owner, event_id).status_code == 200


def test_the_calendars_owner_changes_an_event_a_writer_created(team, upstream):
    owner, writer, calendar_id = team
    event_id = _event_by(writer, calendar_id)

    updated = _call(owner, upstream, "update_calendar_event", event_id=event_id, title="Reviewed")

    assert "error" not in updated, updated
    assert _stored_event(owner, event_id).json()["title"] == "Reviewed"


def test_the_calendars_owner_deletes_an_event_a_writer_created(team, upstream):
    owner, writer, calendar_id = team
    event_id = _event_by(writer, calendar_id)

    deleted = _call(owner, upstream, "delete_calendar_event", event_id=event_id)

    assert deleted["status"] == "success", deleted
    assert _stored_event(owner, event_id).status_code == 404


def test_a_current_writer_changes_and_deletes_an_event_the_owner_created(team, upstream):
    owner, writer, calendar_id = team
    event_id = _event_by(owner, calendar_id)

    updated = _call(writer, upstream, "update_calendar_event", event_id=event_id, title="Moved")
    title_after_update = _stored_event(owner, event_id).json()["title"]
    deleted = _call(writer, upstream, "delete_calendar_event", event_id=event_id)

    assert "error" not in updated, updated
    assert title_after_update == "Moved"
    assert deleted["status"] == "success", deleted
    assert _stored_event(owner, event_id).status_code == 404


def test_a_reader_cannot_change_or_delete_an_event_in_the_shared_calendar(
    team, make_user, upstream
):
    owner, _, calendar_id = team
    reader = make_user()
    with owner.client() as client:
        client.post(
            f"/api/v1/calendars/{calendar_id}/access/update",
            json={"access_grants": [grant("user", reader.id, "read")]},
        ).raise_for_status()
    event_id = _event_by(owner, calendar_id)

    updated = _call(reader, upstream, "update_calendar_event", event_id=event_id, title="x")
    deleted = _call(reader, upstream, "delete_calendar_event", event_id=event_id)

    assert updated == deleted == {"error": "Access denied"}
    assert _stored_event(owner, event_id).json()["title"] == "Planning"
