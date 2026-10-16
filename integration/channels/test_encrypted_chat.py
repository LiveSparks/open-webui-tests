"""Journey: temporary end-to-end encrypted chats in direct messages and private groups.

An admin turns them on in Admin Settings > General (or with `ENABLE_ENCRYPTED_CHANNELS`), and a
user also needs the Encrypted Channels feature permission (`features.encrypted_channels`, off by
default; `USER_PERMISSIONS_FEATURES_ENCRYPTED_CHANNELS` turns it on for everyone). Admins need no
permission. The browser encrypts every message itself; the server only relays the encrypted
packets over the websocket to the other people in the chat and never saves them. The relay
answers only members of a direct message or private group, refuses a packet over 1 MiB, refuses
everyone while websocket server logging is on, tells the host when someone leaves and ends the
chat for everyone when the host leaves or disconnects, when someone is removed from the group or
when the admin switches the feature off.

Discriminates: passes on dev f5a26f53a. Each of these edits in a backend copy turns the named
test red: the relay skipping the permission (permission tests) or the membership check (outsider
test), letting public group channels through (channel kind test), a 2 MiB packet limit (size
test), ignoring websocket server logging (logging test), no end of the session on a member change
(removal test), ignoring the admin switch (switch-off test), ending the session when a guest leaves
(guest leave test), keeping it when the host leaves or disconnects (host tests), forwarding no
packets (exchange test), an admin needing the permission (admin test), and saving each packet as a
channel message, into the settings table, into the relay's Redis state or printing it to the log
(never stored test, the Redis case run with Redis). A settings save that drops the switch when it
is left out, an app config that always reports it off, either default on and the permission
variable ignored turn the switch, default and environment tests red.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

import pytest
import redis
import sqlalchemy

from harness.access import make_group
from harness.actors import Actor, create_user
from harness.backends import wait_until
from harness.channel_quotes import enable_channels
from harness.instance import LaunchedInstance
from harness.socket_client import SocketSession, connected

pytestmark = [pytest.mark.journey, pytest.mark.api, pytest.mark.requires_source]

ADMIN_CONFIG = "/api/v1/auths/admin/config"
MAX_PACKET = 1024 * 1024
PERMITTED = {"features": {"encrypted_channels": True}}
ON_BY_ENV = {
    "ENABLE_CHANNELS": "true",
    "ENABLE_ENCRYPTED_CHANNELS": "true",
    "USER_PERMISSIONS_FEATURES_ENCRYPTED_CHANNELS": "true",
}


def _set_admin_config(admin: Actor, **switches) -> dict:
    with admin.client() as client:
        current = client.get(ADMIN_CONFIG)
        assert current.status_code == 200, current.text
        saved = client.post(ADMIN_CONFIG, json={**current.json(), **switches})
        assert saved.status_code == 200, saved.text
        stored = client.get(ADMIN_CONFIG)
    assert stored.status_code == 200, stored.text
    return stored.json()


def _offered(actor: Actor) -> bool:
    """Whether the app config the web client loads says encrypted chats are on."""
    with actor.client() as client:
        config = client.get("/api/config")
    assert config.status_code == 200, config.text
    return config.json()["features"]["enable_encrypted_channels"]


def _permission(actor: Actor) -> bool:
    with actor.client() as client:
        session = client.get("/api/v1/auths/")
    assert session.status_code == 200, session.text
    return session.json()["permissions"]["features"]["encrypted_channels"]


@pytest.fixture
def relay_on(admin, preserve):
    preserve("admin_config")
    enable_channels(admin)
    _set_admin_config(admin, ENABLE_ENCRYPTED_CHANNELS=True)


@pytest.fixture
def pair(admin, make_user, relay_on) -> tuple[Actor, Actor]:
    """Two people allowed to use encrypted chats."""
    first, second = make_user(), make_user()
    make_group(admin, [first, second], PERMITTED)
    return first, second


def _direct_message(actor: Actor, other: Actor) -> str:
    with actor.client() as client:
        opened = client.get(f"/api/v1/channels/users/{other.id}")
    assert opened.status_code == 200, opened.text
    return opened.json()["id"]


def _channel(owner: Actor, members: list[Actor], **form) -> str:
    with owner.client() as client:
        created = client.post(
            "/api/v1/channels/create",
            json={
                "name": f"channel-{uuid.uuid4().hex[:8]}",
                "user_ids": [member.id for member in members],
                **form,
            },
        )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _private_group(owner: Actor, *members: Actor) -> str:
    return _channel(owner, list(members), type="group", is_private=True)


@contextmanager
def _tab(actor: Actor) -> Iterator[SocketSession]:
    """A signed-in websocket that has joined its channel rooms, as an open web client has."""
    with connected(actor) as tab:
        tab.join_channels(actor)
        yield tab


def _relay(tab: SocketSession, action: str, **data) -> dict:
    return tab.call(f"encryption:{action}", data)


def _encoded(content: bytes) -> str:
    return base64.b64encode(content).decode()


def _key() -> str:
    return _encoded(os.urandom(32))


def _host(tab: SocketSession, channel_id: str) -> dict:
    """Start an encrypted chat and publish the host's key, as Encrypted Chat in the menu does."""
    started = _relay(tab, "start", channel=channel_id)
    assert started["ok"], f"starting an encrypted chat was refused: {started}"
    ticket = {"channel": channel_id, "session": started["session"]}
    bound = _relay(tab, "bind", **ticket, participation=started["participation"], key=_key())
    assert bound["ok"], f"publishing the host's key was refused: {bound}"
    return {**ticket, "participation": started["participation"]}


