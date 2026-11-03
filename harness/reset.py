"""The shared-boot reset contract: per-test state through the server's own admin surface.

`instance` is already one boot per session in `harness/fixtures.py`, so a module-scoped
fixture that re-boots per module would only add boots (under `--dist loadfile` it is at
most one boot per worker). What the wider scope lacks is an explicit reset, and this
module is it: `OWUI_SHARED_BOOT=1` turns on the function-scoped autouse fixture the
`integration` and `e2e` conftests wire, which - after every function item whose fixture
closure reaches the session instance - wipes per-test state through the running server's
own API and restores the persisted config store to what the module's first shared-state
item found:

- chats, via `GET /api/v1/chats/all/db` then `DELETE /api/v1/chats/{id}` each (the admin
  reach `ENABLE_ADMIN_CHAT_ACCESS` grants, on by default): a chat row carries the whole
  conversation - message history, attachment references, status events - so the delete
  takes them with it;
- uploads, via `DELETE /api/v1/files/all` (which also empties the object storage and the
  vector index those uploads filled), skipped when the instance holds no files;
- lessons, for the accounts this harness holds tokens for, via `GET /api/v1/lessons` +
  `DELETE /api/v1/lessons/{id}`: the route only carries a caller's own lessons, and there
  is no admin delete-anything surface for lessons;
- the persisted config store wholesale, via `GET /api/v1/configs/export` snapshot and
  `POST /api/v1/configs/import` restore, with `MFA_STORAGE_KEYS` dropped from the
  payload: the import would route through `update_mfa_config`'s policy branch and revoke
  every session token, including the ones the fixtures hold.

What the reset deliberately does NOT touch, so shared mode stays a subset of mode-off's
dirt and the pass/fail classes hold: users (their tokens must stay valid; mode-off never
removes them either), workspace entities - models, tools, prompts, folders, notes,
groups, channels, knowledge bases, feedback rows, and machines (`routers/agent_machines`
is POST-only, with no admin delete surface) - which mode-off leaves on the session
instance exactly the same way, and the lanes' own `instance_with` backends, which this
reset never sees.
"""

from __future__ import annotations

import os

import httpx
from pytest import FixtureRequest

from harness.instance import LaunchedInstance

CHAT_EXPORT = "/api/v1/chats/all/db"
CHAT_DELETE = "/api/v1/chats/{chat_id}"
FILE_COUNT = "/api/v1/files/count"
FILES_ALL = "/api/v1/files/all"
LESSON_DELETE = "/api/v1/lessons/{lesson_id}"
LESSON_LIST = "/api/v1/lessons"
CONFIG_EXPORT = "/api/v1/configs/export"
CONFIG_IMPORT = "/api/v1/configs/import"

# `update_mfa_config` reads these as the MFA policy: an import that moves them revokes
# every session, and an mfa module that wants them changed sets them itself.
MFA_STORAGE_KEYS = (
    "auth.mfa.enable",
    "auth.mfa.allow_oauth_bypass",
    "auth.mfa.allow_trusted_header_bypass",
)

ON_VALUE = "1"
ENV_NAME = "OWUI_SHARED_BOOT"
OPT_OUT_MARKER = "shared_boot_off"


def register_opt_out_marker(config) -> None:
    """Idempotent registration of the `shared_boot_off` marker, called by both conftests."""
    if not any(line.split(":")[0] == OPT_OUT_MARKER for line in config.getini("markers")):
        config.addinivalue_line(
            "markers",
            f"{OPT_OUT_MARKER}: this module's items are out of the {ENV_NAME} reset contract",
        )


def enabled(environ: dict[str, str] | None = None) -> bool:
    """`OWUI_SHARED_BOOT=1`, and nothing else: unset, empty or any other value is mode-off."""
    source = environ if environ is not None else os.environ
    return source.get(ENV_NAME) == ON_VALUE


def opted_out(module_name: str, opt_out: tuple[str, ...]) -> bool:
    """The module itself or any parent package of its dotted path named on the opt-out list."""
    parts = module_name.split(".")
    names = (module_name, *(".".join(parts[:width]) for width in range(1, len(parts))))
    return any(name in opt_out for name in names)


