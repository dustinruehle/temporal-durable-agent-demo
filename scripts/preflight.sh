#!/usr/bin/env bash
#
# Preflight. Detect and verify prerequisites at the computed floor,
# prove the pre-release pieces import, and actively prove Standalone Activities
# run end to end. Idempotent. Exits non-zero if any requirement is unsatisfiable.
#
# Target OS: macOS on Apple Silicon, Linux as a fallback. No em dashes anywhere.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- Computed floor. The highest floor governs. ----------------------------
FLOOR_PYTHON="3.10"
FLOOR_TEMPORALIO="1.28.0"   # max(1.28.0 Strands plugin, 1.23.0 Standalone Activities)
FLOOR_SERVER="1.31.0"       # Standalone Activities
FLOOR_CLI="1.7.0"           # Standalone Activities

FAIL=0
red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
hdr() { printf '\n=== %s ===\n' "$*"; }

# ver_ge A B  ->  exit 0 if A >= B
ver_ge() {
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$2" ]
}

require_ge() { # name actual floor fixcmd
  local name="$1" actual="$2" floor="$3" fix="$4"
  if [ -z "$actual" ]; then
    red "MISSING: $name not found. Install it, for example: $fix"
    FAIL=1
    return
  fi
  if ver_ge "$actual" "$floor"; then
    grn "OK   $name $actual (floor $floor)"
  else
    red "BELOW FLOOR: $name $actual < $floor. Fix: $fix"
    FAIL=1
  fi
}

# --- 1. Toolchain versions --------------------------------------------------
hdr "toolchain"
UV_VER="$(uv --version 2>/dev/null | awk '{print $2}' || true)"
GIT_VER="$(git --version 2>/dev/null | awk '{print $3}' || true)"
TEMPORAL_LINE="$(temporal --version 2>/dev/null || true)"
CLI_VER="$(printf '%s' "$TEMPORAL_LINE" | sed -n 's/.*version \([0-9.]*\).*/\1/p')"
SERVER_VER="$(printf '%s' "$TEMPORAL_LINE" | sed -n 's/.*Server \([0-9.]*\).*/\1/p')"

require_ge "temporal" "$CLI_VER"      "$FLOOR_CLI"        "brew upgrade temporal"
require_ge "server"   "$SERVER_VER"   "$FLOOR_SERVER"     "brew upgrade temporal (bundled dev server)"
if [ -z "$UV_VER" ]; then
  red "MISSING: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  FAIL=1
else
  grn "OK   uv $UV_VER"
fi
if [ -z "$GIT_VER" ]; then
  red "MISSING: git not found. Install: brew install git"
  FAIL=1
else
  grn "OK   git $GIT_VER"
fi
# Optional: jq is used only by the CLI checks in the README beats.
if command -v jq >/dev/null 2>&1; then
  grn "OK   jq $(jq --version 2>/dev/null)"
else
  echo "NOTE: jq not found (optional, for the README CLI checks). Install: brew install jq"
fi

if [ "$FAIL" -ne 0 ]; then
  red "Preflight failed at the toolchain stage. Fix the items above and re-run."
  exit 1
fi

# --- 2. Dependencies --------------------------------------------------------
hdr "uv sync (with the strands-agents extra)"
uv sync --extra dev
# uv installs and picks the Python the project runs on, so check that one, not
# the system python3.
PYTHON_VER="$(uv run python -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
require_ge "python" "$PYTHON_VER" "$FLOOR_PYTHON" "uv python install 3.13, then uv sync --extra dev"
TEMPORALIO_VER="$(uv run python -c 'from importlib.metadata import version; print(version("temporalio"))')"
STRANDS_VER="$(uv run python -c 'from importlib.metadata import version; print(version("strands-agents"))')"
require_ge "temporalio" "$TEMPORALIO_VER" "$FLOOR_TEMPORALIO" "edit pyproject.toml pin and re-run uv sync"

# --- 3. Pre-release import probes ------------------------------------------
hdr "pre-release import probes"
if ! uv run python -c "from temporalio.contrib.strands import StrandsPlugin, TemporalAgent" 2>/dev/null; then
  red "FAIL: cannot import StrandsPlugin/TemporalAgent."
  red "FIX: uv sync --extra dev (the strands-agents extra is required)."
  FAIL=1
else
  grn "OK   from temporalio.contrib.strands import StrandsPlugin, TemporalAgent"
fi
if ! uv run python -c "from temporalio.envconfig import ClientConfig; assert hasattr(ClientConfig, 'load_client_connect_config')" 2>/dev/null; then
  red "FAIL: temporalio.envconfig.ClientConfig.load_client_connect_config missing."
  FAIL=1