def _join(host: SocketSession, host_ticket: dict, guest: SocketSession) -> dict:
    """Join through the Join link and get admitted by the host's browser."""
    ticket = {"channel": host_ticket["channel"], "session": host_ticket["session"]}
    joined = _relay(guest, "join", **ticket)
    assert joined["ok"], f"joining the encrypted chat was refused: {joined}"
    key_package = _encoded(b"key package " + os.urandom(16))
    bound = _relay(
        guest,
        "bind",
        **ticket,
        participation=joined["participation"],
        key=_key(),
        packet=key_package,
    )
    assert bound["ok"], f"publishing the guest's key was refused: {bound}"
    candidate = host.encryption_event("candidate")
    assert candidate["packet"] == key_package, candidate
    admitted = _relay(
        host,
        "packet",
        **host_ticket,
        kind="admit",
        target=joined["participation"],
        packet=_encoded(b"commit"),
        welcome=_encoded(b"welcome"),
    )
    assert admitted["ok"], f"admitting the guest was refused: {admitted}"
    assert guest.encryption_event("welcome")["packet"] == _encoded(b"welcome")
    return {**ticket, "participation": joined["participation"]}


def _send(tab: SocketSession, ticket: dict, content: bytes) -> dict:
    return _relay(tab, "packet", **ticket, kind="mls", packet=_encoded(content))


def _received(tab: SocketSession) -> list[str]:
    return [event["packet"] for event in tab.encryption_events if event.get("kind") == "mls"]


def _status(tab: SocketSession, channel_id: str) -> dict:
    return _relay(tab, "status", channel=channel_id)


def _wait_ended(tab: SocketSession) -> dict:
    return tab.encryption_event("ended")


def _settle() -> None:
    # bounded pause showing that nothing more arrives
    time.sleep(1)


# the switches


def test_encrypted_chats_are_off_by_default_for_the_instance_and_for_users(admin, make_user):
    with admin.client() as client:
        config = client.get(ADMIN_CONFIG)
    assert config.status_code == 200, config.text

    assert config.json()["ENABLE_ENCRYPTED_CHANNELS"] is False, config.json()
    assert _offered(admin) is False
    assert _permission(make_user()) is False, "a new user may use encrypted chats by default"


def test_the_admin_switch_turns_encrypted_chats_on_and_a_save_without_it_keeps_it(
    admin, preserve, make_user
):
    preserve("admin_config")
    user = make_user()

    stored = _set_admin_config(admin, ENABLE_CHANNELS=True, ENABLE_ENCRYPTED_CHANNELS=True)

    assert stored["ENABLE_ENCRYPTED_CHANNELS"] is True, stored
    assert _offered(user) is True, "the app config does not report the switched-on feature"
    without_switch = {
        key: value for key, value in stored.items() if key != "ENABLE_ENCRYPTED_CHANNELS"
    }
    with admin.client() as client:
        saved = client.post(ADMIN_CONFIG, json=without_switch)
    assert saved.status_code == 200, saved.text
    assert _offered(user) is True, "a settings save that did not carry the switch turned it off"

    _set_admin_config(admin, ENABLE_ENCRYPTED_CHANNELS=False)

    assert _offered(user) is False


