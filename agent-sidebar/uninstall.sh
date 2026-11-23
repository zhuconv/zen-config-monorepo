#!/bin/bash
# Removes the agent sidebar from your Zen profile and deletes the adapter and
# config in ~/Library/Application Support/zen-agent-sidebar. The fx-autoconfig
# loader stays (arc-like-cmdT uses it). Restart Zen afterwards.
#
#   ./uninstall.sh                # the profile Zen opens by default
#   ./uninstall.sh PROFILE_DIR
set -euo pipefail

ZEN_DATA="$HOME/Library/Application Support/zen"
SUPPORT="$HOME/Library/Application Support/zen-agent-sidebar"
if [ $# -ge 1 ]; then
  PROFILE="$1"
else
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  PROFILE="$ZEN_DATA/$rel"
fi

rm -f "$PROFILE/chrome/JS/agent-sidebar.uc.js"
rm -rf "$PROFILE/chrome/JS/agent-sidebar"
rm -rf "$SUPPORT"
echo "Removed. Restart Zen (about:support > Clear startup cache…)."
