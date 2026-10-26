#!/usr/bin/env bash
# Boot the agent sidecar plus the scripted provider fake for a live lane.
#
#   agent_sidecar/boot_sidecar.sh start <open-webui-checkout> [port]
#   agent_sidecar/boot_sidecar.sh stop
#
# start: launches openai_fake.py on a scratch port, writes a scratch agent
# dir with models.json pointing at it, launches the sidecar's built dist on
# the given port (default 8930), waits for /health, then prints the env a
# lane needs. Pid and env files land in $AGENT_SIDECAR_LANE_DIR.
# stop: kills both and prints their last log lines.
set -euo pipefail

LANE_DIR=${AGENT_SIDECAR_LANE_DIR:-/tmp/agent_sidecar-lane}
SIDECAR_PORT=${3:-8930}
# Derive the fake's port from the sidecar port so concurrent lanes never collide
# (lane 1's sidecar port 8931 used to fight the hardcoded fake port 8931).
FAKE_PORT=$((18930 + (SIDECAR_PORT - 8930)))
HERE=$(cd "$(dirname "$0")" && pwd)

# The sidecar /health body is {ok:true, version, pi, ...}. Asserting the shape
# proves the responder is our sidecar, not another process on the port.
assert_sidecar_health() {
  node -e "
    const h = JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8'));
    if (h.ok !== true || typeof h.version !== 'string' || typeof h.pi !== 'string') process.exit(1);
  " "$LANE_DIR/health.json"
}

case "${1:-}" in
start)
  CHECKOUT=$(cd "$2" && pwd)
  mkdir -p "$LANE_DIR"
  python3 "$HERE/openai_fake.py" --port "$FAKE_PORT" > "$LANE_DIR/fake.log" 2>&1 &
  echo $! > "$LANE_DIR/fake.pid"

  for i in $(seq 1 50); do
    node -e "fetch('http://127.0.0.1:$FAKE_PORT/v1/models').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" && break
    sleep 0.2
  done

  if [ ! -x "$CHECKOUT/sidecar/dist/index.js" ]; then
    (cd "$CHECKOUT/sidecar" && npm ci && npm run build)
  fi

  mkdir -p "$LANE_DIR/agent_dir" "$LANE_DIR/agent_sessions"
  cat > "$LANE_DIR/agent_dir/models.json" <<EOF
{
  "providers": {
    "fake": {
      "baseUrl": "http://127.0.0.1:$FAKE_PORT/v1",
      "api": "openai-completions",
      "apiKey": "lane-dummy",
      "models": [
        {
          "id": "fake-model",
          "name": "Scripted Fake",
          "input": ["text"],
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
          "contextWindow": 32000,
          "maxTokens": 8192,
          "reasoning": false
        }
      ]
    }
  }
}
EOF
  echo '{}' > "$LANE_DIR/agent_dir/auth.json"

  env AGENT_SIDECAR_PORT=$SIDECAR_PORT \
    AGENT_SIDECAR_HOST=127.0.0.1 \
    AGENT_SIDECAR_AGENT_DIR="$LANE_DIR/agent_dir" \
    AGENT_SIDECAR_MODEL=fake/fake-model \
    AGENT_SIDECAR_CWD="$LANE_DIR" \
    AGENT_SESSIONS="$LANE_DIR/agent_sessions" \
    node "$CHECKOUT/sidecar/dist/index.js" > "$LANE_DIR/sidecar.log" 2>&1 &
  echo $! > "$LANE_DIR/sidecar.pid"

  for i in $(seq 1 50); do
    if curl -fsS "http://127.0.0.1:$SIDECAR_PORT/health" > "$LANE_DIR/health.json" 2>/dev/null && assert_sidecar_health; then
      break
    fi
    sleep 0.2
  done
  if ! curl -fsS "http://127.0.0.1:$SIDECAR_PORT/health" > "$LANE_DIR/health.json" || ! assert_sidecar_health; then
    echo "sidecar did not answer /health with the expected body shape"; tail -5 "$LANE_DIR/sidecar.log"; exit 1
  fi

  echo "sidecar: http://127.0.0.1:$SIDECAR_PORT (fake on $FAKE_PORT)"
  echo "sessions: $LANE_DIR/agent_sessions  logs: $LANE_DIR/{sidecar,fake}.log"
  ;;
stop)
  for name in sidecar fake; do
    if [ -f "$LANE_DIR/$name.pid" ]; then
      kill "$(cat "$LANE_DIR/$name.pid")" 2>/dev/null || true
      rm -f "$LANE_DIR/$name.pid"
    fi
  done
  ;;
*)
  echo "usage: $0 start <open-webui-checkout> [port] | $0 stop" >&2
  exit 2
  ;;
esac