@pytest.mark.slow
def test_the_environment_turns_encrypted_chats_and_the_permission_on(instance_with):
    instance = instance_with(ON_BY_ENV)
    first, second = create_user(instance), create_user(instance)

    assert _offered(first) is True
    assert _permission(first) is True
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab:
        _host(host_tab, channel_id)


@pytest.mark.slow
def test_websocket_server_logging_keeps_the_relay_closed(instance_with):
    instance = instance_with({**ON_BY_ENV, "WEBSOCKET_SERVER_LOGGING": "true"})
    first, second = create_user(instance), create_user(instance)
    channel_id = _direct_message(first, second)

    with _tab(first) as host_tab:
        refused = _relay(host_tab, "start", channel=channel_id)

    assert refused["ok"] is False, (
        f"the relay started an encrypted chat while websocket server logging was on: {refused}"
    )


# who may use the relay


def test_two_people_in_a_direct_message_exchange_packets_through_the_relay(pair):
    first, second = pair
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab, _tab(second) as guest_tab:
        assert _status(guest_tab, channel_id) == {"ok": True, "available": False, "session": None}

        host_ticket = _host(host_tab, channel_id)

        invitation = {"channel": channel_id, "session": host_ticket["session"]}
        status = _status(guest_tab, channel_id)
        assert status == {"ok": True, "available": True, "session": host_ticket["session"]}
        invited = wait_until(lambda: invitation in guest_tab.encryption_invitations, 30)
        assert invited, f"the other member was never told: {guest_tab.encryption_invitations}"
        guest_ticket = _join(host_tab, host_ticket, guest_tab)
        assert _send(host_tab, host_ticket, b"from the host")["ok"]
        assert _send(guest_tab, guest_ticket, b"from the guest")["ok"]
        guest_tab.encryption_event("mls")
        host_tab.encryption_event("mls")

    assert _received(guest_tab) == [_encoded(b"from the host")]
    assert _received(host_tab) == [_encoded(b"from the guest")]


def test_relayed_packets_are_never_stored(pair, instance):
    first, second = pair
    channel_id = _direct_message(first, second)
    marker = f"never-stored-{uuid.uuid4().hex}".encode()
    log_offset = instance.log_size()
    with _tab(first) as host_tab, _tab(second) as guest_tab:
        host_ticket = _host(host_tab, channel_id)
        _join(host_tab, host_ticket, guest_tab)
        assert _send(host_tab, host_ticket, marker)["ok"]
        arrived = wait_until(lambda: _encoded(marker) in _received(guest_tab), 30)
        assert arrived, "the packet never reached the guest"

    with first.client() as client:
        listed = client.get(f"/api/v1/channels/{channel_id}/messages")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [], f"the relayed packet became a channel message: {listed.json()}"
    traces = (marker.decode(), _encoded(marker))
    assert _rows_holding(instance, traces) == [], "a relayed packet was saved to the database"
    assert _redis_keys_holding(instance, traces) == [], "a relayed packet was saved to Redis"
    assert not any(trace in instance.log_since(log_offset) for trace in traces), (
        "a relayed packet was written to the server log"
    )


def _rows_holding(instance: LaunchedInstance, traces: tuple[str, ...]) -> list[str]:
    """Every table row that holds one of the traces, as `table: row`."""
    engine = sqlalchemy.create_engine(instance.database_url)
    found = []
    try:
        with engine.connect() as connection:
            tables = sqlalchemy.inspect(connection).get_table_names()
            assert "channel" in tables, tables
            for table in tables:
                for row in connection.execute(sqlalchemy.text(f'SELECT * FROM "{table}"')):
                    stored = repr(tuple(row))
                    if any(trace in stored for trace in traces):
                        found.append(f"{table}: {stored[:200]}")
    finally:
        engine.dispose()
    return found


