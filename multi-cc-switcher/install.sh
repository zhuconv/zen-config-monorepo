#!/bin/bash
# Installs Claude Accounts into your own Zen:
#   1. claude-accounts.uc.js and claude-accounts/ into the profile's chrome/JS
#   2. its end-to-end test into ~/Library/Application Support/zen-claude-accounts,
#      registered with the self-check arc-like-cmdT/install.sh sets up, so
#      each Zen update tests this mod too
# It needs the fx-autoconfig loader that arc-like-cmdT/install.sh puts in
# place, and your account table at ~/.config/claude-accounts/accounts.json
# (accounts.example.json shows the format). Safe to run again. Restart Zen
# afterwards.
#
#   ./install.sh                # the profile Zen opens by default
#   ./install.sh PROFILE_DIR    # a specific profile
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${ZEN_APP:-/Applications/Zen.app}"
ZEN_DATA="$HOME/Library/Application Support/zen"
SUPPORT="$HOME/Library/Application Support/zen-claude-accounts"
SELFCHECK="$HOME/Library/Application Support/zen-tab-autofill"
TABLE="$HOME/.config/claude-accounts/accounts.json"

if [ $# -ge 1 ]; then
  PROFILE="$1"
else
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  PROFILE="$ZEN_DATA/$rel"
fi
[ -f "$PROFILE/prefs.js" ] || { echo "Not a Zen profile: $PROFILE" >&2; exit 1; }
if [ ! -f "$APP/Contents/Resources/config.js" ] || [ ! -f "$PROFILE/chrome/utils/boot.sys.mjs" ]; then
  echo "The fx-autoconfig loader is missing. Run ../install.sh (or ../arc-like-cmdT/install.sh) first." >&2
  exit 1
fi

# Copies a file only when it differs, creating folders as needed.
copy() {
  if ! cmp -s "$1" "$2"; then
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    echo "  updated ${2/#$HOME/~}"
  fi
}

echo "Profile ${PROFILE/#$HOME/~}"
copy "$HERE/claude-accounts.uc.js" "$PROFILE/chrome/JS/claude-accounts.uc.js"
mkdir -p "$PROFILE/chrome/JS/claude-accounts"
rsync -a --delete "$HERE/claude-accounts/" "$PROFILE/chrome/JS/claude-accounts/"

echo "Self-check in ${SUPPORT/#$HOME/~}"
copy "$HERE/test/run_test.py" "$SUPPORT/test/run_test.py"
copy "$HERE/claude-accounts.uc.js" "$SUPPORT/claude-accounts.uc.js"
mkdir -p "$SUPPORT/claude-accounts"
rsync -a --delete "$HERE/claude-accounts/" "$SUPPORT/claude-accounts/"
if [ -d "$SELFCHECK" ]; then
  mkdir -p "$SELFCHECK/checks.d"
  /usr/bin/python3 -c 'import json, sys; json.dump({"name": "Claude Accounts", "script": "claude-accounts.uc.js", "test": sys.argv[1]}, open(sys.argv[2], "w"), indent=2)' \
    "$SUPPORT/test/run_test.py" "$SELFCHECK/checks.d/claude-accounts.json"
  # Forget the last result so the next check covers this mod.
  rm -f "$SELFCHECK/state.json"
  launchctl kickstart "gui/$(id -u)/local.zen-tab-autofill.healthcheck" 2>/dev/null || true
  echo "  registered with the self-check; it runs now and after each Zen update"
else
  echo "  no self-check to join (arc-like-cmdT/install.sh sets it up)"
fi

echo
if [ -f "$TABLE" ]; then
  echo "Account table: ${TABLE/#$HOME/~}"
else
  echo "No account table yet. Until there is one, Claude links open as usual."
  echo "  mkdir -p ~/.config/claude-accounts"
  echo "  cp \"$HERE/accounts.example.json\" ~/.config/claude-accounts/accounts.json   # then fill it in"
fi
echo "Done. Restart Zen to load the script: open about:support and click 'Clear startup cache…'."
