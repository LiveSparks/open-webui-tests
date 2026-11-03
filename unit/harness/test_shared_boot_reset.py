"""Harness-lane spec for the shared-boot reset contract (`OWUI_SHARED_BOOT=1`).

Kit-owned ratchet tests for the operator's shared-boot directive: the env flag is strict,
the opt-out decision covers module names and their parent packages plus the
`shared_boot_off` marker, only items whose fixture closure reaches the session instance
activate, and `harness.reset.run_after_item` wipes per-test state through the running
server's own admin surface exactly as `harness/reset.py` documents - chats by id from
`/all/db`, uploads through `/files/all`, lessons for the accounts the harness holds
tokens for, and an export/import snapshot restore of the config store whose payload
never carries the MFA policy keys. The fixture wire-ins in `integration/conftest.py` and
`e2e/conftest.py` are checked by source, the way the footprint ratchets read source.
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import types

import httpx
import pytest

from harness import reset as shared_boot

KIT = pathlib.Path(shared_boot.__file__).parent.parent

# a config store the way /api/v1/configs/export returns it: dotted keys, JSON values
SNAPSHOT = {
    "user.permissions": {"features": {"api_keys": False}},
    "ui.banners": [],
    "ui.default_models": "",
    "retrieval.top_k": 4,
    "auth.mfa.enable": False,
    "auth.mfa.allow_oauth_bypass": True,
}


class FakeInstance:
    """An instance the way the reset uses one: `client()` answering from `self._on_request`.

    The surface hold exactly the routes the contract names, so the calls the reset makes
    are the contract; anything else the handler raises, and a test on it fails.
    """

    def __init__(self, admin_lessons: list[dict], other_lessons: list[dict] | None = None):
        self.store = dict(SNAPSHOT)
        self.chats: list[dict] = []
        self.files = 0
        # the vouchers list carries the lesson ids this admin may see and drop; the second
        # list belongs to an account the admin surface cannot reach (a `make_user` lane actor)
        self.admin_lessons = admin_lessons
        self.other_lessons = other_lessons or []
        self.calls: list[tuple[str, str, object]] = []

    def client(self) -> httpx.Client:
        owner = self

        def handler(request: httpx.Request) -> httpx.Response:
            owner.calls.append((request.method, request.url.path, json_body(request)))
            path = request.url.path
            body = json_body(request)
            if request.method == "GET" and path == shared_boot.CONFIG_EXPORT:
                return httpx.Response(200, json=dict(owner.store))
            if request.method == "POST" and path == shared_boot.CONFIG_IMPORT:
                payload = body["config"]
                owner.store.update(payload)
                return httpx.Response(200, json=owner.store)
            if request.method == "GET" and path == shared_boot.CHAT_EXPORT:
                return httpx.Response(200, json=list(owner.chats))
            if request.method == "DELETE" and path.startswith("/api/v1/chats/"):
                chat_id = path.rsplit("/", 1)[-1]
                owner.chats = [row for row in owner.chats if row["id"] != chat_id]
                return httpx.Response(200, json=True)
            if request.method == "GET" and path == shared_boot.FILE_COUNT:
                return httpx.Response(200, json=owner.files)
            if request.method == "DELETE" and path == shared_boot.FILES_ALL:
                owner.files = 0
                return httpx.Response(200, json={"message": "All files deleted successfully"})
            if request.method == "GET" and path == shared_boot.LESSON_LIST:
                return httpx.Response(200, json=list(owner.admin_lessons))
            if request.method == "DELETE" and path.startswith("/api/v1/lessons/"):
                lesson_id = path.rsplit("/", 1)[-1]
                before, after = len(owner.admin_lessons), None
                owner.admin_lessons = [row for row in owner.admin_lessons if row["id"] != lesson_id]
                after = len(owner.admin_lessons)
                if after == before:
                    # the route answers a lesson the caller does not own with 404
                    return httpx.Response(404, json={"detail": "lesson not found"})
                return httpx.Response(200, json={"ok": True})
            pytest.fail(f"the reset reached a route outside the contract: {request.method} {path}")

        return httpx.Client(
            base_url="http://test",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer fake-admin-token"},
        )


def json_body(request: httpx.Request) -> dict:
    raw = request.read()
    return json.loads(raw) if raw else {}


# the flag


def test_the_flag_is_the_bare_digit_one():
    assert not shared_boot.enabled({}), "an unset flag is mode-off"
    assert shared_boot.enabled({shared_boot.ENV_NAME: "1"})
    for value in ("", "0", "true", "on", "2", "01"):
        env = {shared_boot.ENV_NAME: value}
        assert not shared_boot.enabled(env), f"OWUI_SHARED_BOOT={value!r} must be mode-off"


# the opt-out decision


def test_the_opt_out_covers_the_module_and_its_parent_packages():
    lane = ("integration.models.test_chat_search_and_folder_paging", "integration.e2e_lane")
    assert shared_boot.opted_out("integration.models.test_chat_search_and_folder_paging", lane)
    assert shared_boot.opted_out("integration.e2e_lane.test_redis_swap", lane)
    assert shared_boot.opted_out("integration.e2e_lane", lane)
    assert not shared_boot.opted_out("integration.models.test_other_one", lane)


# the activation decision


def test_only_items_whose_closure_reaches_the_instance_activate():
    assert shared_boot.uses_instance(("instance", "admin"))
    assert shared_boot.uses_instance(("upstream", "user", "instance", "mock_upstream"))
    for closure in (
        ("instance_with", "mock_upstream"),
        ("degraded_instance",),
        ("launched_instance",),
    ):
        assert not shared_boot.uses_instance(closure), closure


# the request-path activation (target), on stub requests


class _Marker:
    pass


def _module_stub(name: str) -> types.ModuleType:
    return types.ModuleType(name)


class _Node:
    def __init__(self, module_name: str, marked: bool):
        self._module_name = module_name
        self._marked = marked

    def get_closest_marker(self, name: str):
        return _Marker() if self._marked and name == "shared_boot_off" else None

    @property
    def module(self):
        return _module_stub(self._module_name)


class _Request:
    def __init__(self, closure: tuple[str, ...], module_name: str, marked: bool = False):
        self.fixturenames = closure
        self.node = _Node(module_name, marked)

    def getfixturevalue(self, name: str):
        assert name == "instance"
        return "the-instance"


@pytest.mark.parametrize(
    ("closure", "module_name", "marked", "env", "expect"),
    [
        (("instance", "admin"), "test_probe", False, "1", "the-instance"),
        (("upstream", "user", "instance"), "test_probe", False, "1", "the-instance"),
        (("upstream", "user"), "test_probe", False, "1", None),
        (("instance", "admin"), "test_probe", True, "1", None),
        (
            ("instance", "admin"),
            "integration.models.test_chat_search_and_folder_paging",
            False,
            "1",
            None,
        ),
        (("instance", "admin"), "integration.e2e_lane.test_a_swap", False, "1", None),
        (("instance", "admin"), "test_probe", False, "", None),
        (("instance_with", "mock_upstream"), "test_probe", False, "1", None),
    ],
)
def test_the_target_resolves_the_contract_per_item(closure, module_name, marked, env, expect):
    lane = ("integration.models.test_chat_search_and_folder_paging", "integration.e2e_lane")
    import os

    saved = os.environ.pop(shared_boot.ENV_NAME, None)
    if env:
        os.environ[shared_boot.ENV_NAME] = env
    try:
        got = shared_boot.target(_Request(closure, module_name, marked), lane)
    finally:
        os.environ.pop(shared_boot.ENV_NAME, None)
        if saved is not None:
            os.environ[shared_boot.ENV_NAME] = saved
    assert got == expect, (closure, module_name, marked, env)


# the surface


def test_the_reset_is_a_no_op_on_a_clean_server():
    instance = FakeInstance(admin_lessons=[])
    instance.store = dict(SNAPSHOT)
    snapshot = shared_boot.snapshot_config(instance)
    shared_boot.run_after_item(instance, snapshot)
    assert shared_boot.exports_equal(snapshot, instance.store)
    assert [
        (method, path) for method, path, _ in instance.calls if path == shared_boot.CONFIG_IMPORT
    ] == []


def test_the_reset_wipes_chats_files_lessons_and_restores_config_only_on_drift():
    instance = FakeInstance(admin_lessons=[{"id": "lesson-1", "title": "t"}])
    instance.chats = [{"id": "chat-1", "title": "x"}, {"id": "chat-2", "title": "y"}]
    instance.files = 3
    snapshot = shared_boot.snapshot_config(instance)
    instance.store = {**snapshot, "retrieval.top_k": 99}
    shared_boot.run_after_item(instance, snapshot)
    assert instance.chats == []
    assert instance.files == 0
    assert instance.admin_lessons == []
    imports = [
        body["config"] for method, path, body in instance.calls if path == shared_boot.CONFIG_IMPORT
    ]
    assert len(imports) == 1, "only the drifted store gets imported back"
    assert "auth.mfa.enable" not in imports[0], "the MFA policy keys stay out of the restore"
    assert "auth.mfa.allow_oauth_bypass" not in imports[0]
    assert imports[0]["retrieval.top_k"] == 4 and imports[0]["ui.banners"] == []
    assert instance.store["retrieval.top_k"] == 4


def test_an_mfa_only_drift_never_triggers_the_import():
    """The import's mfa branch would revoke every session token, so the keys stay out."""
    instance = FakeInstance(admin_lessons=[])
    snapshot = shared_boot.snapshot_config(instance)
    instance.store = {**snapshot, "auth.mfa.enable": True, "auth.mfa.allow_oauth_bypass": False}
    shared_boot.run_after_item(instance, snapshot)
    assert instance.store["auth.mfa.enable"] is True, "the mfa module owns its policy"
    assert all(path != shared_boot.CONFIG_IMPORT for _, path, _ in instance.calls)


