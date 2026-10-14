"""Dependency smoke: how responses and live updates travel, each library through its feature.

starlette-compress compresses a response in the encoding the client asks for, with Brotli,
gzip and zstandard doing the work, and leaves a small response, or one for a client that names
no encoding, as it is (twin of unit/deps/test_starlette_compress.py). A response sent whole goes
through `brotli.compress`, a file streamed in parts through a `brotli.Compressor`.
/api/changelog is a large public response that shows it, and its body is CHANGELOG.md turned
into HTML by Markdown (bold text and links included) and split into versions and items by
BeautifulSoup, each item cut at its first ": " into a title and its text.
aiohttp decodes a Brotli-encoded provider reply with brotlicffi (Brotli when that is absent).
python-socketio carries the chat events to the browser: over a websocket by default, and over
HTTP long polling alone when `ENABLE_WEBSOCKET_SUPPORT` is off, which then refuses a websocket
(e2e/chat/test_long_polling_chat.py shows the web client streaming a reply that way). Its event
calls to a tab, the disconnects of a changed account, rooms left when access is revoked and the
Redis manager between instances are driven in integration/chat/test_socket_runtime.py,
integration/security/test_revoked_access_leaves_live_rooms.py,
integration/deps/test_redis_stack.py and integration/chat/test_socket_delivery_across_instances.py.
pycrdt merges the live edits two tabs make to one note: a long edit still arrives whole after the
server has folded its oldest updates into one snapshot, and an update that arrives twice counts
once.

Discriminates: passes on dev bbfa876af; in a backend copy, dropping `CompressMiddleware` fails
every encoding, `CompressMiddleware` with a `minimum_size` of 1 compresses the health check and
one that treats a request without `Accept-Encoding` as asking for gzip compresses the plain
changelog, a `brotli.Compressor` that emits nothing (patched in at import) fails the
streamed download and the Brotli changelog, skipping `markdown.markdown` empties the changelog,
an item whose content is its raw HTML fails the changelog test, a provider session with
`auto_decompress=False` hands the Brotli bytes to the stream parser (as does a brotlicffi
`Decompressor` patched to garble), emitting chat events to a room other than `user:{id}` starves
the socket of them and not applying the stored updates before `ydoc.get_update()` sends the
second tab an empty document. On dev ef67cc3fa, a compaction that keeps an empty snapshot fails
the long edit, and a merge that appends each stored update's text fails the repeated update.
A Socket.IO server that keeps the websocket transport whatever the setting fails both long
polling tests. Twin of unit/deps/test_pycrdt.py and unit/deps/test_python_socketio.py.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from typing import Iterator

import brotli
import httpx
import pycrdt
import pytest
import socketio

from harness import raw_provider
from harness import upstream as reply
from harness.actors import create_user
from harness.chat import ask
from harness.instance import resolve_backend
from harness.raw_provider import RAW_MODEL_ID, chunk, sse
from harness.second_provider import OPENAI_CONFIG
from harness.socket_client import SocketSession, connected

pytestmark = [pytest.mark.depcheck, pytest.mark.api, pytest.mark.requires_source]

VERSION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\] - (?P<date>.+)$", re.MULTILINE)
SECTION_HEADING = re.compile(r"^### (?P<section>.+)$", re.MULTILINE)
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
STATE_WAIT = 10.0


def _changelog(instance, encoding: str) -> httpx.Response:
    with httpx.Client(base_url=instance.base_url, timeout=60.0) as client:
        return client.get("/api/changelog", headers={"Accept-Encoding": encoding})


@pytest.mark.parametrize("encoding", ["br", "gzip", "zstd"])
def test_the_changelog_is_compressed_in_the_encoding_asked_for(instance, encoding):
    plain = _changelog(instance, "identity")
    compressed = _changelog(instance, encoding)

    assert compressed.status_code == 200, compressed.text
    assert compressed.headers.get("content-encoding") == encoding
    assert compressed.num_bytes_downloaded < plain.num_bytes_downloaded
    assert compressed.json() == plain.json()


def test_a_small_response_is_sent_plain_even_to_a_client_that_accepts_gzip(instance):
    with httpx.Client(base_url=instance.base_url, timeout=60.0) as client:
        health = client.get("/health", headers={"Accept-Encoding": "gzip"})

    assert health.status_code == 200, health.text
    assert "content-encoding" not in health.headers
    assert health.json() == {"status": True}


def test_a_client_that_names_no_encoding_gets_the_body_plain(instance):
    with httpx.Client(base_url=instance.base_url, timeout=60.0) as client:
        request = client.build_request("GET", "/api/changelog")
        del request.headers["Accept-Encoding"]
        answered = client.send(request)

    assert answered.status_code == 200, answered.text
    assert "content-encoding" not in answered.headers
    assert answered.num_bytes_downloaded == len(answered.content) > 10_000


def test_a_large_file_download_is_streamed_brotli_compressed(make_user):
    # past one 64 KiB read, so the file leaves in several parts
    logbook = "".join(f"Tide {index}: high water at the harbour mouth.\n" for index in range(8000))
    with make_user().client() as client:
        uploaded = client.post(
            "/api/v1/files/",
            params={"process": "false"},
            files={"file": ("logbook.txt", logbook.encode(), "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        downloaded = client.get(
            f"/api/v1/files/{uploaded.json()['id']}/content", headers={"Accept-Encoding": "br"}
        )

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers.get("content-encoding") == "br"
    assert downloaded.num_bytes_downloaded < len(logbook) // 5
    assert downloaded.text == logbook


def _changelog_source() -> str:
    """The CHANGELOG.md the server reads: the checkout's, else the one packaged with it."""
    backend = resolve_backend()
    for candidate in (backend.parent / "CHANGELOG.md", backend / "open_webui" / "CHANGELOG.md"):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise AssertionError(f"no CHANGELOG.md next to {backend}")


