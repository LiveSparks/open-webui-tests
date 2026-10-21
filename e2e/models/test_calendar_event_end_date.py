"""A calendar event's end kept its old date when the event was moved to another day, #31302.

Fix commit `6fdbe3ab6` (open-webui/open-webui#31303). The event editor has one date field, for
the start. Saving combined the end time with the end's old date, and only an end that landed
before the new start was repaired (e2e/models/test_calendar_moves_and_recurrence.py pins that
case). So an event moved to an earlier day ended on its old day and spanned the days between,
and a multi-day event moved later by less than its length lost days. The end now moves by
as many days as the start did.

Discriminates: passes on dev efe63bd34; with 6fdbe3ab6 reverted the event moved earlier still
ends a week later and the multi-day event ends a day early; the time-only edit passes on both.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from playwright.sync_api import Page

from harness.actors import Actor
from harness.calendar_api import NO_REMINDER, create_event, default_calendar_id, to_ns

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


def _this_month(day: int, hour: int) -> dt.datetime:
    today = dt.date.today()
    return dt.datetime(today.year, today.month, day, hour, 0).astimezone()


def _event(owner: Actor, start: dt.datetime, end: dt.datetime) -> tuple[str, str]:
    title = f"Review {uuid.uuid4().hex[:6]}"
    with owner.client() as client:
        created = create_event(
            client,
            default_calendar_id(client),
            title=title,
            start_at=to_ns(start),
            end_at=to_ns(end),
            meta=NO_REMINDER,
        )
    assert created.status_code == 200, created.text
    return created.json()["id"], title


def _stored_span(owner: Actor, event_id: str) -> tuple[int, int]:
    with owner.client() as client:
        stored = client.get(f"/api/v1/calendars/events/{event_id}").json()
    return stored["start_at"], stored["end_at"]


def _edit(page: Page, title: str, *, date: dt.date | None = None, end_time: str | None = None):
    page.goto("/calendar")
    page.get_by_role("button", name=title).last.click()
    if date:
        page.locator('input[type="date"]').fill(date.isoformat())
    if end_time:
        page.locator('input[type="time"]').last.fill(end_time)
    with page.expect_response(lambda response: "/update" in response.url):
        page.get_by_role("button", name="Save").click()


def test_an_event_moved_to_an_earlier_day_ends_on_that_day(page_for, make_user):
    owner = make_user()
    event_id, title = _event(owner, _this_month(8, 10), _this_month(8, 11))

    _edit(page_for(owner), title, date=_this_month(1, 10).date())

    assert _stored_span(owner, event_id) == (
        to_ns(_this_month(1, 10)),
        to_ns(_this_month(1, 11)),
    ), "the end stayed on the old date, so the event spans a week (#31302)"


def test_a_multi_day_event_moved_a_day_later_keeps_its_length(page_for, make_user):
    owner = make_user()
    event_id, title = _event(owner, _this_month(3, 10), _this_month(5, 12))

    _edit(page_for(owner), title, date=_this_month(4, 10).date())

    assert _stored_span(owner, event_id) == (
        to_ns(_this_month(4, 10)),
        to_ns(_this_month(6, 12)),
    ), "the end kept its old date, so the event lost a day (#31302)"


# ---------------------------------------------------------------- nearby


def test_changing_only_the_end_time_keeps_the_day(page_for, make_user):
    owner = make_user()
    event_id, title = _event(owner, _this_month(9, 10), _this_month(9, 11))

    _edit(page_for(owner), title, end_time="12:30")

    assert _stored_span(owner, event_id) == (
        to_ns(_this_month(9, 10)),
        to_ns(_this_month(9, 12).replace(minute=30)),
    )
