#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/.claude-web/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Claude Code Web Proxy is not running: missing pid file"
  exit 0
fi

PID="$(cat "$PID_FILE")"

if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped Claude Code Web Proxy: pid=$PID"
else
  echo "Claude Code Web Proxy was not running: stale pid=$PID"
fi

rm -f "$PID_FILE"