def _redis_keys_holding(instance: LaunchedInstance, traces: tuple[str, ...]) -> list[str]:
    if not instance.redis_url:
        return []
    client = redis.Redis.from_url(instance.redis_url)
    found = []
    try:
        for key in client.scan_iter():
            stored = repr(_redis_value(client, key))
            if any(trace in stored for trace in traces):
                found.append(key.decode())
    finally:
        client.close()
    return found


def _redis_value(client: redis.Redis, key: bytes):
    kind = client.type(key).decode()
    readers = {
        "string": client.get,
        "hash": client.hgetall,
        "list": lambda name: client.lrange(name, 0, -1),
        "set": client.smembers,
        "zset": lambda name: client.zrange(name, 0, -1),
        "stream": client.xrange,
    }
    return readers[kind](key) if kind in readers else None


@pytest.mark.parametrize("refused_at", ["start", "join"])
def test_a_user_without_the_permission_cannot_start_or_join(refused_at, admin, make_user, relay_on):
    permitted, unpermitted = make_user(), make_user()
    make_group(admin, [permitted], PERMITTED)
    channel_id = _direct_message(permitted, unpermitted)
    with _tab(permitted) as permitted_tab, _tab(unpermitted) as unpermitted_tab:
        if refused_at == "start":
            answer = _relay(unpermitted_tab, "start", channel=channel_id)
        else:
            ticket = _host(permitted_tab, channel_id)
            assert _status(unpermitted_tab, channel_id)["ok"] is False
            answer = _relay(unpermitted_tab, "join", channel=channel_id, session=ticket["session"])
        _settle()

    assert answer["ok"] is False, (
        f"a user without the Encrypted Channels permission was let in at {refused_at}: {answer}"
    )
    assert permitted_tab.encryption_events == [], permitted_tab.encryption_events


def test_an_admin_needs_no_permission(admin, make_user, relay_on):
    other = make_user(role="admin")
    channel_id = _direct_message(other, make_user())

    with _tab(other) as host_tab:
        _host(host_tab, channel_id)


def test_someone_outside_the_conversation_cannot_reach_the_relay(admin, make_user, relay_on):
    first, second, outsider = make_user(), make_user(), make_user()
    make_group(admin, [first, second, outsider], PERMITTED)
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab, _tab(second) as guest_tab, _tab(outsider) as outsider_tab:
        host_ticket = _host(host_tab, channel_id)
        _join(host_tab, host_ticket, guest_tab)
        session = host_ticket["session"]
        answers = {
            "status": _status(outsider_tab, channel_id),
            "start": _relay(outsider_tab, "start", channel=channel_id),
            "join": _relay(outsider_tab, "join", channel=channel_id, session=session),
            "packet": _relay(
                outsider_tab,
                "packet",
                **host_ticket,
                kind="mls",
                packet=_encoded(b"forged"),
            ),
        }
        assert _send(host_tab, host_ticket, b"for members only")["ok"]
        _settle()

    let_in = {action: answer for action, answer in answers.items() if answer["ok"] is not False}
    assert let_in == {}, f"someone outside the direct message reached the relay: {let_in}"
    assert outsider_tab.encryption_events == [], outsider_tab.encryption_events
    assert outsider_tab.encryption_invitations == [], outsider_tab.encryption_invitations
    assert _encoded(b"forged") not in _received(guest_tab)


@pytest.mark.parametrize(
    ("kind", "form", "offered"),
    [
        ("private group", {"type": "group", "is_private": True}, True),
        ("public group", {"type": "group", "is_private": False}, False),
        ("standard channel", {}, False),
    ],
)
def test_only_direct_messages_and_private_groups_hold_encrypted_chats(
    kind, form, offered, admin, make_user, relay_on
):
    owner, member = make_user(role="admin"), make_user()
    channel_id = _channel(owner, [member], **form)

    with _tab(owner) as owner_tab:
        started = _relay(owner_tab, "start", channel=channel_id)

    assert started["ok"] is offered, f"starting an encrypted chat in a {kind}: {started}"


# what the relay carries


