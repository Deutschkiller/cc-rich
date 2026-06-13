#!/usr/bin/env bash
set -euo pipefail

LABEL="com.cc-rich.claude-web-proxy"
PLIST_FILE="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)" "$PLIST_FILE" >/dev/null 2>&1 || true
rm -f "$PLIST_FILE"

echo "Uninstalled launchd service: $LABEL"
