#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$ROOT/.claude-web"
PID_FILE="$STATE_DIR/server.pid"
LOG_FILE="$STATE_DIR/server.log"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-3000}"

mkdir -p "$STATE_DIR"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    echo "Claude Code Web Proxy is already running: pid=$PID url=http://$HOST:$PORT"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

cd "$ROOT"
nohup env HOST="$HOST" PORT="$PORT" python3 server.py >>"$LOG_FILE" 2>&1 &
PID="$!"
echo "$PID" > "$PID_FILE"

sleep 0.5

if ! kill -0 "$PID" 2>/dev/null; then
  echo "Failed to start. See log: $LOG_FILE" >&2
  rm -f "$PID_FILE"
  exit 1
fi

echo "Started Claude Code Web Proxy: pid=$PID url=http://$HOST:$PORT"
echo "Log: $LOG_FILE"
