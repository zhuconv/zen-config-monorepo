#!/bin/bash
# Removes what install.sh set up: PR Live Folder, Claude Accounts, Tab
# Autofill, its self-check and the fx-autoconfig loader. Restart Zen
# afterwards.
#
#   ./uninstall.sh                # the profile Zen opens by default
#   ./uninstall.sh PROFILE_DIR
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/arc-like-pr-folder/uninstall.sh" "$@"
"$HERE/multi-cc-switcher/uninstall.sh" "$@"
"$HERE/arc-like-cmdT/uninstall.sh" "$@"
