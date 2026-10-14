"""The analytics tab of the admin settings could not be scrolled, #30428.

Fix commit `d66e5dc40` (open-webui/open-webui#30429). The analytics dashboard renders inside the
settings modal, whose tab area clips what overflows it. The other tabs scroll inside their own
area; the analytics one had none, so the lower rows of the model and user tables were cut off
and the wheel moved nothing. It scrolls like the other tabs now.

Discriminates: passes on dev efe63bd34; with d66e5dc40 reverted the wheel leaves the end of the
dashboard out of view.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]


def test_the_wheel_reaches_the_end_of_the_analytics_dashboard(page_for, make_user):
    page = page_for(make_user(role="admin"))
    # a short window overflows the dashboard even on an instance with little data
    page.set_viewport_size({"width": 1280, "height": 420})
    page.goto("/admin/settings/analytics")
    settings = page.get_by_role("dialog")
    leaderboard = settings.get_by_role("button", name="Models", exact=True, pressed=True)
    expect(leaderboard).to_be_visible()
    end_note = settings.get_by_text("Message counts are based on assistant responses.")
    expect(end_note).not_to_be_in_viewport()

    leaderboard.hover()
    for _ in range(25):
        page.mouse.wheel(0, 400)

    expect(end_note).to_be_in_viewport()
