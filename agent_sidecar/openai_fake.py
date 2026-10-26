"""A scripted OpenAI-shaped provider fake for the sidecar live lanes.

Standalone (stdlib only): `python agent_sidecar/openai_fake.py --port 8931`.
It speaks the Chat Completions wire the sidecar's pi ModelRuntime points at
through `models.json`. The reply is keyed on the prompt text, so a lane
asserts the roundtrip it scripted rather than a canned constant:

- `ECHO: <rest>` streams `echoed: <rest>` in three chunks
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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "fake-model"
CHUNK_DELAY = 0.05


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return json.dumps(content)


def _script_reply(body: dict) -> list[tuple[dict, str | None]]:
    """The scripted reply as (delta, finish_reason) pairs."""
    messages = body.get("messages") or []
    declared = [tool.get("function", {}).get("name") for tool in body.get("tools") or []]
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    prompt = _text(last_user.get("content"))

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

    tool_result = next((m for m in messages if m.get("role") == "tool"), None)
    if tool_result is not None:
        content = f"tool said: {_text(tool_result.get('content'))}"
        return [({"role": "assistant"}, None), ({"content": content}, "stop")]
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
        if not self.path.endswith("/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in _script_reply(body):
            chunk = {"object": "chat.completion.chunk", "model": MODEL_ID, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()
            if CHUNK_DELAY:
                threading.Event().wait(CHUNK_DELAY)
            if self.wfile.closed:
                return
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

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
