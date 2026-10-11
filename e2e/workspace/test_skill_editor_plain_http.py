"""Regression: the skill editor stays usable when Open WebUI is opened over plain HTTP by address.

Issue open-webui/open-webui#32238. Opened at an address such as http://192.168.1.20:8080, the page
is not a secure context, so the browser offers no `crypto.randomUUID`. Creating a skill or opening
one to edit then threw "crypto.randomUUID is not a function" while the editor loaded, and its
instructions could not be typed or saved. The browser here reaches the instance by a host name
that is neither `localhost` nor a loopback address, as a phone or another computer on the network
does.

Discriminates: both tests fail on the dev ee1d9255f build (the editor never takes the typed
instructions), and pass where the skill editor makes its ids without `crypto.randomUUID`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Callable, Iterator
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, Playwright, expect

from harness.actors import Actor
from harness.backends import wait_until
from utils.skill_editor import code_editor, replace_text

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

LAN_HOST = "owui-lan.test"


@pytest.fixture
def lan_page_for(playwright_instance: Playwright, config) -> Iterator[Callable[[Actor], Page]]:
    """`lan_page_for(actor)` opens a signed-in page on the instance through `LAN_HOST`."""
    browser = playwright_instance.chromium.launch(
        headless=config.headless, args=[f"--host-resolver-rules=MAP {LAN_HOST} 127.0.0.1"]
    )

    def open_page(actor: Actor) -> Page:
        with actor.client() as client:
            client.post(
                "/api/v1/users/user/settings/update", json={"ui": {"showChangelog": False}}
            ).raise_for_status()
        port = urlsplit(actor.base_url).port
        context = browser.new_context(
            base_url=f"http://{LAN_HOST}:{port}", viewport={"width": 1920, "height": 1080}
        )
        context.set_default_timeout(config.default_timeout)
        page = context.new_page()
        page.add_init_script(
            f"try {{ localStorage.setItem('token', {json.dumps(actor.token)}); }} catch (e) {{}}"
        )
        page.goto("/")
        assert page.evaluate("isSecureContext") is False
        return page

    yield open_page
    browser.close()


@pytest.fixture
def skill_writer(make_user) -> Iterator[Actor]:
    """A fresh admin; the skills it made are deleted afterwards."""
    account = make_user(role="admin")
    yield account
    with account.client() as client:
        for skill in client.get("/api/v1/skills/").json():
            if skill["user_id"] == account.id:
                client.delete(f"/api/v1/skills/id/{skill['id']}/delete")


def _skill_content(writer: Actor, skill_id: str) -> str:
    with writer.client() as client:
        fetched = client.get(f"/api/v1/skills/id/{skill_id}")
    assert fetched.status_code == 200, fetched.text
    return fetched.json()["content"]


def test_a_new_skill_can_be_written_and_saved(lan_page_for, skill_writer):
    page = lan_page_for(skill_writer)
    page.goto("/workspace/skills/create")
    editor = page.get_by_role("main")
    editor.get_by_placeholder("Skill Name").fill(f"Harbour notes {uuid.uuid4().hex[:6]}")
    editor.get_by_placeholder("Skill Description").fill("how to answer")
    replace_text(page, "Answer in one sentence.")
    expect(code_editor(page)).to_contain_text("Answer in one sentence.")

    editor.get_by_role("button", name="Save & Create").click()

    expect(page).to_have_url(re.compile(r"/workspace/skills/edit\?id="))
    skill_id = page.url.split("id=", 1)[1]
    assert "Answer in one sentence." in _skill_content(skill_writer, skill_id)


def test_an_existing_skill_can_be_edited_and_saved(lan_page_for, skill_writer):
    skill_id = f"skill-{uuid.uuid4().hex[:8]}"
    with skill_writer.client() as client:
        created = client.post(
            "/api/v1/skills/create",
            json={"id": skill_id, "name": "Tide notes", "content": "Be brief.", "meta": {}},
        )
    assert created.status_code == 200, created.text
    page = lan_page_for(skill_writer)
    page.goto(f"/workspace/skills/edit?id={skill_id}")
    expect(code_editor(page)).to_contain_text("Be brief.")

    replace_text(page, "Answer with the tide times.")
    expect(code_editor(page)).to_contain_text("Answer with the tide times.")
    page.get_by_role("main").get_by_role("button", name="Save", exact=True).click()

    assert wait_until(
        lambda: "Answer with the tide times." in _skill_content(skill_writer, skill_id), 10
    )
