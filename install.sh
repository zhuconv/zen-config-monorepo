#!/bin/bash
# Sets up this Zen configuration on a Mac:
#   1. Tab Autofill, the fx-autoconfig loader both mods need, and the
#      self-check that runs after each Zen update (arc-like-cmdT/install.sh)
#   2. Claude Accounts (multi-cc-switcher/install.sh), which reads your
#      account table at ~/.config/claude-accounts/accounts.json
# Safe to run again, e.g. after `git pull`. Restart Zen afterwards.
#
#   ./install.sh                # the profile Zen opens by default
#   ./install.sh PROFILE_DIR    # a specific profile
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${ZEN_APP:-/Applications/Zen.app}"
ZEN_DATA="$HOME/Library/Application Support/zen"

if [ ! -d "$APP" ]; then
  echo "Zen is not at $APP. Install it from https://zen-browser.app first." >&2
  exit 1
fi
PROFILE=""
if [ $# -ge 1 ]; then
  PROFILE="$1"
elif [ -f "$ZEN_DATA/installs.ini" ]; then
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  [ -n "$rel" ] && PROFILE="$ZEN_DATA/$rel"
fi
if [ -z "$PROFILE" ] || [ ! -f "$PROFILE/prefs.js" ]; then
  echo "Zen has no profile yet. Open Zen once, quit it, then run this again." >&2
  exit 1
fi
if ! xcode-select -p >/dev/null 2>&1; then
  echo "The self-check runs on python3 from the Command Line Tools." >&2
  echo "Run: xcode-select --install, then this again." >&2
  exit 1
fi

echo "== Tab Autofill"
"$HERE/arc-like-cmdT/install.sh" "$PROFILE"
echo
echo "== Claude Accounts"
"$HERE/multi-cc-switcher/install.sh" "$PROFILE"
