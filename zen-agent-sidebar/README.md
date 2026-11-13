# Zen Agent Sidebar

Select text in Zen, press **⌘⇧A**, and ask your local Codex or Claude Code
about it in a sidebar. The agent gets the page's text (or the PDF's text) and
your selection, so it answers from what you are reading instead of searching
the web for it. It runs on your own ChatGPT or Claude login through the agent
CLIs. No API key is involved.

## Using it

- **⌘⇧A** opens and closes the panel. It replaces Zen's own ⌘⇧A (search tabs)
  while the mod runs. If you have selected new text on the page, pressing it
  with the panel open puts that text in the composer instead of closing it.
- **The header** picks the agent (Codex or Claude Code), the model and the
  reasoning effort. A model or effort change applies to the current
  conversation at once and becomes the default for new ones. Switching agents
  starts a new conversation, because a session cannot move between agents.
- **Conversations.** Each tab has its own. Follow-up questions keep the
  context. ＋ starts over, and Esc in the composer returns to the page.

## How it works

```
Zen window ── agent-sidebar.uc.js ── shortcut, one conversation per tab
   │                │
   │ tab content    └── AgentService.sys.mjs ── stdio (ACP) ─┬─ codex-acp ──────── codex CLI
   │                                                         └─ claude-agent-acp ── claude CLI
   └── AgentSidebarChild: selection, Readability text, pdf.js text
Sidebar ── panel.html / panel.js: chat UI, Markdown via marked + Firefox's sanitizer
```

- **Context.** The first question about a page attaches its text as an ACP
  resource: Readability's article text, or the page's visible text when
  Readability finds no article. For Firefox's PDF viewer it is the text of
  every page, capped at `uc.agent-sidebar.max-page-chars`. Follow-ups in the
  same conversation send only the new selection and the question.
- **Speed.** ⌘⇧A starts the agent and its session and reads the page while
  you type the question. The header shows the model and effort lists saved
  from earlier runs (`options-cache.json`), so it does not wait for the agent.
- **Memory.** An agent starts on first use and stops after 10 minutes without
  use. Each agent keeps at most 2 sessions open; a Claude Code session is a
  `claude` process of 200–300 MB. A closed session reopens from the agent's
  own transcript when its tab asks again, so the conversation keeps its
  context.
- **Codex** runs read-only at low effort, without your MCP servers, apps,
  plugins and notifier, and without features a reader does not use (the
  shell snapshot alone costs about 0.6 s per new session).
- **Claude Code** runs without your Claude Code settings (CLAUDE.md, MCP
  servers, skills) and without Bash, Edit and Write. It cannot take an
  appended system prompt, so the reading instructions go into the first
  message of each session.
- **Permissions.** Anything else an agent wants to do shows as a card with
  Allow and Deny buttons.

## Install

From the repository root, `./install.sh` installs this together with
Tab Autofill and the fx-autoconfig loader both need. This folder's
`./install.sh` installs only the sidebar, once the loader is in place. It
needs Node 18+, and `codex` and/or `claude` installed and logged in.

```sh
./install.sh            # adapters + config.json + scripts into your Zen profile
```

The adapters come from `adapter/package-lock.json` through `npm ci`, so every
machine gets the same versions. Their bundled copies of the codex and claude
binaries (about 680 MB) are left out: the sidebar uses your installed CLIs.

Then restart Zen (about:support → Clear startup cache…). `./uninstall.sh`
removes it.

## Settings

`~/Library/Application Support/zen-agent-sidebar/config.json` lists the agents
as `{ "default": "codex", "agents": { "codex": {…}, "claude": {…} } }`.
`install.sh` keeps your edits. Per agent:

- `env`: the adapter's environment. For Codex, `CODEX_CONFIG` is JSON merged
  into Codex's config, and `INITIAL_AGENT_MODE` sets the permission mode.
- `defaults`: model or effort values to start sessions with, e.g.
  `{"effort": "low"}`. A choice made in the header wins.
- `sessionMeta`: `_meta` sent with `session/new` and `session/resume`. For
  Claude Code, `claudeCode.options` reaches the Claude Agent SDK.
- `"leanCodex": false`: keep your Codex MCP servers, apps, plugins, notifier
  and features.

Prefs (about:config):

- `uc.agent-sidebar.shortcut`: `accel,shift+A` (restart to apply)
- `uc.agent-sidebar.max-page-chars`: `120000`
- `uc.agent-sidebar.default-question`: asked when you send a selection
  without typing anything
- `uc.agent-sidebar.harness`: the agent for new conversations. The header
  sets it, and also sets `uc.agent-sidebar.<agent>.<option>` for the model
  and effort.
- `uc.agent-sidebar.idle-minutes`: `10`; stop an agent after this long
  without use (`0`: never)
- `uc.agent-sidebar.open-sessions`: `2`; open sessions per agent (`0`: no
  limit)
- `uc.agent-sidebar.debug`: log the agents' stderr to the Browser Console

## Tests

```sh
python3 test/run_test.py                      # headless Zen + two mock ACP agents
python3 test/run_test.py --via-loader PROFILE/chrome --real CONFIG
```

The second form loads the script through your installed fx-autoconfig loader,
then asks each real agent in CONFIG two questions, with an idle stop in
between (this uses a little quota).
