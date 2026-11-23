#!/bin/bash
# Removes what install.sh added. The loader stays if other user scripts are
# still in the profile's chrome/JS folder. Restart Zen afterwards.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${ZEN_APP:-/Applications/Zen.app}"
ZEN_DATA="$HOME/Library/Application Support/zen"
VENDOR="$HERE/vendor/fx-autoconfig"
SUPPORT="$HOME/Library/Application Support/zen-tab-autofill"
LOG="$HOME/Library/Logs/zen-tab-autofill.log"
LABEL="local.zen-tab-autofill.healthcheck"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ $# -ge 1 ]; then
  PROFILE="$1"
elif [ -f "$SUPPORT/config.json" ]; then
  PROFILE="$(/usr/bin/python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["profile"])' "$SUPPORT/config.json")"
else
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  PROFILE="$ZEN_DATA/$rel"
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST" "$LOG"
rm -rf "$SUPPORT"

rm -f "$PROFILE/chrome/JS/tab-autofill.uc.js"
rmdir "$PROFILE/chrome/JS" 2>/dev/null || true
if [ -d "$PROFILE/chrome/JS" ]; then
  echo "Other user scripts remain in $PROFILE/chrome/JS; keeping the loader."
else
  for f in "$VENDOR"/profile/chrome/utils/*; do
    rm -f "$PROFILE/chrome/utils/$(basename "$f")"
  done
  rmdir "$PROFILE/chrome/utils" 2>/dev/null || true
  rm -f "$APP/Contents/Resources/config.js" "$APP/Contents/Resources/defaults/pref/config-prefs.js"
  rmdir "$APP/Contents/Resources/defaults/pref" "$APP/Contents/Resources/defaults" 2>/dev/null || true
fi

echo "Removed. Restart Zen: open about:support and click 'Clear startup cache…'."