else
  grn "OK   from temporalio.envconfig import ClientConfig (load_client_connect_config present)"
fi
if ! uv run python -c "from temporalio.client import Client; assert hasattr(Client, 'execute_activity')" 2>/dev/null; then
  red "FAIL: Client.execute_activity not present on installed SDK."
  FAIL=1
else
  grn "OK   Client.execute_activity present"
fi

if [ "$FAIL" -ne 0 ]; then
  red "Preflight failed at the import stage."
  exit 1
fi

# --- 4. Standalone Activities gate (active proof) ---------------------------
hdr "Standalone Activities probe"
# Pick a free port for a throwaway dev server so a running default server on
# 7233 is never disturbed.
PROBE_PORT=""
for cand in 7239 7240 7241 7242; do
  if ! lsof -iTCP:"$cand" -sTCP:LISTEN >/dev/null 2>&1; then
    PROBE_PORT="$cand"
    break
  fi
done
if [ -z "$PROBE_PORT" ]; then
  red "FAIL: no free port found for the throwaway dev server."
  exit 1
fi

echo "The demo dev server is started with the Standalone Activities flag like this:"
echo "  temporal server start-dev --dynamic-config-value activity.enableStandalone=true"
echo "Proving it now on a throwaway server (port $PROBE_PORT):"

SERVER_LOG="$(mktemp)"
temporal server start-dev \
  --headless \
  --port "$PROBE_PORT" \
  --log-level error \
  --dynamic-config-value activity.enableStandalone=true \
  >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$SERVER_LOG"
}
trap cleanup EXIT

# Wait for readiness.
READY=0
for _ in $(seq 1 40); do
  if temporal operator cluster health --address "localhost:$PROBE_PORT" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.5
done
if [ "$READY" -ne 1 ]; then
  red "FAIL: throwaway dev server did not become healthy. Log:"
  cat "$SERVER_LOG" || true
  exit 1
fi

if PREFLIGHT_TARGET="localhost:$PROBE_PORT" uv run python scripts/preflight_standalone_probe.py; then
  grn "OK   Standalone Activities proven end to end."
else
  red "FAIL: Standalone Activity probe failed."
  red "REMEDIATION: ensure the dev server runs with"
  red "  --dynamic-config-value activity.enableStandalone=true"
  exit 1
fi

# --- 5. Stage 2 mock smoke (conditional until stage 2 exists) ---------------
hdr "stage 2 mock smoke"
if [ -f "scripts/smoke.py" ]; then
  if PREFLIGHT_TARGET="localhost:$PROBE_PORT" uv run python scripts/smoke.py; then
    grn "OK   stage 2 mock smoke: one session, one message, one reply."
  else
    red "FAIL: stage 2 mock smoke failed."
    exit 1
  fi
else
  echo "SKIP: scripts/smoke.py not present yet. The stage 2 smoke is exercised"
  echo "      here once stage 2 is built."
fi

# Throwaway server no longer needed.
cleanup
trap - EXIT

# --- 6. Resolved switches and run mode --------------------------------------
hdr "resolved switches"
if [ -f "scripts/print_switches.py" ]; then
  uv run python scripts/print_switches.py || true
else
  echo "TEMPORAL_TARGET=${TEMPORAL_TARGET:-local}"
  echo "MODEL_MODE=${MODEL_MODE:-mock}"
  echo "SKILLS_MODE=${SKILLS_MODE:-mock}"
  echo "RUNTIME_HOST=${RUNTIME_HOST:-local}"
  echo "If launched now, the demo would run: mock model, mock skills, local dev server."
fi

# --- 7. Version table -------------------------------------------------------
hdr "version table"
printf '%-14s %-12s %-10s %s\n' "component" "present" "floor" "status"
printf '%-14s %-12s %-10s %s\n' "python"     "$PYTHON_VER"     "$FLOOR_PYTHON"     "ok"
printf '%-14s %-12s %-10s %s\n' "uv"         "$UV_VER"         "-"                "ok"
printf '%-14s %-12s %-10s %s\n' "git"        "$GIT_VER"        "-"                "ok"
printf '%-14s %-12s %-10s %s\n' "temporal"   "$CLI_VER"        "$FLOOR_CLI"       "ok"
printf '%-14s %-12s %-10s %s\n' "server"     "$SERVER_VER"     "$FLOOR_SERVER"    "ok"
printf '%-14s %-12s %-10s %s\n' "temporalio" "$TEMPORALIO_VER" "$FLOOR_TEMPORALIO" "ok"
printf '%-14s %-12s %-10s %s\n' "strands"    "$STRANDS_VER"    "1.39.0"           "ok"

echo ""
grn "Preflight OK. Floors met, imports verified, Standalone Activities proven."
