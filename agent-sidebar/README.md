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
  The selection and attached files stay in the composer.
- **The question box** is one row: **+** to attach files, the question, and
  Send. While the agent works, the empty box says what it is doing
  (Thinking…) and Send turns into Stop.
- **Files.** **+** picks files; you can also drop them on the panel or paste
  them (a screenshot, or files copied in Finder). They go with the next
  question:
  - images, scaled down to 1568 px on the long side;
  - PDFs as their text, the same as a PDF open in a tab;
  - other text files (Markdown, code, CSV, LaTeX…) as their text.

  Text is capped like page text. Office files, archives and scanned PDFs are
  refused with a reason; for a figure, paste a screenshot of it.
- **Copy and save.** Under each answer, one button copies its Markdown and
  the other saves it as a `.md` file. Each code block has the same two
  buttons in its top right corner, and saves with its language's extension
  (`.md`, `.py`, `.tex`…), so asking for "a Markdown file" and saving the
  block gives just the file. A file is named after its first heading, or
  else the page's title. It goes where Zen saves downloads: straight into
  Downloads, or through a Save As dialog if Zen is set to ask. It is also
  listed in the downloads panel.
- **Edit.** Point at a question and click ✎ beside it to change it; Enter
  sends it and Esc cancels. The edited question replaces the old one and
  everything after it, and the agent forgets those too. The passage and
  files go with it as they were.
- **Conversations.** Each tab has its own. Follow-up questions keep the
  context. ＋ in the header starts over, and Esc in the question box returns
  to the page.

## How it works

```
Zen window ── agent-sidebar.uc.js ── shortcut, one conversation per tab
   │                │
   │ tab content    └── AgentService.sys.mjs ── stdio (ACP) ─┬─ codex-acp ──────── codex CLI
   │                                                         └─ claude-agent-acp ── claude CLI
   └── AgentSidebarChild: selection, Readability text, pdf.js text
Sidebar ── panel.html / panel.js: chat UI, Markdown via marked + Firefox's sanitizer
           attachments.mjs: attached images, PDFs (Firefox's pdf.js) and text files
           saving.mjs: answers and code blocks copied, or saved as files
```

- **Context.** The first question about a page attaches its text as an ACP
  resource: the text as rendered, the way you see it, including comments,
  reviews and replies that loaded after the page, without the site's
  navigation, header and footer. (Reader mode's Readability kept only the
  article and dropped those.) For Firefox's PDF viewer it is the text of every
  page. Both are capped at `uc.agent-sidebar.max-page-chars`.
- **Page updates.** A web page is read again for every question, which takes
  milliseconds. When lines were added (more replies loaded, a section you
  opened), only the new lines go along, marked as a page update; when most of
  the page is new, all of it goes again; when nothing was added, only the
  selection and the question go. A PDF does not change, so it is read once.
- **Attachments** are read in the panel as soon as they are added, while you
  type: images become ACP image blocks, and PDFs and text files become
  resources headed by the file name. A PDF is sent as its text, not as the
  file: Claude would also read each page as an image (about twice the
  tokens), and codex-acp cannot pass a PDF to Codex at all. A question keeps
  its files in memory after sending, so an edit can send them again.
- **Editing** forks the agent's session at its last answer before the edited
  question (`session/fork`, with the fork point in `_meta.jetbrains.air.fork`,
  which both adapters read) and closes the old session. The fork holds the
  conversation up to that answer, page text included, so nothing goes again
  and the model's prompt cache still applies. The fork takes 0.05 s with
  Codex and 0.3 s with Claude Code. An agent that cannot fork starts a new
  session instead, and the panel says so.
- **Saving.** The panel writes the file itself, then adds it to the
  downloads list, instead of downloading it from a `data:` URL. That takes
  about 5 ms instead of 100, and Firefox's download reputation check never
  holds up a script (`.sh`, `.py`). The copy and save buttons appear once an
  answer is complete, so a streaming answer is rendered the same way as before.
- **Speed.** ⌘⇧A starts the agent and its session, and reads a PDF, while
  you type the question. Typing in the composer also starts an agent that
  was stopped. ⌘⇧A takes the selection from the frame you are in, and
  never waits on other frames, such as a busy ad or tracker. Measured: the
  panel opens in about 30 ms, and a page is read and attached in 5–50 ms, even a
  120k-character paper. What remains is the model's own time before it
  answers, about 3–5 s for Codex at low effort. The header shows the model and effort lists saved
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
then asks each real agent in CONFIG a few short questions: about the page,
again after an idle stop, about an attached image, and one it must forget
after an edit (this uses a little quota).
