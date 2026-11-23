#!/bin/bash
# Installs Tab Autofill into your own Zen, without a mod manager:
#   1. fx-autoconfig's loader into Zen.app (two files, read once at startup)
#   2. the loader's profile part and tab-autofill.uc.js into your Zen profile
#   3. a self-check that runs after each Zen update and shows a notification
#      only when something broke
# Safe to run again: it copies only what changed, then re-checks. Restart Zen
# afterwards.
#
#   ./install.sh                # the profile Zen opens by default
#   ./install.sh PROFILE_DIR    # a specific profile
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${ZEN_APP:-/Applications/Zen.app}"
ZEN_DATA="$HOME/Library/Application Support/zen"
VENDOR="$HERE/vendor/fx-autoconfig"
SUPPORT="$HOME/Library/Application Support/arc-like-cmdT"
LOG="$HOME/Library/Logs/arc-like-cmdT.log"
LABEL="local.arc-like-cmdT.healthcheck"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON=/usr/bin/python3

if [ $# -ge 1 ]; then
  PROFILE="$1"
else
  rel="$(awk -F= '/^Default=/ { print $2; exit }' "$ZEN_DATA/installs.ini")"
  PROFILE="$ZEN_DATA/$rel"
fi
[ -d "$APP/Contents/Resources" ] || { echo "Zen not found at $APP" >&2; exit 1; }
[ -f "$PROFILE/prefs.js" ] || { echo "Not a Zen profile: $PROFILE" >&2; exit 1; }

probe="$APP/Contents/Resources/.tab-autofill-write-test"
if ! touch "$probe" 2>/dev/null; then
  echo "Cannot write into $APP." >&2
  echo "Allow your terminal under System Settings > Privacy & Security > App Management, then run this again." >&2
  exit 1
fi
rm -f "$probe"

# Copies a file only when it differs, creating folders as needed.
copy() {
  if ! cmp -s "$1" "$2"; then
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    echo "  updated ${2/#$HOME/~}"
  fi
}

echo "Loader in $APP"
copy "$VENDOR/program/config.js" "$APP/Contents/Resources/config.js"
copy "$VENDOR/program/defaults/pref/config-prefs.js" "$APP/Contents/Resources/defaults/pref/config-prefs.js"

echo "Profile ${PROFILE/#$HOME/~}"
for f in "$VENDOR"/profile/chrome/utils/*; do
  copy "$f" "$PROFILE/chrome/utils/$(basename "$f")"
done
copy "$HERE/tab-autofill.uc.js" "$PROFILE/chrome/JS/tab-autofill.uc.js"

echo "Self-check in ${SUPPORT/#$HOME/~}"
copy "$HERE/tools/healthcheck.py" "$SUPPORT/healthcheck.py"
copy "$HERE/test/run_test.py" "$SUPPORT/test/run_test.py"
copy "$HERE/tab-autofill.uc.js" "$SUPPORT/tab-autofill.uc.js"
"$PYTHON" -c 'import json, sys; json.dump({"app": sys.argv[1], "profile": sys.argv[2]}, open(sys.argv[3], "w"), indent=2)' \
  "$APP" "$PROFILE" "$SUPPORT/config.json"
# Forget the last result so the check below runs against what was just installed.
rm -f "$SUPPORT/state.json"

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SUPPORT/healthcheck.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>WatchPaths</key>
  <array>
    <string>$APP/Contents/Resources/application.ini</string>
  </array>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Self-check scheduled; it runs now and after each Zen update. Log: ${LOG/#$HOME/~}"

echo
echo "Done. Restart Zen to load the script: open about:support and click 'Clear startup cache…'."
