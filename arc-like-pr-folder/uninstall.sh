#!/bin/bash
# Removes PR Live Folder from your Zen profile and from the self-check. Your
# live folders stay, with the boxes the mod ticked in them ("Created by Me",
# "My Review Requests"), and so do any uc.pr-folder.* settings you changed in
# about:config. The fx-autoconfig loader stays too. Restart Zen afterwards.
#
#   ./uninstall.sh                # the profile Zen opens by default
#   ./uninstall.sh PROFILE_DIR
set -euo pipefail

ZEN_DATA="$HOME/Library/Application Support/zen"
SUPPORT="$HOME/Library/Application Support/arc-like-pr-folder"
SELFCHECK="$HOME/Library/Application Support/arc-like-cmdT"
if [ $# -ge 1 ]; then
  PROFILE="$1"
else
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  PROFILE="$ZEN_DATA/$rel"
fi

rm -f "$PROFILE/chrome/JS/pr-folder.uc.js"
rm -rf "$PROFILE/chrome/JS/pr-folder"
rm -rf "$SUPPORT"
rm -f "$SELFCHECK/checks.d/pr-folder.json"
echo "Removed. Restart Zen (about:support > Clear startup cache…)."
