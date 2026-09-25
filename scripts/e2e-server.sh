#!/usr/bin/env bash
# Starts an isolated Demojo API + worker in FIXTURE mode for browser tests.
# Usage: scripts/e2e-server.sh <port> <data_dir>
set -euo pipefail
PORT="${1:-8765}"
DATA="${2:-$(mktemp -d -t demojo-e2e-XXXX)}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export DATA_DIR="$DATA" DEMOJO_PROVIDER_MODE=fixture DEMOJO_ENV_FILE=/dev/null
cd "$HERE/backend"
uv run demojo worker > "$DATA/worker.log" 2>&1 &
WORKER=$!
trap 'kill $WORKER 2>/dev/null || true' EXIT
uv run demojo serve --port "$PORT"
