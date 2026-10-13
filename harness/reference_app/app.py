"""The reference Open WebUI app: every oix feature behind a channel command or a mention word.

Runs in an environment of its own with `oix[fastapi,redis]` and `uvicorn` installed, started
by `harness.apps`. Open WebUI connects to it over Socket.IO (oix 0.8). Every verified event it
handles, and every API error it meets, is appended as a JSON line to `APP_RECORD_FILE`, so a
test reads what the app saw and did. Configuration comes from the environment: `API_KEY` and
`APP_PRIVATE_KEY_FILE` (read by oix), `APP_PORT`, `APP_PATH`, `APP_SCOPES`, `APP_NAME`,
`APP_MANIFEST_ID`, `APP_PROFILE_IMAGE` (a PNG file the manifest embeds), `APP_REDIS_URL` (routes
calls between replicas), `APP_SERVER` (`builtin` for `App.run`, `fastapi` for the router plus a
proactive-message route), `APP_MANIFEST` (`false` disables discovery), `APP_IGNORE_SELF`
(`false` hands the app its own messages) and `APP_ADMISSION` (`true` accepts a mention only once
its handler has stored it, oix's `background=False`).

Channel commands, in any message the app can read:
  !echo <text>     reply to the message           !send <text>    post in the channel or thread
  !thread <text>   start a thread on the message  !history        count the messages it can read
  !page            read two pages of one message  !channels       list the channels it joined
  !me              read its own installation      !progress       post, edit, then edit stale
  !buttons         post a message with buttons    !loop           answer its own messages once
  !tools           buttons for the other answers  !card           post a card with a link button
  !recard          post a card, swap it, clear it

Mention words, after `@App`. Open WebUI only learns that the app received a mention; whatever
the app says, it posts as an ordinary message:
  public  post in the channel       buttons  post a message with buttons     card  reply with a card
  later   post a result ~3 s later in the thread, after rechecking the execution token
  crash   raise in the handler      authorize  recheck the execution token   (else)  reply pong
With `APP_ADMISSION`, `refuse` raises and `stall` outlasts the acceptance budget instead.

Buttons: approve (update for all), whoami (private), reject (no handler of its own), noted (ack),
and on `!tools` report (a download), fail (an error), slow (exceed 2.5 s), card (a private card),
crash (raise), plus a link button that never reaches the app.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from oix import (
    Action,
    APIError,
    App,
    AppChannelReply,
    ConnectionLost,
    ConnectionUnavailable,
    Embed,
    EmbedField,
)

ALL_SCOPES = "channels:mentions:read,channels:messages:read,channels:messages:write,channels:read"
BUTTONS = [
    Action(action_id="approve", label="Approve", context={"step": 1}),
    Action(action_id="whoami", label="Who am I"),
    Action(action_id="reject", label="Reject", context={"reason": "unhandled"}),
    Action(action_id="noted", label="Noted"),
]
TOOLS = [
    Action(action_id="report", label="Report"),
    Action(action_id="fail", label="Fail"),
    Action(action_id="slow", label="Slow"),
    Action(action_id="card", label="Card"),
    Action(action_id="crash", label="Crash"),
    Action(label="Docs", url="https://docs.openwebui.com/"),
]
CARD = Embed(
    title="Advisory reported",
    url="https://example.com/advisories/1",
    description="Review **the** reported issue.",
    color=0xDA3633,
    author={"name": "Security Desk"},
    footer={"text": "Filed by the reference app"},
    thumbnail={"url": "https://example.com/thumbnail.png"},
    fields=[
        EmbedField(name="Severity", value="High", inline=True),
        EmbedField(name="Package", value="oix", inline=True),
        EmbedField(name="Summary", value="A field across the whole card"),
    ],
)
SWAPPED_CARD = Embed(title="Advisory resolved", description="Nothing left to do.")
LINK = Action(label="View Full Report", url="https://example.com/advisories/1")
LATER_SECONDS = 3.0
STALL_SECONDS = 12.0

app = App(
    manifest_id=os.environ.get("APP_MANIFEST_ID", "reference"),
    name=os.environ.get("APP_NAME", "Reference App"),
    description="The regression suite's reference app.",
    scopes=[scope for scope in os.environ.get("APP_SCOPES", ALL_SCOPES).split(",") if scope],
    profile_image_url=Path(os.environ["APP_PROFILE_IMAGE"])
    if os.environ.get("APP_PROFILE_IMAGE")
    else None,
    redis_url=os.environ.get("APP_REDIS_URL") or None,
    ignore_self_events=os.environ.get("APP_IGNORE_SELF", "true") == "true",
)


def record(kind: str, event=None, **details) -> None:
    entry = {"kind": kind, "at": time.time(), **details}
    if event is not None:
        entry.update(
            event_id=event.id,
            type=event.type,
            app_id=event.app_id,
            installation_id=event.installation_id,
            channel_id=event.channel_id,
            actor=event.actor,
            message=event.message,
            data=event.data,
        )
    with open(os.environ["APP_RECORD_FILE"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


async def run_command(event) -> None:
    command, _, argument = (event.content or "").strip().partition(" ")
    client = event.client
    if command == "!echo":
        await event.reply(f"echo: {argument}")
    elif command == "!send":
        await event.send(argument)
    elif command == "!thread":
        await client.create_message(event.channel_id, content=argument, parent_id=event.message_id)
    elif command == "!history":
        listed = await client.list_messages(event.channel_id, limit=100, parent_id=event.parent_id)
        await event.send(f"history: {len(listed['messages'])}")
    elif command == "!page":
        first = await client.list_messages(event.channel_id, limit=1)
        second = await client.list_messages(event.channel_id, limit=1, before=first["next_cursor"])
        ids = [page["messages"][0]["id"] for page in (first, second)]
        record("page", event, ids=ids)
        await event.send("page: " + ",".join(ids))
    elif command == "!channels":
        channels = await client.list_channels()
        record("channels", event, channels=channels)
        await event.send("channels: " + ",".join(sorted(item["name"] for item in channels)))
    elif command == "!me":
        installation = await client.get_me()
        await event.send(f"me: {installation['slug']}")
    elif command == "!progress":
        posted = await client.create_message(event.channel_id, content="Working on it...")
        await client.update_message(posted["id"], content="Done.", etag=posted["etag"])
        await client.update_message(posted["id"], content="Stale edit.", etag=posted["etag"])
    elif command == "!buttons":
        await event.send("Pick one", actions=BUTTONS)
    elif command == "!loop":
        await event.send("loop: first")
    elif command == "!tools":
        await event.send("Tools", actions=TOOLS)
    elif command == "!card":
        await event.send(embeds=[CARD], actions=[LINK])
    elif command == "!recard":
        posted = await client.create_message(event.channel_id, content="Card", embeds=[CARD])
        swapped = await client.update_message(
            posted["id"], content="Card", embeds=[SWAPPED_CARD], etag=posted["etag"]
        )
        record("recard", event, message_id=posted["id"], etag=swapped["etag"])


@app.on("channel.message.created")
async def message_created(event) -> None:
    record("message.created", event)
    author = (event.message or {}).get("author", {})
    if author.get("type") == "app":
        if event.content == "loop: first":
            await event.send("loop: second")
        return
    try:
        await run_command(event)
    except APIError as error:
        record("api_error", event, status=error.status, method=error.method, path=error.path)


@app.on("channel.message.updated")
async def message_updated(event) -> None:
    record("message.updated", event)


@app.on("channel.message.deleted")
async def message_deleted(event) -> None:
    record("message.deleted", event)


async def mentioned(event) -> None:
    record("mention", event)
    try:
        await answer_mention(event)
    except APIError as error:
        record("api_error", event, status=error.status, method=error.method, path=error.path)


async def answer_mention(event) -> None:
    words = event.content.split()
    if "public" in words:
        await event.send(f"public answer for {event.actor['id']}")
    elif "buttons" in words:
        await event.send("Choose", actions=BUTTONS)
    elif "card" in words:
        await event.reply(embeds=[CARD], actions=[LINK])
    elif "later" in words:
        await finish_later(event)
    elif "crash" in words:
        raise RuntimeError("the reference app crashed on purpose")
    elif "authorize" in words:
        verified = await event.client.authorize(event.execution_token)
        await event.reply(
            f"authorized {verified['actor']['id']} admin={verified['actor']['is_admin']}"
        )
    else:
        await event.reply(f"pong: {event.content}")


async def admit(event) -> None:
    """Durable acceptance: only store the mention; the receipt waits for this to return."""
    words = event.content.split()
    if "refuse" in words:
        raise RuntimeError("the reference app refused to store the mention")
    if "stall" in words:
        await asyncio.sleep(STALL_SECONDS)
    record("admitted", event)


if os.environ.get("APP_ADMISSION") == "true":
    app.on("channel.mention", background=False)(admit)
else:
    app.on("channel.mention")(mentioned)


async def finish_later(event) -> None:
    """Slow work after the receipt: recheck the asker's access, then publish the result."""
    await asyncio.sleep(LATER_SECONDS)
    try:
        verified = await event.client.authorize(event.execution_token)
        await event.client.create_message(
            event.channel_id,
            content=f"Job finished for {verified['actor']['name']}",
            parent_id=event.parent_id,
            reply_to_id=event.message_id,
        )
        record("later.finished", event)
    except APIError as error:
        record("later.refused", event, status=error.status)


