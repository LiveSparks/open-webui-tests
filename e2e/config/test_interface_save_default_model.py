"""Saving the Interface settings overwrote the account's default model, open-webui/open-webui#30952.

Fix commit `e67d7f9e4` (PR open-webui/open-webui#30953). The Interface tab's Save wrote
`models: [defaultModelId]` along with its own fields, where `defaultModelId` was the first of
the account's default models, or the administrator's default when one was set. So a Save cut a
multi-model default down to its first model, or replaced the account's choice with the
administrator's. The tab no longer touches the default model.

Discriminates: passes on the efe63bd34 build, fails on it with `e67d7f9e4` reverted (the saved
default models are cut to the first one, or replaced by the administrator's).
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from harness.python_tools import EVERYONE_READS
from harness.upstream import MOCK_MODEL_ID
from utils.chat_ui import chat_input
from utils.user_menu import open_settings

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

MODELS_CONFIG = ("/api/v1/configs/models", "/api/v1/configs/models")


@pytest.fixture
def second_model(admin) -> str:
    """A preset on the scripted model that every account may use."""
    model_id = f"second-{uuid.uuid4().hex[:8]}"
    form = {
        "id": model_id,
        "base_model_id": MOCK_MODEL_ID,
        "name": "Second model",
        "meta": {},
        "params": {},
        "access_grants": [EVERYONE_READS],
    }
    with admin.client() as client:
        created = client.post("/api/v1/models/create", json=form)
        assert created.status_code == 200, created.text
        yield model_id
        client.post("/api/v1/models/model/delete", json={"id": model_id})


def account_with(make_user, **ui):
    account = make_user()
    if ui:
        with account.client() as client:
            client.post("/api/v1/users/user/settings/update", json={"ui": ui}).raise_for_status()
    return account


def save_interface_tab(page: Page) -> None:
    expect(chat_input(page)).to_be_visible()
    open_settings(page)
    page.get_by_role("tab", name="Interface").click()
    with page.expect_response(lambda response: "/user/settings/update" in response.url):
        page.locator("#tab-interface").get_by_role("button", name="Save").click()


def saved_ui(account) -> dict:
    with account.client() as client:
        settings = client.get("/api/v1/users/user/settings")
    settings.raise_for_status()
    return settings.json()["ui"]


def test_saving_keeps_a_multi_model_default(page_for, make_user, second_model):
    account = account_with(make_user, models=[MOCK_MODEL_ID, second_model])

    save_interface_tab(page_for(account))

    assert saved_ui(account)["models"] == [MOCK_MODEL_ID, second_model]


def test_saving_keeps_the_accounts_default_over_the_administrators(
    page_for, make_user, admin, preserve, second_model
):
    preserve(MODELS_CONFIG)
    with admin.client() as client:
        current = client.get(MODELS_CONFIG[0]).json()
        saved = client.post(MODELS_CONFIG[1], json={**current, "DEFAULT_MODELS": second_model})
        saved.raise_for_status()
    account = account_with(make_user, models=[MOCK_MODEL_ID])

    save_interface_tab(page_for(account))

    assert saved_ui(account)["models"] == [MOCK_MODEL_ID]


def test_saving_without_a_default_model_leaves_none(page_for, make_user):
    account = account_with(make_user, highContrastMode=False)

    save_interface_tab(page_for(account))

    assert saved_ui(account).get("models") in (None, [])
