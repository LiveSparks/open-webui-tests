"""Regression: a failed Read Aloud playback left the speaker button stuck in "speaking".

Issue open-webui/open-webui#30262, fix 7924ce7d7 (PR open-webui/open-webui#30373). The audio
queue ignored a rejected `play()`, so when the browser refused to play the speech (as iOS does
for playback started outside a tap) its state never reset: the button stayed in "speaking", no
error showed and the next tap only stopped a playback that never started. A rejected playback
now resets the queue, returns the button to idle and shows "Audio playback failed". The iOS
unlock itself needs WebKit on a device and is not covered here.

Discriminates: passes on the dev efe63bd34 build, fails on that build with 7924ce7d7 reverted
(no error shows and the second tap asks for no speech).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from harness.chat_history import seed_chat

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

ANSWER = "The tide comes in twice a day."
UNPLAYABLE = b"this is not audio at all"


@pytest.fixture
def unplayable_speech(speech_engine):
    speech_engine.speech = UNPLAYABLE
    return speech_engine


def open_answer(page_for, make_user) -> Page:
    owner = make_user()
    with owner.client() as client:
        chat_id, _ = seed_chat(
            client,
            [{"role": "user", "content": "tides?"}, {"role": "assistant", "content": ANSWER}],
        )
    page = page_for(owner, path=f"/c/{chat_id}")
    expect(page.get_by_text(ANSWER)).to_be_visible()
    return page


def read_aloud(page: Page):
    """Press Read Aloud; returns the speech request the page made."""
    with page.expect_request(lambda request: request.url.endswith("/api/v1/audio/speech")) as asked:
        page.get_by_role("button", name="Read Aloud").click()
    return asked.value


def test_a_failed_playback_shows_an_error_and_the_next_tap_reads_again(
    page_for, make_user, unplayable_speech
):
    page = open_answer(page_for, make_user)

    read_aloud(page)
    expect(page.get_by_text("Audio playback failed").first).to_be_visible()

    # a wedged button only stops on this tap and asks for no speech
    again = read_aloud(page)
    assert again.post_data_json["input"] == ANSWER
