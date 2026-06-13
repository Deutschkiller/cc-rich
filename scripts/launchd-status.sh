#!/usr/bin/env bash
set -euo pipefail

LABEL="com.cc-rich.claude-web-proxy"
launchctl print "gui/$(id -u)/$LABEL"