def test_a_packet_over_one_mebibyte_is_refused_and_not_forwarded(pair):
    first, second = pair
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab, _tab(second) as guest_tab:
        host_ticket = _host(host_tab, channel_id)
        _join(host_tab, host_ticket, guest_tab)
        before = len(_received(guest_tab))

        oversized = _send(host_tab, host_ticket, b"x" * (MAX_PACKET + 1))
        _settle()
        forwarded_oversized = len(_received(guest_tab)) - before
        largest = _send(host_tab, host_ticket, b"y" * MAX_PACKET)
        wait_until(lambda: len(_received(guest_tab)) > before, 30)

    assert oversized["ok"] is False, "the relay accepted a packet over 1 MiB"
    assert forwarded_oversized == 0, "the relay forwarded a packet over 1 MiB"
    assert largest["ok"] is True, f"the relay refused a packet of exactly 1 MiB: {largest}"
    assert _received(guest_tab)[before:] == [_encoded(b"y" * MAX_PACKET)]


# how a session ends


def test_a_guest_who_leaves_is_reported_to_the_host_and_the_chat_goes_on(pair):
    first, second = pair
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab, _tab(second) as guest_tab:
        host_ticket = _host(host_tab, channel_id)
        guest_ticket = _join(host_tab, host_ticket, guest_tab)

        left = _relay(guest_tab, "leave", **guest_ticket)
        departed = host_tab.encryption_event("departed")
        after_leaving = _send(guest_tab, guest_ticket, b"after leaving")
        status = _status(host_tab, channel_id)

    assert left["ok"], left
    assert departed["participation"] == guest_ticket["participation"], departed
    assert after_leaving["ok"] is False, "a guest who left could still send into the chat"
    assert status["available"] is True, f"the chat ended when a guest left: {status}"
    assert "ended" in [event["kind"] for event in guest_tab.encryption_events]
    assert "ended" not in [event["kind"] for event in host_tab.encryption_events]


@pytest.mark.parametrize("how", ["leaves", "disconnects"])
def test_the_chat_ends_for_everyone_when_the_host_goes(how, pair):
    first, second = pair
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab, _tab(second) as guest_tab:
        host_ticket = _host(host_tab, channel_id)
        _join(host_tab, host_ticket, guest_tab)

        if how == "leaves":
            assert _relay(host_tab, "leave", **host_ticket)["ok"]
        else:
            host_tab.client.disconnect()
        ended = _wait_ended(guest_tab)
        status = _status(guest_tab, channel_id)

    assert ended["session"] == host_ticket["session"], ended
    assert status == {"ok": True, "available": False, "session": None}, status
    assert {"channel": channel_id, "session": None} in guest_tab.encryption_invitations


def test_removing_someone_from_the_private_group_ends_the_chat(admin, make_user, relay_on):
    owner, guest, bystander = make_user(), make_user(), make_user()
    make_group(admin, [owner, guest], PERMITTED)
    channel_id = _private_group(owner, guest, bystander)
    with _tab(owner) as host_tab, _tab(guest) as guest_tab:
        host_ticket = _host(host_tab, channel_id)
        _join(host_tab, host_ticket, guest_tab)

        with owner.client() as client:
            removed = client.post(
                f"/api/v1/channels/{channel_id}/update/members/remove",
                json={"user_ids": [bystander.id]},
            )
        assert removed.status_code == 200, removed.text
        ended = {"host": _wait_ended(host_tab), "guest": _wait_ended(guest_tab)}
        after = _send(host_tab, host_ticket, b"after the member change")

    assert all(event["session"] == host_ticket["session"] for event in ended.values()), ended
    assert after["ok"] is False, "the chat went on after the group's members changed"


def test_switching_the_feature_off_ends_a_running_chat(admin, pair):
    first, second = pair
    channel_id = _direct_message(first, second)
    with _tab(first) as host_tab, _tab(second) as guest_tab:
        host_ticket = _host(host_tab, channel_id)
        _join(host_tab, host_ticket, guest_tab)

        _set_admin_config(admin, ENABLE_ENCRYPTED_CHANNELS=False)
        after = _send(host_tab, host_ticket, b"after the switch")
        ended = _wait_ended(guest_tab)

    assert after["ok"] is False, "the relay forwarded a packet after the admin switched it off"
    assert ended["session"] == host_ticket["session"], ended
    assert _encoded(b"after the switch") not in _received(guest_tab)
