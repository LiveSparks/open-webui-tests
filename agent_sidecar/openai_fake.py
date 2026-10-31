"""A scripted OpenAI-shaped provider fake for the sidecar live lanes.

Standalone (stdlib only): `python agent_sidecar/openai_fake.py --port 8931`.
It speaks the Chat Completions wire the sidecar's pi ModelRuntime points at
through `models.json`. The reply is keyed on the prompt text, so a lane
asserts the roundtrip it scripted rather than a canned constant:

- `ECHO: <rest>` streams `echoed: <rest>` in three chunks
- `TOOL: <name> :: <json>` makes the fake call the declared tool `<name>` with
  those arguments (the loopback bridge's tool calls use the pi name,
  `mcp__owui__<tool>`); a tool result in the request then answers
  `tool said: <content>`
- `TOOL_CHAIN: <name> :: <json> -> <name2> :: <json2>` calls the tools in
  order, one per tool result that came back, and answers
  `chained: <last content>` after the last one lands (tool_search discovery
  before a deferred bridge tool)
- `TOOL_ECHO ...` makes the fake call the `echo` tool when one is declared
- a tool result in the request answers `tool said: <content>`
- `COUNT_HISTORY` answers `history: <n> user messages`
- `SLOW` streams sixty chunks fifty milliseconds apart (abort mid-generation)
- `LONG` streams two hundred numbered chunks (frame-loss checks)
- anything else answers `echoed: <prompt>` in one chunk
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "fake-model"
CHUNK_DELAY = 0.05


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return json.dumps(content)


def _tool_call(name: str, arguments: str) -> dict:
    return {
        "index": 0,
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }


def _declared(declared: list[str | None], name: str) -> bool:
    return any(candidate == name for candidate in declared)


def _split_steps(text: str) -> list[str]:
    """Top-level `->` splits only: quotes and brackets nest, and a delegated
    task carries its own TOOL_CHAIN through the arguments JSON."""
    steps: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    start = 0
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        elif depth == 0 and char == "-" and text[index : index + 2] == "->":
            steps.append(text[start:index])
            start = index + 2
    steps.append(text[start:])
    return [step.strip() for step in steps if step.strip()]


def _script_reply(body: dict) -> list[tuple[dict, str | None]]:
    """The scripted reply as (delta, finish_reason) pairs."""
    messages = body.get("messages") or []
    declared = [tool.get("function", {}).get("name") for tool in body.get("tools") or []]
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    # A browser composer turns straight quotes into curly ones; the scripted
    # JSON arguments need the straight form back.
    prompt = _text(last_user.get("content")).replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'").replace("\u2192", "->")

    if "COUNT_HISTORY" in prompt:
        users = [m for m in messages if m.get("role") == "user"]
        return [({"role": "assistant"}, None), ({"content": f"history: {len(users)} user messages"}, "stop")]
    if prompt.startswith("SLOW"):
        frames = [({"role": "assistant"}, None)]
        frames += [({"content": f"chunk{i}"}, None) for i in range(1, 61)]
        frames += [({"content": ""}, "stop")]
        return frames
    if prompt.startswith("LONG"):
        frames = [({"role": "assistant"}, None)]
        frames += [({"content": f"frame-{i} "}, None) for i in range(200)]
        frames += [({"content": ""}, "stop")]
        return frames

    # The chat prompt head injects an <attached_files> block ahead of the
    # user's text; anchor the scripted markers wherever they sit.
    if "TOOL_CHAIN:" in prompt:
        prompt = prompt[prompt.index("TOOL_CHAIN:"):]
    if "TOOL:" in prompt and not prompt.startswith("TOOL"):
        prompt = prompt[prompt.index("TOOL:"):]
    if prompt.startswith("TOOL_CHAIN:"):
        steps = [
            (name.strip(), arguments.strip())
            for step in _split_steps(prompt.removeprefix("TOOL_CHAIN:"))
            for name, _, arguments in [step.partition("::")]
        ]
        # Tool results that came back after the latest user message are the
        # steps already run; the next step is the one not answered yet.
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        done = len([m for m in messages[last_user:] if m.get("role") == "tool"])
        if done >= len(steps):
            trailing = messages[last_user:]
            last_content = _text(trailing[-1].get("content")) if trailing else ""
            return [({"role": "assistant"}, None), ({"content": f"chained: {last_content}"}, "stop")]
        name, arguments = steps[done]
        if _declared(declared, name):
            return [
                ({"role": "assistant"}, None),
                ({"tool_calls": [_tool_call(name, arguments)]}, "tool_calls"),
            ]
        return [({"role": "assistant"}, None), ({"content": f"tool {name} was not offered"}, "stop")]
    # The after-call leg is the one carrying a tool result after the latest
    # user message: earlier turns' results sit in the history of every
    # request a multi-turn chat makes, and only the trailing one is pending.
    last_user_index = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    trailing = [m for m in messages[last_user_index + 1:] if m.get("role") == "tool"]
    if trailing:
        content = f"tool said: {_text(trailing[-1].get('content'))}"
        return [({"role": "assistant"}, None), ({"content": content}, "stop")]
    if prompt.startswith("TOOL:"):
        name, _, arguments = prompt.removeprefix("TOOL:").partition("::")
        name = name.strip()
        if _declared(declared, name):
            return [
                ({"role": "assistant"}, None),
                ({"tool_calls": [_tool_call(name, arguments.strip())]}, "tool_calls"),
            ]
        return [({"role": "assistant"}, None), ({"content": f"tool {name} was not offered"}, "stop")]
    if "echo" in declared and "TOOL_ECHO" in prompt:
        call = {
            "index": 0,
            "id": "call_fake_1",
            "type": "function",
            "function": {"name": "echo", "arguments": json.dumps({"message": f"fake-was-here:{prompt}"})},
        }
        return [({"role": "assistant"}, None), ({"tool_calls": [call]}, "tool_calls")]

    payload = prompt[6:] if prompt.startswith("ECHO: ") else prompt
    reply = f"echoed: {payload}"
    third = max(1, len(reply) // 3)
    return [
        ({"role": "assistant"}, None),
        ({"content": reply[:third]}, None),
        ({"content": reply[third : 2 * third]}, None),
        ({"content": reply[2 * third :]}, "stop"),
    ]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the agent hung up mid-reply, which is what abort looks like

    def do_GET(self) -> None:
        self._json(200, {"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw) if raw else {}
        received = int(time.time() * 1000)
        if not self.path.endswith("/chat/completions"):
            self._log_request(body, received, [])
            self._json(404, {"error": "not found"})
            return
        chunk_ts: list[int] = []
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in _script_reply(body):
                chunk = {"object": "chat.completion.chunk", "model": MODEL_ID, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                chunk_ts.append(int(time.time() * 1000))
                if CHUNK_DELAY:
                    threading.Event().wait(CHUNK_DELAY)
                if self.wfile.closed:
                    return
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        finally:
            self._log_request(body, received, chunk_ts)

    def _log_request(self, body: dict, received: int, chunk_ts: list[int]) -> None:
        """One line per provider request: the declared tools, the last user
        message, the role shape, the receipt time, and each chunk's send time
        (the source side of the round's frame-latency probe)."""
        if not os.environ.get("OPENAI_FAKE_LOG"):
            return
        with open(os.environ["OPENAI_FAKE_LOG"], "a") as log:
            names = [t.get("function", {}).get("name") for t in body.get("tools") or []]
            log.write(json.dumps({"tools": names, "last_user": _text(next((m for m in reversed(body.get("messages") or []) if m.get("role") == "user"), {}).get("content")), "roles": [m.get("role") for m in body.get("messages") or []], "ts": received, "chunk_ts": chunk_ts}) + "\n")

    def _json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8931)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"scripted provider on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