def test_the_changelog_is_read_from_the_markdown(instance):
    source = _changelog_source()
    headings = list(VERSION_HEADING.finditer(source))
    newest_body = source[headings[0].end() : headings[1].start()]

    changelog = _changelog(instance, "identity").json()

    assert list(changelog) == [heading["version"] for heading in headings[:5]]
    assert [entry["date"] for entry in changelog.values()] == [
        heading["date"].strip() for heading in headings[:5]
    ]
    newest = changelog[headings[0]["version"]]
    sections = [match["section"].strip().lower() for match in SECTION_HEADING.finditer(newest_body)]
    assert [name for name in newest if name != "date"] == sections
    items = [item for name in sections for item in newest[name]]
    assert items and all(item["raw"].startswith("<li>") for item in items)
    assert any("<strong>" in item["raw"] for item in items), "the bold markdown was not rendered"
    rendered = "".join(item["raw"] for item in items)
    links = MARKDOWN_LINK.findall(newest_body)
    assert links, "the newest release links nothing; retarget the link check"
    unrendered = [
        f"[{text}]({url})" for text, url in links if f'<a href="{url}">{text}</a>' not in rendered
    ]
    assert not unrendered, f"links left as markdown: {unrendered[:3]}"
    # each item's text is split at its first ": " into a title and the rest
    assert all("<strong>" not in item["title"] + item["content"] for item in items)
    for item in items:
        text = re.sub(r"<[^>]+>", "", item["raw"])
        if ": " in text:
            assert item["title"] and ": " not in item["title"], item
        else:
            assert item["title"] == "", item
        assert item["content"], item


@pytest.fixture
def raw(admin, preserve, listener) -> raw_provider.RawProvider:
    preserve(OPENAI_CONFIG)
    return raw_provider.connect(admin, listener)


def test_a_brotli_encoded_provider_reply_is_decoded(admin, raw, listener):
    body = sse(chunk({"role": "assistant", "content": ""}), chunk({"content": "decoded"}))
    headers = {"Content-Type": "text/event-stream", "Content-Encoding": "br"}
    listener.route("POST", "/v1/chat/completions", (200, headers, brotli.compress(body)))

    with admin.client() as client:
        _, message = ask(client, "hello?", model=RAW_MODEL_ID)

    sent = listener.requests_to("/v1/chat/completions")[-1]
    assert "br" in sent.headers.get("Accept-Encoding", ""), "the provider was not offered Brotli"
    assert message["content"] == "decoded"


def test_a_socket_joins_its_account_and_receives_the_chat_events(make_user, upstream):
    account = make_user()
    upstream.queue(reply.text("over the socket"))

    with connected(account) as socket, account.client() as client:
        joined = socket.client.call("user-join", {"auth": {"token": account.token}}, timeout=30)
        turn, _ = ask(client, "hello?")
        socket.wait_for(turn.chat_id, "chat:completion", done=True)
        pushed = json.dumps(socket.events_of(turn.chat_id))

    assert joined == {"id": account.id, "name": account.name}
    assert "over the socket" in pushed, "the streamed reply never reached the socket"


def _text_of(state: list[int]) -> str:
    document = pycrdt.Doc()
    document.apply_update(bytes(state))
    return str(document.get("content", type=pycrdt.Text))


