"""The reference Open WebUI app: every oix feature behind a channel command or a mention word.

Runs in an environment of its own with `oix[fastapi,redis]` and `uvicorn` installed, started
by `harness.apps`. Open WebUI connects to it over Socket.IO (oix 0.5). Every verified event it
handles, and every API error it meets, is appended as a JSON line to `APP_RECORD_FILE`, so a
test reads what the app saw and did. Configuration comes from the environment: `API_KEY` and
`APP_PRIVATE_KEY_FILE` (read by oix), `APP_PORT`, `APP_PATH`, `APP_SCOPES`, `APP_NAME`,
`APP_MANIFEST_ID`, `APP_PROFILE_IMAGE` (a PNG file the manifest embeds), `APP_REDIS_URL` (routes
calls between replicas), `APP_SERVER` (`builtin` for `App.run`, `fastapi` for the router plus a
proactive-message route), `APP_MANIFEST` (`false` disables discovery) and `APP_IGNORE_SELF`
(`false` hands the app its own messages).

Channel commands, in any message the app can read:
  !echo <text>     reply to the message           !send <text>    post in the channel or thread
  !thread <text>   start a thread on the message  !history        count the messages it can read
  !page            read two pages of one message  !channels       list the channels it joined
  !me              read its own installation      !progress       post, edit, then edit stale
  !buttons         post a message with buttons    !loop           answer its own messages once

Mention words, after `@App`:
  public  answer in the channel     buttons  channel message with buttons    report  a download
  defer   deferred work, ~2 s       fail     an error reply                  slow    exceed 2.5 s
  crash   raise in the handler      authorize  recheck the execution token   (else)  private pong
"""

import asyncio
import json
import os
import time
from pathlib import Path

from oix import Action, APIError, App, AppChannelReply, ConnectionLost, ConnectionUnavailable

ALL_SCOPES = "channels:mentions:read,channels:messages:read,channels:messages:write,channels:read"
BUTTONS = [
    Action(action_id="approve", label="Approve", context={"step": 1}),
    Action(action_id="whoami", label="Who am I"),
    Action(action_id="reject", label="Reject", context={"reason": "unhandled"}),
]
DEFER_SECONDS = 2.0

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
jobs: dict[str, float] = {}


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


@app.on("channel.mention")
async def mentioned(event):
    record("mention", event)
    words = event.content.split()
    if "public" in words:
        return AppChannelReply(
            visibility="channel", content=f"public answer for {event.actor['id']}"
        )
    if "buttons" in words:
        return AppChannelReply(visibility="channel", content="Choose", actions=BUTTONS)
    if "report" in words:
        return AppChannelReply(
            type="download", title="Weekly report", filename="weekly-report.md", content="# Report"
        )
    if "defer" in words:
        jobs[event.id] = time.monotonic() + DEFER_SECONDS
        return AppChannelReply(type="deferred", operation_id=event.id, content="Started")
    if "fail" in words:
        return AppChannelReply(type="error", content="It broke")
    if "slow" in words:
        await asyncio.sleep(5)
    if "crash" in words:
        raise RuntimeError("the reference app crashed on purpose")
    if "authorize" in words:
        verified = await event.client.authorize(event.execution_token)
        return f"authorized {verified['actor']['id']} admin={verified['actor']['is_admin']}"
    return f"pong: {event.content}"


@app.on("channel.operation.status")
async def operation_status(event):
    record("operation.status", event)
    finishes_at = jobs.get(event.id)
    if finishes_at is None:
        return AppChannelReply(type="error", content="Unknown job")
    if time.monotonic() < finishes_at:
        return AppChannelReply(type="deferred", operation_id=event.id, content="Still working")
    return AppChannelReply(title="Result", content="Job finished")


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

    uvicorn.run(server, host="127.0.0.1", port=port, log_level="warning", ws_max_size=1048576)


if __name__ == "__main__":
    port = int(os.environ["APP_PORT"])
    path = os.environ.get("APP_PATH", "/events")
    manifest = os.environ.get("APP_MANIFEST", "true") == "true"
    if os.environ.get("APP_SERVER", "builtin") == "fastapi":
        serve_with_fastapi(port, path, manifest)
    else:
        app.run(host="127.0.0.1", port=port, path=path, manifest=manifest)
