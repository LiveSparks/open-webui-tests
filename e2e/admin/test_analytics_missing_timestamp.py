"""Regression: the Daily Messages chart vanished for All time once a message had no timestamp.

Fix f25708484 (PR #31888, issue #27316). A message row stored without a time failed the Daily
Messages request for All time with a 500, so the dashboard showed its other figures and no chart.
Such a row now counts on today.

One fresh user has a reply from ten days ago and one whose time is gone; the dashboard narrowed
to that user's group shows neither for the last 7 days and both for All time, with the chart
in daily buckets and a bar of one message ten days ago.

Twin of integration/models/test_analytics_missing_timestamp.py.

Discriminates: passes on dev 21f770fb7, fails with f25708484 reverted (the dashboard cannot load
All time and shows no figures).
"""

from __future__ import annotations

import re
import time
from datetime import date

import pytest
from playwright.sync_api import expect

from harness.access import make_group
from harness.backends import write_rows
from harness.chat_history import seed_chat
from utils.analytics import (
    chart_interval,
    chart_point,
    count_by,
    dashboard,
    day_label,
    period_picker,
    summary_figure,
)

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

DAY = 24 * 3600


def seed_reply(client, timestamp: int) -> str:
    chat_id, _ = seed_chat(
        client,
        [
            {"role": "user", "content": "a question", "timestamp": timestamp},
            {"role": "assistant", "content": "an answer", "timestamp": timestamp},
        ],
    )
    return chat_id


def test_all_time_loads_with_a_message_stored_without_a_time(instance, admin, make_user, page_for):
    owner = make_user()
    group_id = make_group(admin, [owner])
    ten_days_ago = int(time.time()) - 10 * DAY
    with owner.client() as client:
        seed_reply(client, ten_days_ago)
        untimed_chat = seed_reply(client, int(time.time()))
    write_rows(
        instance,
        "UPDATE chat_message SET created_at = NULL WHERE chat_id = :chat_id",
        [{"chat_id": untimed_chat}],
    )
    with admin.client() as client:
        group_name = client.get(f"/api/v1/groups/id/{group_id}").json()["name"]

    page = page_for(make_user(role="admin"), path="/admin/analytics")
    settings = dashboard(page, group_name, "Last 7 days")
    expect(summary_figure(settings, "Messages")).to_have_text("0")

    period_picker(settings).select_option(label="All time")

    expect(summary_figure(settings, "Messages")).to_have_text("2")
    expect(chart_interval(settings)).to_have_text("Daily")
    count_by(settings, "Messages")
    first_day = date.fromtimestamp(ten_days_ago)
    expect(chart_point(settings, day_label(first_day))).to_have_attribute(
        "aria-label", re.compile(r": 1 Messages\.")
    )
