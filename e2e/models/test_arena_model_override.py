"""A public arena model vanished from users' model selector once an admin edited it, #29564.

Fix commit `b8de508db` (open-webui/open-webui#31309). Saving the arena model on the admin models
page (to give it tools, say) replaced its metadata with the saved row's, dropping the access
grants set under Settings > Evaluations, so every non-admin user lost it. The override now keeps
the grants and the model pool. Twin of integration/models/test_arena_model_override.py.

Discriminates: passes on dev efe63bd34; with b8de508db reverted in a backend copy the user's
selector no longer offers the edited arena model; the unedited one is offered on both.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterator

import pytest
from playwright.sync_api import Page, expect

from harness.actors import Actor
from harness.upstream import MOCK_MODEL_ID
from utils.model_selector import model_search

pytestmark = [pytest.mark.regression, pytest.mark.requires_browser, pytest.mark.requires_source]

EVALUATION_CONFIG = ("/api/v1/evaluations/config", "/api/v1/evaluations/config")
PUBLIC_READ = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]


@pytest.fixture
def arena(admin, preserve) -> Iterator[tuple[str, str]]:
    """A public arena model on the scripted model, named uniquely for the selector search."""
    preserve(EVALUATION_CONFIG)
    arena_id = f"arena-{uuid.uuid4().hex[:8]}"
    name = f"Arena {arena_id[-8:]}"
    arena = {
        "id": arena_id,
        "name": name,
        "meta": {"model_ids": [MOCK_MODEL_ID], "access_grants": PUBLIC_READ},
    }
    with admin.client() as client:
        saved = client.post(
            "/api/v1/evaluations/config",
            json={"ENABLE_EVALUATION_ARENA_MODELS": True, "EVALUATION_ARENA_MODELS": [arena]},
        )
        assert saved.status_code == 200, saved.text
        yield arena_id, name
        client.post("/api/v1/models/model/delete", json={"id": arena_id})


def _edit_in_settings_models(admin: Actor, arena_id: str, name: str) -> None:
    """What saving the arena model on the admin models page stores."""
    with admin.client() as client:
        saved = client.post(
            "/api/v1/models/create",
            json={
                "id": arena_id,
                "base_model_id": None,
                "name": name,
                "meta": {"description": "Edited on the models page", "toolIds": []},
                "params": {},
            },
        )
        assert saved.status_code == 200, saved.text
        client.get("/api/models", params={"refresh": True}).raise_for_status()


def _expect_offered(page: Page, name: str) -> None:
    page.goto("/")
    page.get_by_role("button", name=re.compile("^Selected model")).click()
    model_search(page).fill(name)
    available = page.get_by_role("listbox", name="Available models")
    expect(available.get_by_role("option", name=f"Select {name} model")).to_be_visible()


def test_a_user_is_still_offered_an_edited_public_arena_model(page_for, make_user, admin, arena):
    arena_id, name = arena
    _edit_in_settings_models(admin, arena_id, name)

    _expect_offered(page_for(make_user()), name)


# ---------------------------------------------------------------- nearby


def test_a_user_is_offered_an_unedited_public_arena_model(page_for, make_user, arena):
    _, name = arena
    _expect_offered(page_for(make_user()), name)
