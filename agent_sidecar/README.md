# Agent sidecar live-lane boot recipe

How a lane boots the sidecar from the PR1 head of `LiveSparks/open-webui`, drives it over HTTP, and reads the evidence. The plan's boot recipe (`docs/plan/pi-engine-plan.md`) applies; this module adds the sidecar-specific steps. Nothing here modifies the existing 78 integration or 128 browser modules.

## What lives here

- `openai_fake.py` — the scripted OpenAI-shaped provider. Stdlib only. Replies are keyed on the prompt text (`ECHO:`, `TOOL_ECHO`, `COUNT_HISTORY`, `SLOW`, `LONG`), so a lane asserts the roundtrip it scripted. See the module docstring for the full script.
- `boot_sidecar.sh` — starts the fake, writes the scratch agent dir, starts the built sidecar, waits for `/health`. `stop` tears both down.

The sidecar holds no credentials: `models.json` points at the fake with a dummy key, and the sidecar never sees a real provider key.

## Boot a lane

From the parity suite checkout, against the open-webui checkout at the PR head:

```sh
agent_sidecar/boot_sidecar.sh start /path/to/open-webui 8930
# prints health.json content; sidecar serves http://127.0.0.1:8930
agent_sidecar/boot_sidecar.sh stop
```

`start` builds `sidecar/dist` first if it is missing (`npm ci --omit=dev && npm run build`), so a lane on a fresh worktree needs no hand steps. Scratch state lands in `/tmp/agent_sidecar-lane` (override with `AGENT_SIDECAR_LANE_DIR`).

## Drive the session lifecycle

```sh
# create or resume a chat's session
curl -s -X POST localhost:8930/sessions -d '{"chatId":"lane1"}'

# prompt: pi-native SSE; a reply frame then a done frame
curl -sN -X POST localhost:8930/sessions/lane1/prompt -d '{"message":"ECHO: hello lane"}'

# steer into an in-flight run, then abort it
curl -sN -X POST localhost:8930/sessions/lane1/prompt -d '{"message":"SLOW"}' &
curl -s -X POST localhost:8930/sessions/lane1/steer -d '{"message":"steered"}'
curl -s -X POST localhost:8930/sessions/lane1/abort
```

## Read the evidence

- `GET /health` returns `{ ok, version, pi, model, sessions }`; a wrong `x-agent-client-version` header gets a `409` handshake rejection.
- The prompt stream's `done` frame carries `{ reason, assistantText, sessionFile }`; `reason` is `completed`, `aborted`, or `error`.
- `ECHO:` prompts must answer `echoed: <payload>`; `COUNT_HISTORY` after a sidecar restart must report every earlier user message, which proves the JSONL resumed.
- Session JSONL files are at `/tmp/agent_sidecar-lane/agent_sessions/<chat_id>/*.jsonl`.
- Sidecar and fake logs are at `/tmp/agent_sidecar-lane/{sidecar,fake}.log`.
- The `echo` tool roundtrip: prompt `TOOL_ECHO ...`; the fake calls `echo`, the sidecar executes it, the next reply starts with `tool said: echo:fake-was-here:`, and the JSONL holds both the tool call and its result.