def _edit(text: str) -> list[int]:
    """The update a tab sends after typing `text` into an empty document."""
    document = pycrdt.Doc()
    document["content"] = content = pycrdt.Text()
    content += text
    return list(document.get_update())


@contextlib.contextmanager
def _document_tab(account, document_id: str) -> Iterator[tuple[SocketSession, list[list[int]]]]:
    """A socket joined to the note's live document, keeping every state the server sends it."""
    with connected(account) as socket:
        states: list[list[int]] = []
        socket.client.on("ydoc:document:state", lambda payload: states.append(payload["state"]))
        socket.call("ydoc:document:join", {"document_id": document_id})
        yield socket, states


def _a_state_reads(states: list[list[int]], text: str) -> bool:
    deadline = time.monotonic() + STATE_WAIT
    while time.monotonic() < deadline:
        if any(_text_of(state) == text for state in list(states)):
            return True
        time.sleep(0.05)
    return False


def test_two_tabs_editing_one_note_see_the_document_the_server_merged(make_user):
    account = make_user()
    with account.client() as client:
        note = client.post(
            "/api/v1/notes/create", json={"title": "shared", "data": {"content": {"md": ""}}}
        )
    assert note.status_code == 200, note.text
    document_id = f"note:{note.json()['id']}"

    with (
        _document_tab(account, document_id) as (first, _),
        _document_tab(account, document_id) as (second, states),
    ):
        first.call("ydoc:document:update", {"document_id": document_id, "update": _edit("hi")})
        second.call("ydoc:document:state", {"document_id": document_id})

        assert _a_state_reads(states, "hi"), "the second tab never got the merged document"


def _typing(text: str) -> list[list[int]]:
    """The updates a tab sends while `text` is typed into an empty note, one per character."""
    document = pycrdt.Doc()
    document["content"] = content = pycrdt.Text()
    updates = []
    for character in text:
        before = document.get_state()
        content += character
        updates.append(list(document.get_update(before)))
    return updates


def _live_note(account) -> str:
    with account.client() as client:
        note = client.post(
            "/api/v1/notes/create", json={"title": "typed", "data": {"content": {"md": ""}}}
        )
    assert note.status_code == 200, note.text
    return f"note:{note.json()['id']}"


def test_a_long_edit_survives_the_servers_compaction_of_its_updates(make_user):
    # past 500 stored updates the server merges the oldest half into one
    account = make_user()
    document_id = _live_note(account)
    text = "".join(f"tide {index:03d}. " for index in range(52))
    assert len(text) > 500

    with _document_tab(account, document_id) as (typist, _):
        for update in _typing(text):
            typist.call("ydoc:document:update", {"document_id": document_id, "update": update})
        with _document_tab(account, document_id) as (_, states):
            assert _a_state_reads(states, text), "the note came back without its early edits"


def test_an_update_sent_twice_is_applied_once(make_user):
    account = make_user()
    document_id = _live_note(account)
    edit = {"document_id": document_id, "update": _edit("hi")}

    with _document_tab(account, document_id) as (typist, _):
        typist.call("ydoc:document:update", edit)
        typist.call("ydoc:document:update", edit)
        with _document_tab(account, document_id) as (_, states):
            assert _a_state_reads(states, "hi"), "the repeated update was applied again"


@pytest.fixture(scope="module")
def long_polling(instance_with):
    """An instance with websockets switched off, as behind a proxy that cannot pass them."""
    return instance_with({"ENABLE_WEBSOCKET_SUPPORT": "false"})


@pytest.mark.slow
def test_with_websockets_off_a_socket_gets_its_chat_events_over_long_polling(long_polling):
    account = create_user(long_polling)
    long_polling.upstream.queue(reply.text("over long polling"))

    with connected(account, transports=("polling",)) as socket, account.client() as client:
        joined = socket.client.call("user-join", {"auth": {"token": account.token}}, timeout=30)
        turn, _ = ask(client, "hello?")
        socket.wait_for(turn.chat_id, "chat:completion", done=True)
        pushed = json.dumps(socket.events_of(turn.chat_id))
        transport = socket.client.transport()

    assert joined == {"id": account.id, "name": account.name}
    assert transport == "polling"
    assert "over long polling" in pushed, "the streamed reply never reached the socket"


@pytest.mark.slow
def test_with_websockets_off_a_websocket_is_refused(long_polling):
    with pytest.raises(socketio.exceptions.ConnectionError):
        with connected(create_user(long_polling)):
            pass
