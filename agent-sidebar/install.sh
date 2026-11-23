#!/bin/bash
# Installs the agent sidebar into your own Zen:
#   1. the ACP adapters for Codex and Claude Code, exactly as adapter/package-lock.json
#      pins them, into ~/Library/Application Support/zen-agent-sidebar/adapter
#   2. config.json next to them, naming node, the adapters and your CLIs
#   3. agent-sidebar.uc.js and agent-sidebar/ into the profile's chrome/JS
# It needs the fx-autoconfig loader that arc-like-cmdT/install.sh puts in
# place. Safe to run again: paths are refreshed, your config edits are kept.
# Restart Zen afterwards.
#
#   ./install.sh                # the profile Zen opens by default
#   ./install.sh PROFILE_DIR    # a specific profile
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="${ZEN_APP:-/Applications/Zen.app}"
ZEN_DATA="$HOME/Library/Application Support/zen"
SUPPORT="$HOME/Library/Application Support/zen-agent-sidebar"

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

# Prints the first executable among `command -v NAME` and the given paths.
find_tool() {
  local name="$1"; shift
  local found
  found="$(command -v "$name" 2>/dev/null || true)"
  for candidate in "$found" "$@"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      echo "$candidate"
      return
    fi
  done
}
NODE="$(find_tool node /opt/homebrew/bin/node /opt/homebrew/opt/node@24/bin/node /usr/local/bin/node)"
NPM="$(find_tool npm "$(dirname "${NODE:-/x}")/npm" /opt/homebrew/bin/npm)"
CODEX="$(find_tool codex /opt/homebrew/bin/codex /usr/local/bin/codex)"
CLAUDE="$(find_tool claude "$HOME/.local/bin/claude" /opt/homebrew/bin/claude /usr/local/bin/claude)"
[ -n "$NODE" ] && [ -n "$NPM" ] || { echo "Node.js 18+ and npm are needed." >&2; exit 1; }
[ -n "$CODEX" ] || [ -n "$CLAUDE" ] || { echo "Neither codex nor claude is installed." >&2; exit 1; }
echo "node   $NODE ($("$NODE" --version))"
if [ -n "$CODEX" ]; then
  echo "codex  $CODEX ($("$CODEX" --version 2>/dev/null | head -1))"
  "$CODEX" login status 2>&1 | grep -qi "logged in" || echo "  warning: Codex is not logged in; run: codex login"
fi
[ -n "$CLAUDE" ] && echo "claude $CLAUDE ($("$CLAUDE" --version 2>/dev/null | head -1))"

echo "Adapters"
ADAPTER="$SUPPORT/adapter"
mkdir -p "$ADAPTER" "$SUPPORT/workspace" "$SUPPORT/logs"
# npm ci installs exactly what the lockfile pins. The adapters' own copies of
# the codex and claude binaries are optional dependencies (about 680 MB); they
# are skipped because config.json points the adapters at your installed CLIs
# (CODEX_PATH, CLAUDE_CODE_EXECUTABLE). A marker skips the install when the
# lockfile has not changed.
MARKER="$ADAPTER/node_modules/.installed-from-lockfile"
if ! cmp -s "$HERE/adapter/package-lock.json" "$MARKER"; then
  cp "$HERE/adapter/package.json" "$HERE/adapter/package-lock.json" "$ADAPTER/"
  (cd "$ADAPTER" && PATH="$(dirname "$NODE"):$PATH" "$NPM" ci --omit=optional --no-audit --no-fund --silent)
  cp "$HERE/adapter/package-lock.json" "$MARKER"
fi
CODEX_JS="$ADAPTER/node_modules/@agentclientprotocol/codex-acp/dist/index.js"
CLAUDE_JS="$ADAPTER/node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js"
for js in "$CODEX_JS" "$CLAUDE_JS"; do
  pkg="$(dirname "$(dirname "$js")")"
  echo "  $(basename "$pkg")@$("$NODE" -p 'require(process.argv[1]).version' "$pkg/package.json")"
done

# A reading assistant needs no coding tools. Codex runs read-only at low
# effort, and the sidebar also switches off your Codex MCP servers, apps and
# plugins ("leanCodex": false keeps them). Claude Code starts without your
# Claude Code settings (CLAUDE.md, MCP servers, skills) and without the tools
# that change files or run commands. Edit config.json to change any of this.
"$NODE" -e '
const [file, node, codexJs, codex, claudeJs, claude, workspace, logs] = process.argv.slice(1);
const fs = require("fs");
const { dirname } = require("path");
let old = {};
try { old = JSON.parse(fs.readFileSync(file, "utf8")); } catch {}
if (old.command) {
  // The first version held a single Codex agent at the top level.
  old = { default: "codex", agents: { codex: { name: "Codex", ...old } } };
}
const PATH = tool => [...new Set([dirname(node), dirname(tool), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])].join(":");
const agents = { ...old.agents };
if (codex) {
  const mine = agents.codex ?? {};
  agents.codex = {
    name: "Codex",
    ...mine,
    command: node,
    args: [codexJs],
    cwd: workspace,
    env: {
      CODEX_CONFIG: JSON.stringify({ model_reasoning_effort: "low" }),
      INITIAL_AGENT_MODE: "read-only",
      ...mine.env,
      CODEX_PATH: codex,
      APP_SERVER_LOGS: logs,
      PATH: PATH(codex),
    },
  };
}
if (claude) {
  const mine = agents.claude ?? {};
  agents.claude = {
    name: "Claude Code",
    sessionMeta: {
      claudeCode: {
        options: { settingSources: [], disallowedTools: ["Bash", "Edit", "Write", "NotebookEdit"] },
      },
    },
    defaults: { effort: "low" },
    ...mine,
    command: node,
    args: [claudeJs],
    cwd: workspace,
    env: { ...mine.env, CLAUDE_CODE_EXECUTABLE: claude, CLAUDE_AGENT_LOGS: logs, PATH: PATH(claude) },
  };
}
const config = { ...old, default: old.default ?? (codex ? "codex" : "claude"), agents };
fs.writeFileSync(file, JSON.stringify(config, null, 2) + "\n");
' "$SUPPORT/config.json" "$NODE" "$CODEX_JS" "$CODEX" "$CLAUDE_JS" "$CLAUDE" "$SUPPORT/workspace" "$SUPPORT/logs"
echo "  wrote ${SUPPORT/#$HOME/~}/config.json"

echo "Profile ${PROFILE/#$HOME/~}"
mkdir -p "$PROFILE/chrome/JS"
cp "$HERE/agent-sidebar.uc.js" "$PROFILE/chrome/JS/agent-sidebar.uc.js"
rsync -a --delete --exclude config.json "$HERE/agent-sidebar/" "$PROFILE/chrome/JS/agent-sidebar/"
echo "  copied agent-sidebar.uc.js and agent-sidebar/"

echo
echo "Done. Restart Zen: open about:support, click 'Clear startup cache…', and restart."
echo "Then select text on a page and press Cmd+Shift+A."