@app.action("approve")
async def approve(event):
    record("action.approve", event)
    return AppChannelReply(
        type="update", visibility="channel", content=f"Approved by {event.actor['id']}"
    )


@app.action("whoami")
async def whoami(event):
    record("action.whoami", event)
    return f"you are {event.actor['id']} admin={event.actor['is_admin']}"


@app.action("noted")
async def noted(event):
    record("action.noted", event)
    return AppChannelReply(type="ack")


@app.action("report")
async def report(event):
    return AppChannelReply(
        type="download", title="Weekly report", filename="weekly-report.md", content="# Report"
    )


@app.action("fail")
async def fail(event):
    return AppChannelReply(type="error", content="It broke")


@app.action("slow")
async def slow(event):
    await asyncio.sleep(5)


@app.action("card")
async def card(event):
    return AppChannelReply(embeds=[CARD])


@app.action("crash")
async def crash(event):
    raise RuntimeError("the reference app crashed on purpose")


@app.on("channel.action")
async def any_action(event):
    record("action.generic", event)
    return f"unhandled {event.data['action_id']} {json.dumps(event.data['context'])}"


def serve_with_fastapi(port: int, path: str, manifest: bool) -> None:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from oix.fastapi import create_router

    server = FastAPI()
    server.include_router(create_router(app, path=path, manifest=manifest))

    @server.post("/proactive")
    async def proactive(form: dict):
        # a message nobody asked for, to an installation saved before a restart
        client = app.client(form["installation_id"])
        try:
            return await client.create_message(form["channel_id"], content=form["content"])
        except (ConnectionUnavailable, ConnectionLost) as error:
            return JSONResponse({"error": type(error).__name__}, status_code=503)

    uvicorn.run(
        server, host="127.0.0.1", port=port, log_level="warning", ws_max_size=16 * 1024 * 1024
    )


if __name__ == "__main__":
    port = int(os.environ["APP_PORT"])
    path = os.environ.get("APP_PATH", "/events")
    manifest = os.environ.get("APP_MANIFEST", "true") == "true"
    if os.environ.get("APP_SERVER", "builtin") == "fastapi":
        serve_with_fastapi(port, path, manifest)
    else:
        app.run(host="127.0.0.1", port=port, path=path, manifest=manifest)
