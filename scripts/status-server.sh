#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT/.claude-web"
PID_FILE="$STATE_DIR/server.pid"
LOG_FILE="$STATE_DIR/server.log"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3000}"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "running pid=$PID url=http://$HOST:$PORT"
    exit 0
  fi
  echo "stale pid=$PID"
  exit 1
fi

echo "not running"
echo "log: $LOG_FILE"
