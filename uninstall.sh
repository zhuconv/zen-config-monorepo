#!/bin/bash
# Removes what install.sh set up: the agent sidebar, Claude Accounts, Tab
# Autofill, its self-check and the fx-autoconfig loader. Restart Zen afterwards.
#
#   ./uninstall.sh                # the profile Zen opens by default
#   ./uninstall.sh PROFILE_DIR
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/zen-agent-sidebar/uninstall.sh" "$@"
"$HERE/zen-claude-accounts/uninstall.sh" "$@"
"$HERE/zen-tab-autofill/uninstall.sh" "$@"
