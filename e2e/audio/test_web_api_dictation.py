"""Regression: Web API dictation switched itself off while the user was still talking.

Issue open-webui/open-webui#19727, fix 1e834c09e (PR open-webui/open-webui#31891). The chat
input's dictation ends the browser's speech recognition after two seconds without a result. It
never asked for interim results, so a browser like Chromium sends nothing while a phrase is
still being spoken, and talking for longer than two seconds cut the dictation off and kept only
the earlier phrases. It now asks for interim results, which keep it listening, and takes only
the final ones into the text. Leaving the chat while dictating turns the microphone off
(open-webui/open-webui#32640, fix 636b631ab; before it the recording stopped showing but the
microphone stayed on).

Discriminates: passes on the dev b859124f9 build, fails on that build with 1e834c09e reverted
(dictation stops two seconds into the long phrase and the chat input has only the first words).
The leaving test goes red on the 0.12.4 pre-fix build (636b631ab reverted among other fixes): the
microphone track stays live after the chat is left.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from harness.chat_history import seed_chat
from utils.chat_ui import chat_input

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

FIRST_PHRASE = "hello there "
SECOND_PHRASE = "this is a long dictated sentence"
# A recognizer that behaves like Chromium's: interim results only when asked for, a final result
# as each phrase ends. The user says one short phrase, then talks for 3.4 s with the recognizer
# reporting progress every 400 ms, pauses for 0.3 s and the phrase is final.
FAKE_RECOGNIZER = """() => {
    const first = %s;
    const second = %s;
    const words = second.split(' ');
    const state = {started: 0, running: false, interimSent: 0, runningAtLastFinal: null};
    window.dictation = state;

    class FakeSpeechRecognition {
        constructor() {
            this.timers = [];
            this.results = [];
        }
        start() {
            state.started += 1;
            state.running = true;
            this.timers.push(setTimeout(() => this.report(0, first, true), 300));
            words.forEach((_, index) => {
                const delay = 600 + index * 400;
                const last = index === words.length - 1;
                if (!last) {
                    this.timers.push(setTimeout(() => this.interim(index), delay));
                }
            });
            const end = 600 + words.length * 400 + 300;
            this.timers.push(setTimeout(() => this.report(1, second, true, true), end));
        }
        interim(index) {
            if (!this.interimResults) return;
            state.interimSent += 1;
            this.report(1, words.slice(0, index + 1).join(' '), false);
        }
        report(position, transcript, isFinal, lastFinal = false) {
            const result = [{transcript, confidence: 0.9}];
            result.isFinal = isFinal;
            this.results[position] = result;
            if (lastFinal) state.runningAtLastFinal = state.running;
            this.onresult?.({resultIndex: position, results: [...this.results]});
        }
        stop() {
            if (!state.running) return;
            this.timers.forEach(clearTimeout);
            state.running = false;
            setTimeout(() => this.onend?.(), 0);
        }
    }
    window.SpeechRecognition = FakeSpeechRecognition;
    window.webkitSpeechRecognition = FakeSpeechRecognition;
}""" % (json.dumps(FIRST_PHRASE), json.dumps(SECOND_PHRASE))


@pytest.fixture
def web_api_dictation(voice_page_for, make_user) -> Page:
    """A page whose user dictates with the browser's own speech recognition."""
    account = make_user()
    with account.client() as client:
        saved = client.post(
            "/api/v1/users/user/settings/update", json={"ui": {"audio": {"stt": {"engine": "web"}}}}
        )
    assert saved.status_code == 200, saved.text
    page = voice_page_for(account)
    page.add_init_script(f"({FAKE_RECOGNIZER})()")
    page.goto("/")
    return page


def test_dictation_keeps_listening_through_a_long_phrase(web_api_dictation):
    page = web_api_dictation
    expect(chat_input(page)).to_be_visible()

    page.get_by_role("button", name="Voice Input").click()

    page.wait_for_function(
        "() => window.dictation.runningAtLastFinal !== null"
        " || (window.dictation.started > 0 && !window.dictation.running)"
    )
    assert page.evaluate("() => window.dictation.runningAtLastFinal === true"), (
        "dictation stopped before the user did"
    )
    expect(chat_input(page)).to_have_text(f"{FIRST_PHRASE}{SECOND_PHRASE}")


# keeps every microphone track the page opens, to read whether it is still live
RECORD_MICROPHONE_TRACKS = """() => {
    const open = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    window.microphoneTracks = [];
    navigator.mediaDevices.getUserMedia = async (constraints) => {
        const stream = await open(constraints);
        window.microphoneTracks.push(...stream.getAudioTracks());
        return stream;
    };
}"""
LIVE_TRACKS = "() => window.microphoneTracks.filter((track) => track.readyState === 'live').length"


def test_leaving_the_chat_while_dictating_turns_the_microphone_off(voice_page_for, make_user):
    """open-webui/open-webui#32640, fix 636b631ab: the microphone stayed on after leaving."""
    account = make_user()
    with account.client() as client:
        chat_id, _ = seed_chat(
            client,
            [
                {"role": "user", "content": "the dictated question"},
                {"role": "assistant", "content": "The answer before dictating."},
            ],
        )
    page = voice_page_for(account)
    page.add_init_script(f"({RECORD_MICROPHONE_TRACKS})()")
    page.goto(f"/c/{chat_id}")
    expect(chat_input(page)).to_be_visible()

    page.get_by_role("button", name="Voice Input").click()
    expect(page.get_by_role("button", name="Confirm recording")).to_be_visible()
    page.wait_for_function(f"({LIVE_TRACKS})() > 0")
    page.get_by_role("link", name="New Chat").first.click()

    expect(page).to_have_url(re.compile(r"/$"))
    expect(page.get_by_role("button", name="Confirm recording")).to_have_count(0)
    try:
        page.wait_for_function(f"({LIVE_TRACKS})() === 0")
    except PlaywrightTimeoutError:
        live = page.evaluate(LIVE_TRACKS)
        raise AssertionError(
            f"the microphone stayed on after leaving the chat: {live} live"
        ) from None