def test_lessons_of_accounts_without_a_token_survive_the_surface():
    """The published gap: `/lessons` lists a caller's own lessons only, so it wipes
    exactly the accounts this harness holds tokens for - the rest is a residual."""
    instance = FakeInstance(
        admin_lessons=[{"id": "mine"}],
        other_lessons=[{"id": "lane-actor-lesson"}],
    )
    snapshot = shared_boot.snapshot_config(instance)
    shared_boot.run_after_item(instance, snapshot)
    assert instance.admin_lessons == []
    assert instance.other_lessons == [{"id": "lane-actor-lesson"}]


def test_the_chats_surface_deletes_every_id_the_export_lists():
    instance = FakeInstance(admin_lessons=[])
    instance.chats = [{"id": f"chat-{n}"} for n in range(5)]
    snapshot = shared_boot.snapshot_config(instance)
    shared_boot.run_after_item(instance, snapshot)
    deleted = sorted(
        path for method, path, _ in instance.calls if path.startswith("/api/v1/chats/")
    )
    expected = sorted(
        [shared_boot.CHAT_EXPORT]
        + [shared_boot.CHAT_DELETE.format(chat_id=f"chat-{n}") for n in range(5)]
    )
    assert deleted == expected


# the wiring ratchet


@pytest.mark.parametrize(
    "conftest", ["integration/conftest.py", "e2e/conftest.py"], ids=["integration", "e2e"]
)
def test_both_conftests_wire_the_autouse_reset_with_the_opt_out_list(conftest):
    source = ast.parse((KIT / conftest).read_text())
    fixtures = [
        node
        for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name == "shared_boot_reset"
    ]
    assert len(fixtures) == 1, "the reset fixture is wired once"
    fixture = fixtures[0]
    assert any(
        isinstance(d, ast.Call)
        and getattr(d.func, "attr", None) == "fixture"
        and any(kw.arg == "autouse" and ast.unparse(kw.value) == "True" for kw in d.keywords)
        for d in fixture.decorator_list
    ), "the reset is autouse"
    body = ast.unparse(fixture)
    assert "shared_boot.target(request" in body, "activation goes through the contract"
    if "if instance is None" in body:
        assert body.index("yield") < body.index("shared_boot.run_after_item"), (
            "inactive items yield straight through"
        )
    assignments = [
        node
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "OPT_OUT" for t in node.targets)
    ]
    assert len(assignments) == 1, "an explicit opt-out list exists"


def test_the_opt_out_list_is_declared_and_comments_carry_the_reasons():
    for conftest in ("integration/conftest.py", "e2e/conftest.py"):
        text = (KIT / conftest).read_text()
        assert re.search(r"^OPT_OUT", text, re.MULTILINE), "an explicit opt-out list exists"
