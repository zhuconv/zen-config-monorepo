#!/bin/bash
# Removes Claude Accounts from your Zen profile and from the self-check. Your
# account table (~/.config/claude-accounts) and the "Claude · <alias>"
# containers stay, with their logins: remove a container in Zen's settings if
# you no longer want it. The fx-autoconfig loader stays too. Restart Zen
# afterwards.
#
#   ./uninstall.sh                # the profile Zen opens by default
#   ./uninstall.sh PROFILE_DIR
set -euo pipefail

ZEN_DATA="$HOME/Library/Application Support/zen"
SUPPORT="$HOME/Library/Application Support/zen-claude-accounts"
SELFCHECK="$HOME/Library/Application Support/zen-tab-autofill"
if [ $# -ge 1 ]; then
  PROFILE="$1"
else
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  PROFILE="$ZEN_DATA/$rel"
fi

rm -f "$PROFILE/chrome/JS/claude-accounts.uc.js"
rm -rf "$PROFILE/chrome/JS/claude-accounts"
rm -f "$PROFILE/claude-accounts-memory.json"
rm -rf "$SUPPORT"
rm -f "$SELFCHECK/checks.d/claude-accounts.json"
echo "Removed. Restart Zen (about:support > Clear startup cache…)."