def uses_instance(fixturenames: tuple[str, ...]) -> bool:
    """The item's fixture closure holds the session instance (or anything that reaches it)."""
    return "instance" in fixturenames


def target(request: FixtureRequest, opt_out: tuple[str, ...]) -> LaunchedInstance | None:
    """The session instance to reset, or None when this item is out of the contract's reach."""
    if not enabled() or not uses_instance(request.fixturenames):
        return None
    if request.node.get_closest_marker(OPT_OUT_MARKER) or opted_out(
        request.node.module.__name__, opt_out
    ):
        return None
    return request.getfixturevalue("instance")


def exports_equal(left: dict, right: dict) -> bool:
    """The live store still matches the snapshot, and with an MFA key ignored.

    The MFA keys stay out of the comparison the same reason they stay out of the
    restore: an mfa module manages them itself.
    """
    without_mfa = lambda store: {  # noqa: E731
        key: value for key, value in store.items() if key not in MFA_STORAGE_KEYS
    }
    return without_mfa(left) == without_mfa(right)


def snapshot_config(instance) -> dict:
    """The config store as the caller reads it off the instance's export surface."""
    with instance.client() as client:
        exported = client.get(CONFIG_EXPORT)
        exported.raise_for_status()
        return exported.json()


_snapshot_cache: dict[str, dict] = {}


def snapshot_for(module_name: str, instance: LaunchedInstance) -> dict:
    """The module's cached config-store snapshot, taken on the first shared-state item of it."""
    if module_name not in _snapshot_cache:
        with instance.client() as client:
            exported = client.get(CONFIG_EXPORT)
            exported.raise_for_status()
            _snapshot_cache[module_name] = exported.json()
    return _snapshot_cache[module_name]


def restore_config(client: httpx.Client, snapshot: dict) -> None:
    """Write the exported store back under itself; skipped when nothing drifted."""
    exported = client.get(CONFIG_EXPORT)
    exported.raise_for_status()
    if exports_equal(snapshot, exported.json()):
        return
    payload = {key: value for key, value in snapshot.items() if key not in MFA_STORAGE_KEYS}
    imported = client.post(CONFIG_IMPORT, json={"config": payload})
    imported.raise_for_status()


def wipe_chats(client: httpx.Client) -> list[str]:
    """Every chat on the instance, this admin's or anyone's: ids through `/all/db`."""
    listed = client.get(CHAT_EXPORT)
    listed.raise_for_status()
    dropped: list[str] = []
    for row in listed.json():
        dropped.append(row["id"])
        removed = client.delete(CHAT_DELETE.format(chat_id=row["id"]))
        if removed.status_code not in (200, 404):
            removed.raise_for_status()
    return dropped


def wipe_files(client: httpx.Client) -> bool:
    """Every upload with its storage and its vectors, when there are any."""
    counted = client.get(FILE_COUNT)
    counted.raise_for_status()
    if not counted.json():
        return False
    client.delete(FILES_ALL).raise_for_status()
    return True


def wipe_admin_lessons(client: httpx.Client) -> list[str]:
    """The lessons this admin's own account holds.

    The route lists a caller's own lessons and deletes only one the caller owns, so
    lessons of accounts this harness holds no token for survive the reset. The session
    instance stays quiet there - the learning lanes write lessons onto their own
    `instance_with` backends - and the shared-boot lane report publishes the gap.
    """
    listed = client.get(LESSON_LIST)
    listed.raise_for_status()
    dropped: list[str] = []
    for lesson in listed.json():
        removed = client.delete(LESSON_DELETE.format(lesson_id=lesson["id"]))
        if removed.status_code not in (200, 404):
            removed.raise_for_status()
        dropped.append(str(lesson["id"]))
    return dropped


def run_after_item(instance: LaunchedInstance, snapshot: dict) -> None:
    """The post-item body of the autouse reset fixture."""  # lessons, files, chats, store restore
    # one keep-alive connection for the four steps: the reset runs per item, where a
    # fresh connection each would be a visible slice of its wall
    with instance.client() as client:
        wipe_admin_lessons(client)
        wipe_files(client)
        wipe_chats(client)
        restore_config(client, snapshot)
