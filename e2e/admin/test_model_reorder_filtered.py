"""Journey: an admin drags models into place on the admin models page, with and without a filter.

Open WebUI 0.11.5 lets a model be dragged while a search, view or tag filter narrows the list
(open-webui/open-webui#30390, issue #29634); before that the grip only worked on the full list.
The dragged model lands next to the visible model it was dropped on, and every model the filter
hides keeps its place in the stored order. Each test gives its models a place at the head of
`MODEL_ORDER_LIST` over the API, drags one on the page, saves and reads the stored order back.

Discriminates: passes on dev ef67cc3fa; with the frontend change of #30390 reverted both filtered
tests fail (the dragged row does not move under a search or a tag), while the unfiltered drag
passes on both. The drag hovers over the target until the row has moved, because the list ignores
the pointer for a moment after each swap. It passes on dev b40891e0a, and a frontend copy of it
that only lets a row be dragged without a search still turns the searched test red.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Locator, Page, expect

from harness.actors import Actor

pytestmark = [pytest.mark.journey, pytest.mark.requires_browser, pytest.mark.requires_source]

MODELS_CONFIG = ("/api/v1/configs/models", "/api/v1/configs/models")


class OrderedModels:
    """Workspace models named `<prefix> <label>`, stored at the head of the model order."""

    def __init__(self, admin: Actor, suffix: str) -> None:
        self.admin = admin
        self.suffix = suffix
        self.ids: dict[str, str] = {}

    def create(self, prefix: str, label: str, tags: tuple[str, ...] = ()) -> str:
        model_id = f"order-{label.lower()}-{self.suffix}"
        form = {
            "id": model_id,
            "name": f"{prefix} {label}",
            "base_model_id": "mock-model",
            "meta": {"tags": [{"name": tag} for tag in tags]},
            "params": {},
        }
        with self.admin.client() as client:
            created = client.post("/api/v1/models/create", json=form)
        assert created.status_code == 200, created.text
        self.ids[label] = model_id
        return model_id

    def put_first(self, labels: list[str]) -> None:
        with self.admin.client() as client:
            current = client.get("/api/v1/configs/models").json()
            head = [self.ids[label] for label in labels]
            rest = [
                model_id for model_id in current["MODEL_ORDER_LIST"] or [] if model_id not in head
            ]
            saved = client.post(
                "/api/v1/configs/models", json={**current, "MODEL_ORDER_LIST": head + rest}
            )
        assert saved.status_code == 200, saved.text

    def stored_head(self) -> list[str]:
        with self.admin.client() as client:
            stored = client.get("/api/v1/configs/models").json()["MODEL_ORDER_LIST"] or []
        labels_by_id = {model_id: label for label, model_id in self.ids.items()}
        return [labels_by_id.get(model_id, model_id) for model_id in stored[: len(self.ids)]]

    def delete_all(self) -> None:
        with self.admin.client() as client:
            for model_id in self.ids.values():
                client.post("/api/v1/models/model/delete", json={"id": model_id})


@pytest.fixture
def ordered_models(admin, preserve):
    preserve(MODELS_CONFIG)
    models = OrderedModels(admin, uuid.uuid4().hex[:8])
    yield models
    models.delete_all()


def _open_models_page(page: Page) -> Locator:
    page.goto("/admin/settings/models")
    settings = page.get_by_role("dialog")
    expect(settings.get_by_role("textbox", name="Search Models")).to_be_visible()
    return settings


def _row(settings: Locator, name: str) -> Locator:
    return settings.locator("#model-list > div").filter(has_text=name)


def _listed_names(settings: Locator, prefix: str) -> Locator:
    return settings.locator("#model-list").get_by_text(prefix)


def _row_index(settings: Locator, name: str) -> int:
    return settings.locator("#model-list > div").evaluate_all(
        "(rows, name) => rows.findIndex((row) => row.textContent.includes(name))", name
    )


def _drag_above(page: Page, settings: Locator, moved: str, onto: str) -> None:
    grip = _row(settings, moved).locator("svg").first
    target = _row(settings, onto)
    grip.hover()
    page.mouse.down()
    box = target.bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 4, steps=15)
    # the list ignores the pointer while a swap animates, so hover until the row has moved up
    for nudge in range(40):
        if _row_index(settings, moved) < _row_index(settings, onto):
            break
        page.mouse.move(box["x"] + 20, box["y"] + 2 + nudge % 3, steps=2)
        page.wait_for_timeout(50)
    page.mouse.up()


def _save(settings: Locator) -> None:
    settings.get_by_role("button", name="Save", exact=True).click()
    expect(settings.page.get_by_text("Model order saved successfully")).to_be_visible()


def test_a_searched_model_moves_and_the_hidden_models_keep_their_places(
    page_for, make_user, ordered_models
):
    shown, hidden = f"Shelf {ordered_models.suffix}", f"Crate {ordered_models.suffix}"
    for prefix, label in [
        (shown, "A"),
        (hidden, "H1"),
        (shown, "B"),
        (hidden, "H2"),
        (shown, "C"),
        (hidden, "H3"),
    ]:
        ordered_models.create(prefix, label)
    ordered_models.put_first(["A", "H1", "B", "H2", "C", "H3"])

    page = page_for(make_user(role="admin"))
    settings = _open_models_page(page)
    settings.get_by_role("textbox", name="Search Models").fill(shown)
    listed = _listed_names(settings, shown)
    expect(listed).to_have_text([f"{shown} A", f"{shown} B", f"{shown} C"])

    _drag_above(page, settings, f"{shown} C", f"{shown} A")
    expect(listed).to_have_text([f"{shown} C", f"{shown} A", f"{shown} B"])
    _save(settings)

    assert ordered_models.stored_head() == ["C", "A", "H1", "B", "H2", "H3"]


def test_a_model_in_a_tag_moves_and_the_untagged_models_stay_in_the_order(
    page_for, make_user, ordered_models
):
    tag = f"shelf{ordered_models.suffix}"
    prefix = f"Tagged {ordered_models.suffix}"
    for label, tags in [("A", (tag,)), ("H1", ()), ("B", (tag,)), ("H2", ()), ("C", (tag,))]:
        ordered_models.create(prefix, label, tags)
    ordered_models.put_first(["A", "H1", "B", "H2", "C"])

    page = page_for(make_user(role="admin"))
    settings = _open_models_page(page)
    settings.get_by_role("button", name="Tag", exact=True).click()
    page.get_by_role("button", name=tag, exact=True).click()
    listed = _listed_names(settings, prefix)
    expect(listed).to_have_text([f"{prefix} A", f"{prefix} B", f"{prefix} C"])

    _drag_above(page, settings, f"{prefix} B", f"{prefix} A")
    expect(listed).to_have_text([f"{prefix} B", f"{prefix} A", f"{prefix} C"])
    _save(settings)

    assert ordered_models.stored_head() == ["B", "A", "H1", "H2", "C"]


# ---------------------------------------------------------------- nearby


def test_a_model_dragged_in_the_full_list_keeps_its_new_place(page_for, make_user, ordered_models):
    prefix = f"Plain {ordered_models.suffix}"
    for label in ["A", "B", "C"]:
        ordered_models.create(prefix, label)
    ordered_models.put_first(["A", "B", "C"])

    page = page_for(make_user(role="admin"))
    settings = _open_models_page(page)
    listed = _listed_names(settings, prefix)
    expect(listed).to_have_text([f"{prefix} A", f"{prefix} B", f"{prefix} C"])

    _drag_above(page, settings, f"{prefix} C", f"{prefix} A")
    expect(listed).to_have_text([f"{prefix} C", f"{prefix} A", f"{prefix} B"])
    _save(settings)

    assert ordered_models.stored_head() == ["C", "A", "B"]
    reopened = _open_models_page(page)
    expect(_listed_names(reopened, prefix)).to_have_text(
        [f"{prefix} C", f"{prefix} A", f"{prefix} B"]
    )
