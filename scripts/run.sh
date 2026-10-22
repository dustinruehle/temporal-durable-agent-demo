#!/usr/bin/env bash
#
# Launch the demo. Default is fully local with no keys.
#
#   scripts/run.sh                 local: dev server (with the Standalone Activities
#                                  flag), worker, gateway, dashboard, both harnesses
#   scripts/run.sh --workers 2     add a second model-calls replica for beat 7
#   scripts/run.sh --cloud         read .env and connect to Cloud (no dev server)
#
# Every process prints its resolved target, mode, and role on startup. No em dashes.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/src"

MODE="local"
WORKERS=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --local) MODE="local" ;;
    --cloud) MODE="cloud" ;;
    --workers) WORKERS="$2"; shift ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
  shift
done

# Load .env if present, for Cloud config and switches. Anything already exported
# in the calling shell wins, so `MODEL_MODE=mock ./scripts/run.sh` runs the mock
# even when .env is set up for a live provider.
if [ -f "$REPO_ROOT/.env" ]; then
  CALLER_ENV="$(export -p)"
  set -a; . "$REPO_ROOT/.env"; set +a
  eval "$CALLER_ENV"
fi

# --local forces a local connection even when .env carries Cloud config, so the
# local dev server is actually used. Switches like MODEL_MODE still come from
# .env, so you can run a real model against a local server. --cloud keeps the
# Cloud connection from .env.
if [ "$MODE" = "local" ]; then
  unset TEMPORAL_ADDRESS TEMPORAL_NAMESPACE TEMPORAL_API_KEY
  export TEMPORAL_TARGET=local
  # The local dev server registers NextFollowUpAt below, so sessions can publish it.
  # On Cloud, register it once and pass FOLLOW_UP_SEARCH_ATTRIBUTE=on.
  export FOLLOW_UP_SEARCH_ATTRIBUTE="${FOLLOW_UP_SEARCH_ATTRIBUTE:-on}"
fi

# Outbound messages leave through the gateway's local stub unless the operator
# points them elsewhere. This is what makes beat 5's follow-up leave through the
# same channel a customer would receive it on.
export OUTBOUND_URL="${OUTBOUND_URL:-http://127.0.0.1:8090/outbound}"

PIDS=()
CLEANED=0
cleanup() {
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  echo ""
  echo "shutting down..."
  # Stop the Workers and the other processes first, and wait for them, so no
  # Worker is left polling a dev server that is already gone.
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" >/dev/null 2>&1 || true; done
  for _ in $(seq 1 40); do
    alive=0
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do
      if kill -0 "$pid" >/dev/null 2>&1; then alive=1; fi
    done
    [ "$alive" = 0 ] && break
    sleep 0.25
  done
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  echo "stopped."
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "=== resolved switches ==="
uv run python scripts/print_switches.py || true
echo ""

if [ "$MODE" = "local" ]; then
  echo "starting local dev server with the Standalone Activities flag..."
  echo "  temporal server start-dev --dynamic-config-value activity.enableStandalone=true"
  temporal server start-dev \
    --dynamic-config-value activity.enableStandalone=true \
    --search-attribute NextFollowUpAt=Datetime \
    --log-level error &
  SERVER_PID=$!
  # Wait for health.
  for _ in $(seq 1 40); do
    if temporal operator cluster health >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
  echo "dev server ready on localhost:7233 (Web UI on http://localhost:8233)"
else
  echo "cloud mode: connecting via .env (TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, creds)"
fi

# Session worker plus one model-calls replica.
uv run python scripts/worker.py &
PIDS+=($!)
# Extra model-calls replicas for beat 7.
i=2
while [ "$i" -le "$WORKERS" ]; do
  uv run python scripts/worker.py --replica-only &
  PIDS+=($!)
  i=$((i + 1))
done

# Channel gateway (the seam a real channel attaches to).
uv run python scripts/gateway.py &
PIDS+=($!)

# Stage 0 and stage 1 harness feeds, so the dashboard can show every stage.
HARNESS_PORT=8091 uv run python -m harness_plain.app &
PIDS+=($!)
HARNESS_PORT=8092 uv run python -m harness_wrapped.app &
PIDS+=($!)

# Dashboard.
uv run uvicorn dashboard.app:app --host 127.0.0.1 --port 8080 --log-level warning &
PIDS+=($!)

sleep 1
echo ""
echo "=== up ==="
echo "  dashboard:       http://127.0.0.1:8080/?conversation=smoke-1"
echo "  gateway inbound: POST http://127.0.0.1:8090/inbound"
echo "  temporal web ui: http://localhost:8233 (local mode)"
echo "  model-calls replicas: $WORKERS"
echo ""
echo "smoke check (proves gateway, client, worker, workflow all live):"
echo "  curl -s -X POST http://127.0.0.1:8090/inbound -H 'content-type: application/json' \\"
echo "    -d '{\"conversation\":\"smoke-1\",\"text\":\"I am looking to buy a car under 15k\",\"scenario\":\"happy\"}'"
echo ""
echo "Ctrl-C to stop everything."
wait
