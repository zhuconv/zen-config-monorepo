/* Agent Sidebar, shared service.
 *
 * Runs local ACP agents (Codex through codex-acp, Claude Code through
 * claude-agent-acp) for the whole browser and keeps the conversations the
 * sidebar panels show. Each conversation is one ACP session with one agent
 * (its "harness"); agent-sidebar.uc.js gives every tab its own conversation.
 *
 * The agents come from config.json, which install.sh writes to
 * ~/Library/Application Support/zen-agent-sidebar/. The pref
 * uc.agent-sidebar.config can point somewhere else (the tests use this).
 *
 * Memory: an agent process starts on first use and stops after
 * uc.agent-sidebar.idle-minutes without use. At most
 * uc.agent-sidebar.open-sessions sessions per agent stay open (a Claude Code
 * session is a claude process of 200-300 MB); older ones are closed and
 * resumed from the agent's own transcript when their tab asks again.
 */

import { Subprocess } from "resource://gre/modules/Subprocess.sys.mjs";
import { clearInterval, clearTimeout, setInterval, setTimeout } from "resource://gre/modules/Timer.sys.mjs";

const LOG = "[agent-sidebar]";
const ACTOR = "AgentSidebar";
const PREF_CONFIG = "uc.agent-sidebar.config";
const PREF_DEBUG = "uc.agent-sidebar.debug";
const PREF_HARNESS = "uc.agent-sidebar.harness";
const PREF_IDLE_MINUTES = "uc.agent-sidebar.idle-minutes";
const PREF_OPEN_SESSIONS = "uc.agent-sidebar.open-sessions";
const DEFAULT_CONFIG = ["Library", "Application Support", "zen-agent-sidebar", "config.json"];
// The last model and effort lists each agent reported, kept next to
// config.json so the panel header can show them before an agent has started.
const OPTIONS_CACHE = "options-cache.json";
const CLIENT = { name: "zen-agent-sidebar", title: "Zen Agent Sidebar", version: "0.4.0" };

/** Session settings the panel header offers: the model and the effort. */
export const MODEL_OPTION = "model";
export const EFFORT_OPTIONS = ["reasoning_effort", "effort"];

const INSTRUCTIONS = `You are a reading assistant inside the user's web browser (Zen).
The user is reading a web page or a PDF. With their question, the browser attaches:
- the page's text as the user sees it, as an embedded resource whose URI is the page URL. It is attached in full the first time the user asks about a page. After that only what is new on the page is attached, marked as a page update, so look back at the earlier text for the rest.
- the passage they selected, inside <selection> tags, when they selected one.
- files they chose to attach: images, and the text of PDFs and text files as embedded resources headed by the file name.
Answer from the attached page first: it is exactly what the user is looking at. Search the web only when the question needs information the page does not contain.
Be concise and direct, and reply in the language of the user's question. Use Markdown, and write math in LaTeX between $...$ or $$...$$.
This is not a coding task: do not run shell commands, read local files, or edit anything unless the user explicitly asks you to.`;
const SYSTEM_PROMPT = { mode: "append", content: [{ type: "text", text: INSTRUCTIONS }] };

function debug(...args) {
  if (Services.prefs.getBoolPref(PREF_DEBUG, false)) {
    console.log(LOG, ...args);
  }
}

function optionPref(harness, optionId) {
  return `uc.agent-sidebar.${harness}.${optionId}`;
}

/** Every value a select option offers, through option groups too. */
export function optionValues(option) {
  return (option?.options ?? []).flatMap(o => (Array.isArray(o.options) ? o.options : [o])).map(o => o.value);
}

/** One JSON-RPC connection to an ACP agent over the agent's stdin/stdout. */
class AcpConnection {
  #proc = null;
  #nextId = 0;
  #pending = new Map();
  #encoder = new TextEncoder();
  #stderr = [];

  exited = false;
  stopping = false; // stopped on purpose, after it sat unused
  initResult = null;
  lastUsed = Date.now();
  onNotification = () => {};
  onRequest = () => {};
  onExit = () => {};

  constructor(harness, config) {
    this.harness = harness;
    this.config = config;
  }

  /** Whether session/new takes a system prompt that is added to the agent's own. */
  get appendsSystemPrompt() {
    const capability = this.initResult?.agentCapabilities?.sessionCapabilities?.systemPrompt;
    return !!capability && (!capability.mode || !!capability.mode.append);
  }

  /** The ways this agent can reopen a closed session, best first. */
  get restoreMethods() {
    const caps = this.initResult?.agentCapabilities;
    const methods = [];
    if (caps?.sessionCapabilities?.resume) {
      methods.push("session/resume");
    }
    if (caps?.loadSession) {
      methods.push("session/load");
    }
    return methods;
  }

  get closesSessions() {
    return !!this.initResult?.agentCapabilities?.sessionCapabilities?.close;
  }

  get forksSessions() {
    return !!this.initResult?.agentCapabilities?.sessionCapabilities?.fork;
  }

  async start() {
    const { command, args = [], env = {}, cwd } = this.config;
    if (cwd) {
      await IOUtils.makeDirectory(cwd, { createAncestors: true, ignoreExisting: true });
    }
    this.#proc = await Subprocess.call({
      command,
      arguments: args,
      environment: env,
      environmentAppend: true,
      workdir: cwd || null,
      stderr: "pipe",
    });
    this.#readStdout();
    this.#readStderr();
    this.#proc.wait().then(({ exitCode }) => this.#exited(exitCode));
    this.initResult = await this.request(
      "initialize",
      {
        protocolVersion: 1,
        clientCapabilities: {
          fs: { readTextFile: false, writeTextFile: false },
          terminal: false,
        },
        clientInfo: CLIENT,
      },
      30000
    );
    return this.initResult;
  }

  request(method, params, timeout = 0) {
    if (this.exited) {
      return Promise.reject(new Error("the agent is not running"));
    }
    const id = ++this.#nextId;
    return new Promise((resolve, reject) => {
      const timer = timeout
        ? setTimeout(() => {
            this.#pending.delete(id);
            reject(new Error(`${method} timed out`));
          }, timeout)
        : null;
      this.#pending.set(id, { resolve, reject, timer });
      this.#write({ jsonrpc: "2.0", id, method, params });
    });
  }

  notify(method, params) {
    this.#write({ jsonrpc: "2.0", method, params });
  }

  respond(id, result) {
    this.#write({ jsonrpc: "2.0", id, result });
  }

  respondError(id, code, message) {
    this.#write({ jsonrpc: "2.0", id, error: { code, message } });
  }

  stderrTail(lines = 5) {
    return this.#stderr.join("").trim().split("\n").slice(-lines).join("\n");
  }

  kill() {
    if (!this.exited) {
      this.#proc?.kill();
    }
  }

  #write(message) {
    if (this.exited || !this.#proc) {
      return;
    }
    this.#proc.stdin
      .write(this.#encoder.encode(JSON.stringify(message) + "\n"))
      .catch(e => debug("write failed", e));
  }

  async #readStdout() {
    let buffer = "";
    try {
      for (;;) {
        const chunk = await this.#proc.stdout.readString();
        if (!chunk) {
          break;
        }
        buffer += chunk;
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line) {
            this.#handleLine(line);
          }
        }
      }
    } catch (e) {
      debug("stdout closed", e);
    }
  }

  async #readStderr() {
    try {
      for (;;) {
        const chunk = await this.#proc.stderr.readString();
        if (!chunk) {
          break;
        }
        this.#stderr.push(chunk);
        if (this.#stderr.length > 50) {
          this.#stderr.splice(0, this.#stderr.length - 50);
        }
        debug(`${this.harness} stderr:`, chunk.trimEnd());
      }
    } catch (e) {
      debug("stderr closed", e);
    }
  }

  #handleLine(line) {
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      debug("non-JSON line from agent:", line.slice(0, 300));
      return;
    }
    if (message.method === undefined && message.id !== undefined) {
      const pending = this.#pending.get(message.id);
      if (!pending) {
        return;
      }
      this.#pending.delete(message.id);
      if (pending.timer) {
        clearTimeout(pending.timer);
      }
      if (message.error) {
        const error = new Error(errorText(message.error));
        error.code = message.error.code;
        pending.reject(error);
      } else {
        pending.resolve(message.result);
      }
    } else if (message.id !== undefined) {
      this.onRequest(message);
    } else {
      this.onNotification(message);
    }
  }

  #exited(code) {
    if (this.exited) {
      return;
    }
    this.exited = true;
    for (const { reject, timer } of this.#pending.values()) {
      if (timer) {
        clearTimeout(timer);
      }
      reject(new Error(`the agent stopped (exit code ${code})`));
    }
    this.#pending.clear();
    this.onExit(code);
  }
}

// codex-acp puts the upstream error, as JSON text, inside error.data.message.
function errorText(error) {
  let text = error?.data?.message || error?.message || "the agent reported an error";
  try {
    const parsed = JSON.parse(text);
    text = parsed?.error?.message || parsed?.message || text;
  } catch {}
  return text;
}

/** One conversation, shown in a sidebar panel; one ACP session. */
class Conversation {
  sessionId = null;
  conn = null;
  parkedId = null; // a session closed to save memory, to resume on the next question
  configOptions = null; // the session's settings, from the agent
  instructed = false; // the session has the reading-assistant instructions
  instructedBy = null; // "system" (session/new's system prompt) or "prompt" (in a message)
  lastUsed = 0;
  messages = [];
  busy = false;
  status = "";
  draft = null; // { selection, text } the composer keeps while switching tabs
  lastSelection = ""; // the page selection the shortcut last picked up
  current = null; // assistant message receiving text
  sentPages = new Map(); // page URL -> { kind, text }: what this session has of each page
  // The session and the sessions it was forked from: their answers are in its
  // history.
  lineage = new Set();
  pageText = null; // { url, promise }: a PDF's text, read ahead while the user types
  listeners = new Set();

  constructor(id, harness) {
    this.id = id;
    this.harness = harness;
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  emit(change) {
    for (const listener of [...this.listeners]) {
      try {
        listener(change);
      } catch (e) {
        console.error(LOG, e);
      }
    }
  }

  push(message) {
    message.v = 1;
    this.messages.push(message);
    this.emit({ type: "add", message });
    return message;
  }

  touch(message) {
    message.v++;
    this.emit({ type: "update", message });
  }

  /** Drop messages[i] and every message after it. */
  truncate(i) {
    const dropped = this.messages.splice(i);
    this.emit({ type: "truncate", dropped });
  }

  setStatus(status) {
    this.status = status;
    this.emit({ type: "status" });
  }
}

function registerActor(base) {
  try {
    ChromeUtils.registerWindowActor(ACTOR, {
      parent: { esModuleURI: base + "AgentSidebarParent.sys.mjs" },
      child: { esModuleURI: base + "AgentSidebarChild.sys.mjs" },
      allFrames: true,
      messageManagerGroups: ["browsers"],
      // Without this the actor is refused in web and file content processes.
      safeForUntrustedWebProcess: true,
    });
  } catch (e) {
    if (!/already/i.test(String(e))) {
      throw e;
    }
  }
}

/** The page for the agent. `update` is "new" for only the lines that are new
 * since the page was attached, or "full" for the whole page again after it
 * changed a lot. */
function resourceBlock(page, info, update = null) {
  const header = [`Title: ${page.title}`, `URL: ${page.url}`];
  if (update === "new") {
    header.push("Page update: the lines that are new on the page since it was attached. The rest of the page is as before.");
  } else if (update === "full") {
    header.push(`The page changed since it was attached. Its full text again${info.truncated ? " (truncated)" : ""}:`);
  } else if (info.kind === "pdf") {
    header.push(
      `PDF text, pages 1-${info.pagesRead} of ${info.pages}${info.truncated ? " (truncated)" : ""}`
    );
  } else if (info.truncated) {
    header.push("Page text (truncated)");
  }
  return {
    type: "resource",
    resource: {
      uri: page.url,
      mimeType: "text/plain",
      text: `${header.join("\n")}\n\n${info.text}`,
    },
  };
}

/**
 * The lines of `now` that `before` does not have, in order, with "…" where
 * lines it has were skipped. Each line of `before` counts once, so a line that
 * appears one more time is new.
 */
function addedLines(before, now) {
  const left = new Map();
  for (const line of before.split("\n")) {
    const key = line.trim();
    if (key) {
      left.set(key, (left.get(key) ?? 0) + 1);
    }
  }
  const out = [];
  let skipped = false;
  for (const line of now.split("\n")) {
    const key = line.trim();
    if (!key) {
      continue;
    }
    const count = left.get(key) ?? 0;
    if (count > 0) {
      left.set(key, count - 1);
      skipped = true;
      continue;
    }
    if (skipped && out.length) {
      out.push("…");
    }
    skipped = false;
    out.push(line);
  }
  return out.join("\n");
}

/** A file the user attached in the panel, as read by attachments.mjs. */
function attachmentBlock(file) {
  if (file.kind === "image") {
    return { type: "image", mimeType: file.mimeType, data: file.data };
  }
  const header = [`File: ${file.name}`];
  if (file.kind === "pdf") {
    header.push(`PDF text, pages 1-${file.pagesRead} of ${file.pages}${file.truncated ? " (truncated)" : ""}`);
  } else if (file.truncated) {
    header.push("Text (truncated)");
  }
  return {
    type: "resource",
    resource: { uri: file.uri, mimeType: "text/plain", text: `${header.join("\n")}\n\n${file.text}` },
  };
}

function promptText(question, selection) {
  return selection ? `<selection>\n${selection}\n</selection>\n\n${question}` : question;
}

function merge(base, over) {
  const out = { ...base };
  for (const [key, value] of Object.entries(over ?? {})) {
    out[key] =
      value && typeof value === "object" && !Array.isArray(value) && typeof base[key] === "object"
        ? merge(base[key], value)
        : value;
  }
  return out;
}

/**
 * A reading assistant needs none of the user's Codex MCP servers, apps or
 * plugins, and loading them costs time and prompt tokens on every turn. An
 * empty mcp_servers table does not remove them (Codex merges overrides), so
 * switch each configured server off by name. Settings in the agent's
 * CODEX_CONFIG win; "leanCodex": false skips this.
 *
 * The features switched off here cost time or processes and add tools a
 * reader does not use. shell_snapshot alone runs a login shell for every new
 * session (about 0.6 s). `notify` runs the user's notifier after every turn;
 * the panel already shows the answer. Codex ignores feature names it does not
 * know, so older and newer versions take this too.
 */
const LEAN_CODEX_FEATURES = {
  apps: false,
  plugins: false,
  multi_agent: false,
  shell_snapshot: false,
  computer_use: false,
  browser_use: false,
  browser_use_external: false,
  in_app_browser: false,
  image_generation: false,
  plugin_discovery_prefetch: false,
  skill_search: false,
  tool_suggest: false,
  goals: false,
  workspace_dependencies: false,
};

export async function leanCodexEnv(agent) {
  const env = { ...agent.env };
  if (!env.CODEX_PATH || agent.leanCodex === false) {
    return env;
  }
  let own = {};
  try {
    own = JSON.parse(env.CODEX_CONFIG || "{}");
  } catch {}
  const home = env.CODEX_HOME || PathUtils.join(Services.dirsvc.get("Home", Ci.nsIFile).path, ".codex");
  let toml = "";
  try {
    toml = await IOUtils.readUTF8(PathUtils.join(home, "config.toml"));
  } catch {}
  const servers = new Set();
  for (const m of toml.matchAll(/^\s*\[mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))/gm)) {
    servers.add(m[1] ?? m[2]);
  }
  const lean = {
    notify: [],
    features: LEAN_CODEX_FEATURES,
    mcp_servers: Object.fromEntries([...servers].map(name => [name, { enabled: false }])),
  };
  env.CODEX_CONFIG = JSON.stringify(merge(lean, own));
  return env;
}

/** config.json, in the current shape: { default, agents: { id: {...} } }. */
function normalizeConfig(raw) {
  if (raw?.command) {
    // The first version held a single Codex agent at the top level.
    const { command, args, cwd, env, leanCodex } = raw;
    return { default: "codex", agents: { codex: { name: "Codex", command, args, cwd, env, leanCodex } } };
  }
  return raw;
}

export const AgentService = {
  base: null,
  config: null,
  conns: new Map(), // harness -> AcpConnection
  connecting: new Map(), // harness -> Promise<AcpConnection>
  options: new Map(), // harness -> the latest session's configOptions
  optionsReady: null, // reading options-cache.json at startup
  savedOptions: "",
  saveTimer: null,
  sweeper: null, // stops idle agents; runs only while an agent runs
  lastExit: "",
  conversations: new Map(),
  bySession: new Map(),
  nextId: 1,

  init(base) {
    if (this.base) {
      return;
    }
    this.base = base;
    registerActor(base);
    Services.obs.addObserver(() => this.shutdown(), "quit-application-granted");
    this.loadConfig().catch(e => debug(e.message));
    this.optionsReady = this.loadOptions();
  },

  optionsPath() {
    return PathUtils.join(PathUtils.parent(this.configPath()), OPTIONS_CACHE);
  },

  /** The model and effort lists from earlier runs, so the header shows them
   * before the agent has started. A live session replaces them. */
  async loadOptions() {
    try {
      const cached = await IOUtils.readJSON(this.optionsPath());
      for (const [harness, options] of Object.entries(cached ?? {})) {
        if (Array.isArray(options) && !this.options.has(harness)) {
          this.options.set(harness, options);
        }
      }
    } catch {}
    this.savedOptions = JSON.stringify(Object.fromEntries(this.options));
  },

  saveOptions() {
    clearTimeout(this.saveTimer);
    this.saveTimer = null;
    const json = JSON.stringify(Object.fromEntries(this.options));
    if (json === this.savedOptions) {
      return;
    }
    this.savedOptions = json;
    const path = this.optionsPath();
    IOUtils.writeUTF8(path, json, { tmpPath: `${path}.tmp` }).catch(e => debug("could not save options:", e.message));
  },

  createConversation() {
    const conv = new Conversation(this.nextId++, this.preferredHarness());
    this.conversations.set(conv.id, conv);
    return conv;
  },

  /** A tab closed: end its session and drop the conversation. */
  forget(conv) {
    this.reset(conv);
    conv.listeners.clear();
    this.conversations.delete(conv.id);
  },

  configPath() {
    return (
      Services.prefs.getStringPref(PREF_CONFIG, "") ||
      PathUtils.join(Services.dirsvc.get("Home", Ci.nsIFile).path, ...DEFAULT_CONFIG)
    );
  },

  async loadConfig() {
    const path = this.configPath();
    let raw;
    try {
      raw = await IOUtils.readJSON(path);
    } catch (e) {
      throw new Error(`No agent configured (could not read ${path}). Run install.sh first.`);
    }
    const config = normalizeConfig(raw);
    if (!config?.agents || !Object.keys(config.agents).length) {
      throw new Error(`${path} names no agents.`);
    }
    this.config = config;
    return config;
  },

  /** [{ id, name }] for the harness menu, from config.json as loaded at startup. */
  harnessList() {
    return Object.entries(this.config?.agents ?? {}).map(([id, agent]) => ({ id, name: agent.name || id }));
  },

  /** The same, once config.json and the cached options are in. */
  async harnesses() {
    if (!this.config) {
      await this.loadConfig();
    }
    await this.optionsReady;
    return this.harnessList();
  },

  preferredHarness() {
    const wanted = Services.prefs.getStringPref(PREF_HARNESS, "");
    const agents = this.config?.agents;
    if (!agents) {
      return wanted || "codex";
    }
    return agents[wanted] ? wanted : this.config.default in agents ? this.config.default : Object.keys(agents)[0];
  },

  /** The running connection for a harness, started on first use. */
  async connection(harness) {
    const running = this.conns.get(harness);
    if (running && !running.exited) {
      return running;
    }
    if (!this.connecting.has(harness)) {
      const starting = (async () => {
        const config = await this.loadConfig();
        const agent = config.agents[harness];
        if (!agent) {
          throw new Error(`config.json has no agent called "${harness}".`);
        }
        const conn = new AcpConnection(harness, { ...agent, env: await leanCodexEnv(agent) });
        conn.onNotification = message => this.onNotification(message);
        conn.onRequest = message => this.onRequest(conn, message);
        conn.onExit = code => this.onExit(conn, code);
        try {
          await conn.start();
        } catch (e) {
          conn.kill();
          const tail = conn.stderrTail(3);
          throw new Error(`Could not start ${agent.name || harness}: ${e.message}${tail ? `\n${tail}` : ""}`);
        }
        this.conns.set(harness, conn);
        this.startSweeper();
        debug("agent started", harness, conn.initResult?.agentInfo);
        return conn;
      })().finally(() => this.connecting.delete(harness));
      this.connecting.set(harness, starting);
    }
    return this.connecting.get(harness);
  },

  async ensureSession(conv) {
    const conn = await this.connection(conv.harness);
    conn.lastUsed = conv.lastUsed = Date.now();
    if (conv.sessionId && conv.conn === conn) {
      return conn;
    }
    if (!conv.opening) {
      conv.opening = this.openSession(conv, conn).finally(() => {
        conv.opening = null;
      });
    }
    await conv.opening;
    return conn;
  },

  /** What session/new, session/resume and session/fork take for this agent. */
  sessionParams(conn) {
    const params = { cwd: conn.config.cwd || PathUtils.tempDir, mcpServers: [] };
    if (conn.config.sessionMeta) {
      params._meta = conn.config.sessionMeta;
    }
    return params;
  },

  /** Resume the conversation's closed session, or start a new one. */
  async openSession(conv, conn) {
    const params = this.sessionParams(conn);
    let sessionId = null;
    let result = null;
    if (conv.parkedId) {
      result = await this.restoreSession(conn, conv.parkedId, params);
      if (result) {
        sessionId = conv.parkedId;
        // Instructions sent in a message are in the transcript; a system
        // prompt may not come back with the session, so send them again.
        if (conv.instructedBy !== "prompt") {
          conv.instructed = false;
        }
      } else if (conv.messages.length) {
        conv.push({
          role: "note",
          text: "The agent could not reopen this conversation, so it starts over without the earlier messages.",
        });
      }
      conv.parkedId = null;
    }
    if (!result) {
      if (conn.appendsSystemPrompt) {
        params.systemPrompt = SYSTEM_PROMPT;
      }
      try {
        result = await conn.request("session/new", params, 60000);
      } catch (e) {
        // An agent that does not take a system prompt rejects the params.
        if (e.code !== -32602 || !params.systemPrompt) {
          throw e;
        }
        delete params.systemPrompt;
        result = await conn.request("session/new", params, 60000);
      }
      sessionId = result.sessionId;
      conv.instructed = !!params.systemPrompt;
      conv.instructedBy = params.systemPrompt ? "system" : null;
      conv.sentPages.clear();
      conv.lineage.clear();
    }
    await this.adopt(conv, conn, sessionId, result);
  },

  /** Make a session the conversation's own. `result` is what the agent
   * answered to the request that opened it. */
  async adopt(conv, conn, sessionId, result) {
    conv.sessionId = sessionId;
    conv.conn = conn;
    conv.configOptions = result.configOptions ?? null;
    conv.lineage.add(sessionId);
    this.remember(conv);
    this.bySession.set(sessionId, conv);
    await this.applyPreferredOptions(conv);
    conv.emit({ type: "session" });
    this.limitOpenSessions(conv);
  },

  /**
   * Where the session goes back to when the user edits conv.messages[i]: the
   * last answer before it that the session holds, from a turn that ended, with
   * what the session had of the pages and the instructions after that turn.
   * Null when the session holds no answer before it, so a new session loses
   * nothing; { messageId: null } when it does but none can be pointed at.
   */
  forkPoint(conv, i) {
    let held = false;
    for (let j = i - 1; j >= 0; j--) {
      const answer = conv.messages[j];
      if (answer.role !== "assistant" || !conv.lineage.has(answer.session)) {
        continue;
      }
      held = true;
      const turn = conv.messages.slice(0, j).findLast(m => m.role === "user");
      if (answer.messageId && turn?.after) {
        return { messageId: answer.messageId, ...turn.after };
      }
    }
    return held ? { messageId: null } : null;
  },

  /**
   * The user edited a question: take the session back to `point` (see
   * forkPoint), so that it holds the conversation up to the end of that
   * answer's turn and nothing after it. session/fork copies the session up to
   * an answer (both adapters take the fork point in `_meta`, from JetBrains
   * AIR); the session the question was in is closed. With nothing to keep, or
   * when the agent cannot fork, the question goes to a new session.
   */
  async rewind(conv, point) {
    await conv.opening?.catch(() => {}); // a session starting while the user typed
    const source = conv.sessionId ?? conv.parkedId;
    let conn = null;
    let result = null;
    try {
      if (point?.messageId && source) {
        conn = await this.connection(conv.harness);
        if (conn.forksSessions) {
          const params = {
            ...this.sessionParams(conn),
            sessionId: source,
            _meta: merge(conn.config.sessionMeta ?? {}, {
              jetbrains: { air: { fork: { version: 1, messageId: point.messageId } } },
            }),
          };
          if (conn.appendsSystemPrompt) {
            params.systemPrompt = SYSTEM_PROMPT; // the same as the session's own
          }
          try {
            result = await conn.request("session/fork", params, 60000);
            result.systemPrompt = !!params.systemPrompt;
          } catch (e) {
            debug("could not fork the session:", e.message);
          }
        }
      }
    } finally {
      // The edited question must not reach the agent in its old session.
      this.drop(conv);
    }
    if (result?.sessionId) {
      conv.sentPages = new Map(point.sentPages);
      conv.instructed = result.systemPrompt || (point.instructed && point.instructedBy === "prompt");
      conv.instructedBy = result.systemPrompt ? "system" : conv.instructed ? "prompt" : null;
      await this.adopt(conv, conn, result.sessionId, result);
      return;
    }
    // ensureSession opens a new session, which starts with nothing from before.
    if (point) {
      conv.push({
        role: "note",
        text: "The agent could not go back to that point, so it starts over without the earlier messages.",
      });
    }
  },

  /** Let go of the conversation's session, closing it if it is open. */
  drop(conv) {
    if (conv.sessionId) {
      this.bySession.delete(conv.sessionId);
      if (conv.conn && !conv.conn.exited) {
        conv.conn.request("session/close", { sessionId: conv.sessionId }, 10000).catch(() => {});
      }
    }
    conv.sessionId = null;
    conv.conn = null;
    conv.parkedId = null;
    conv.configOptions = null;
  },

  /** Reopen a closed session with its history; null when the agent cannot.
   * session/load replays the history as updates first; nothing listens for
   * this session yet, so they are dropped. */
  async restoreSession(conn, sessionId, params) {
    for (const method of conn.restoreMethods) {
      try {
        return (await conn.request(method, { ...params, sessionId }, 60000)) ?? {};
      } catch (e) {
        debug(`${method} failed:`, e.message);
      }
    }
    return null;
  },

  /** Close a conversation's session to free the agent's memory. The next
   * question resumes it. */
  park(conv, { close = true } = {}) {
    if (!conv.sessionId || conv.busy || conv.opening) {
      return;
    }
    const { conn, sessionId } = conv;
    debug("closing idle session", sessionId);
    this.bySession.delete(sessionId);
    conv.parkedId = sessionId;
    conv.sessionId = null;
    conv.conn = null;
    conv.configOptions = null;
    if (close && conn && !conn.exited) {
      conn.request("session/close", { sessionId }, 10000).catch(() => {});
    }
    conv.emit({ type: "options" });
  },

  /** At most uc.agent-sidebar.open-sessions sessions per agent stay open; the
   * least recently used ones close. Only for agents that can resume them. */
  limitOpenSessions(keep) {
    const conn = keep.conn;
    const limit = Services.prefs.getIntPref(PREF_OPEN_SESSIONS, 2);
    if (!conn || limit <= 0 || !conn.closesSessions || !conn.restoreMethods.length) {
      return;
    }
    const others = [...this.conversations.values()].filter(c => c !== keep && c.conn === conn && c.sessionId);
    let excess = others.length + 1 - limit;
    for (const conv of others.filter(c => !c.busy && !c.opening).sort((a, b) => a.lastUsed - b.lastUsed)) {
      if (excess-- <= 0) {
        break;
      }
      this.park(conv);
    }
  },

  startSweeper() {
    if (!this.sweeper) {
      this.sweeper = setInterval(() => this.sweep(), 30000);
    }
  },

  /** Stop agents nobody used for uc.agent-sidebar.idle-minutes (0: never). */
  sweep(now = Date.now()) {
    const minutes = Services.prefs.getIntPref(PREF_IDLE_MINUTES, 10);
    for (const conn of [...this.conns.values()]) {
      const working = [...this.conversations.values()].some(c => c.conn === conn && (c.busy || c.opening));
      if (minutes > 0 && !working && now - conn.lastUsed >= minutes * 60000) {
        this.stop(conn);
      }
    }
    if (!this.conns.size && !this.connecting.size) {
      clearInterval(this.sweeper);
      this.sweeper = null;
    }
  },

  /** Stop an agent process. Its conversations resume on their next question. */
  stop(conn) {
    debug("stopping idle agent", conn.harness);
    for (const conv of this.conversations.values()) {
      if (conv.conn === conn) {
        this.park(conv, { close: false });
      }
    }
    conn.stopping = true;
    if (this.conns.get(conn.harness) === conn) {
      this.conns.delete(conn.harness);
    }
    conn.kill();
  },

  /** Start the agent and the tab's session early, while the user types. */
  prewarm(conv) {
    this.ensureSession(conv).catch(e => debug("prewarm failed:", e.message));
  },

  /** The page's text, shared by the read-ahead and send(). */
  readPage(conv, page) {
    if (conv.pageText?.url !== page.url) {
      const promise = Promise.resolve()
        .then(() => page.getText())
        .catch(e => {
          debug("could not read the page:", e);
          return null;
        });
      conv.pageText = { url: page.url, promise };
    }
    return conv.pageText.promise;
  },

  /** The shortcut opened the panel: read a PDF while the user types (that
   * can take a second). A web page is read when the question is sent, so the
   * agent gets it as it is then. */
  prefetchPage(conv, page) {
    if (page?.url && page.getText && page.kind === "pdf" && !conv.sentPages.has(page.url)) {
      this.readPage(conv, page);
    }
  },

  /**
   * What the agent needs to hear about the page for this question: all of it
   * the first time; afterwards the lines that are new since it last heard,
   * or the whole page again when most of it is new; nothing when nothing
   * was added. Notes it on the user's message.
   */
  pageBlock(conv, page, info, user) {
    const before = conv.sentPages.get(page.url);
    let block = null;
    if (!before) {
      block = resourceBlock(page, info);
    } else if (before.text !== info.text) {
      const added = addedLines(before.text, info.text);
      if (added.length > info.text.length / 2) {
        block = resourceBlock(page, info, "full");
      } else if (added) {
        block = resourceBlock(page, { ...info, text: added }, "new");
      }
    }
    if (block) {
      user.attached = { kind: info.kind, truncated: !!info.truncated, update: !!before };
      conv.touch(user);
    }
    return block;
  },

  remember(conv) {
    if (conv.configOptions) {
      this.options.set(conv.harness, conv.configOptions);
      if (!this.saveTimer) {
        this.saveTimer = setTimeout(() => this.saveOptions(), 2000);
      }
    }
  },

  /** The model and effort the user picked, or the agent's configured defaults. */
  preferredValue(harness, optionId) {
    return (
      Services.prefs.getStringPref(optionPref(harness, optionId), "") ||
      this.config?.agents?.[harness]?.defaults?.[optionId] ||
      ""
    );
  },

  async applyPreferredOptions(conv) {
    for (const option of conv.configOptions ?? []) {
      if (option.id !== MODEL_OPTION && !EFFORT_OPTIONS.includes(option.id)) {
        continue;
      }
      const wanted = this.preferredValue(conv.harness, option.id);
      if (wanted && wanted !== option.currentValue && optionValues(option).includes(wanted)) {
        try {
          await this.setSessionOption(conv, option.id, wanted);
        } catch (e) {
          debug(`could not set ${option.id}:`, e.message);
        }
      }
    }
  },

  async setSessionOption(conv, optionId, value) {
    const result = await conv.conn.request(
      "session/set_config_option",
      { sessionId: conv.sessionId, configId: optionId, value },
      30000
    );
    if (result?.configOptions?.length) {
      conv.configOptions = result.configOptions;
    } else {
      const option = conv.configOptions?.find(o => o.id === optionId);
      if (option) {
        option.currentValue = value;
      }
    }
    this.remember(conv);
  },

  /** The panel changed the model or the effort. */
  async setOption(conv, optionId, value) {
    Services.prefs.setStringPref(optionPref(conv.harness, optionId), value);
    if (conv.sessionId && conv.conn && !conv.conn.exited) {
      try {
        await this.setSessionOption(conv, optionId, value);
      } catch (e) {
        conv.push({ role: "error", text: `Could not change ${optionId}: ${e.message}` });
      }
    }
    conv.emit({ type: "options" });
  },

  /** The panel switched agents: a session cannot move, so start over. What
   * the composer holds (selection, files) stays for the new agent. */
  setHarness(conv, harness) {
    Services.prefs.setStringPref(PREF_HARNESS, harness);
    if (conv.harness === harness) {
      return;
    }
    this.reset(conv, { keepDraft: true });
    conv.harness = harness;
    conv.emit({ type: "session" });
    this.prewarm(conv);
  },

  /**
   * Ask a question. `page` is { url, title, kind, getText } for the tab's
   * page; its text goes along the first time this conversation asks about
   * it, and what is new on it later (pageBlock). `attachments` are files from
   * the panel (attachments.mjs); they stay with the question, so it can be
   * edited and sent again. `replace` is a question the user edited: it and
   * everything after it go, from the panel and from the agent's session
   * (rewind).
   */
  async send(conv, { question, selection = "", page = null, attachments = [], replace = null }) {
    if (conv.busy) {
      return;
    }
    let point = null;
    if (replace) {
      const i = conv.messages.indexOf(replace);
      if (i < 0) {
        return;
      }
      point = this.forkPoint(conv, i);
      conv.truncate(i);
    }
    conv.busy = true;
    conv.current = null;
    const user = conv.push({
      role: "user",
      text: question,
      selection,
      files: attachments.map(({ name, kind, info, thumb }) => ({ name, kind, info, thumb })),
      attachments,
      page: page ? { title: page.title, url: page.url } : null,
      attached: null,
      after: null, // what the session holds once this turn is in (forkPoint)
    });
    conv.emit({ type: "busy" });
    let seen = null; // the page as the session has it once this question is in
    let conn = null;
    try {
      const name = this.config?.agents?.[conv.harness]?.name || conv.harness;
      if (replace) {
        conv.setStatus("Going back…");
        await this.rewind(conv, point);
      }
      conv.setStatus(conv.sessionId ? "" : `Starting ${name}…`);
      // Read the page while the session starts. A web page is read for every
      // question, so the agent hears what changed on it (replies that loaded,
      // sections the user opened). A PDF does not change: it is read until
      // the session has it, usually ahead, while the user types.
      const wantsText = () =>
        page?.url && page.getText && !(page.kind === "pdf" && conv.sentPages.has(page.url));
      let text = wantsText() ? this.readPage(conv, page) : null;
      conn = await this.ensureSession(conv);
      if (!text && wantsText()) {
        text = this.readPage(conv, page); // the session started over
      }
      if (
        attachments.some(file => file.kind === "image") &&
        !conn.initResult?.agentCapabilities?.promptCapabilities?.image
      ) {
        throw new Error(`${name} does not take images.`);
      }
      const prompt = [];
      if (!conv.instructed) {
        prompt.push({ type: "text", text: `<instructions>\n${INSTRUCTIONS}\n</instructions>` });
        conv.instructedBy = "prompt";
      }
      if (text) {
        conv.setStatus("Reading the page…");
        const info = await text;
        conv.pageText = null;
        if (info?.text) {
          const block = this.pageBlock(conv, page, info, user);
          if (block) {
            prompt.push(block);
          }
          seen = { url: page.url, kind: info.kind, text: info.text };
        }
      }
      prompt.push(...attachments.map(attachmentBlock));
      prompt.push({ type: "text", text: promptText(question, selection) });
      conv.setStatus("Thinking…");
      const result = await conn.request("session/prompt", {
        sessionId: conv.sessionId,
        prompt,
      });
      conv.instructed = true;
      if (seen) {
        conv.sentPages.set(seen.url, { kind: seen.kind, text: seen.text });
      }
      user.after = { sentPages: new Map(conv.sentPages), instructed: true, instructedBy: conv.instructedBy };
      const stopped = result?.stopReason === "cancelled";
      if (conv.current) {
        conv.current.state = stopped ? "stopped" : "done";
        conv.touch(conv.current);
      } else if (stopped) {
        conv.push({ role: "note", text: "Stopped." });
      }
      if (result?.stopReason && !["end_turn", "cancelled"].includes(result.stopReason)) {
        conv.push({ role: "note", text: `Stopped: ${result.stopReason}` });
      }
    } catch (e) {
      if (conv.current) {
        conv.current.state = "done";
        conv.touch(conv.current);
      }
      let text = e.message || String(e);
      if (/log ?in|auth|unauthori[sz]ed/i.test(text)) {
        text += `\nLog in to ${conv.harness === "claude" ? "Claude Code (claude /login)" : "Codex (codex login)"} in Terminal, then ask again.`;
      } else if (/agent stopped|not running/.test(text) && this.lastExit) {
        text += `\n${this.lastExit}`;
      }
      conv.push({ role: "error", text });
    } finally {
      this.settlePermissions(conv);
      conv.busy = false;
      conv.current = null;
      conv.lastUsed = Date.now();
      if (conn) {
        conn.lastUsed = conv.lastUsed;
      }
      conv.setStatus("");
      conv.emit({ type: "busy" });
    }
  },

  cancel(conv) {
    if (!conv.busy) {
      return;
    }
    this.settlePermissions(conv);
    if (conv.sessionId && conv.conn && !conv.conn.exited) {
      conv.conn.notify("session/cancel", { sessionId: conv.sessionId });
      conv.setStatus("Stopping…");
    }
  },

  /** New chat: drop the session and the messages, and the composer's draft
   * unless `keepDraft`. */
  reset(conv, { keepDraft = false } = {}) {
    this.cancel(conv);
    this.drop(conv);
    conv.lineage.clear();
    conv.instructed = false;
    conv.instructedBy = null;
    conv.messages = [];
    conv.current = null;
    if (!keepDraft) {
      conv.draft = null;
    }
    conv.pageText = null;
    conv.sentPages.clear();
    conv.emit({ type: "reset" });
  },

  answerPermission(conv, message, optionId) {
    if (message.answer !== null) {
      return;
    }
    message.answer = optionId ?? "cancelled";
    message.conn.respond(message.rpcId, {
      outcome: optionId ? { outcome: "selected", optionId } : { outcome: "cancelled" },
    });
    conv.touch(message);
  },

  settlePermissions(conv) {
    for (const message of conv.messages) {
      if (message.role === "permission" && message.answer === null) {
        this.answerPermission(conv, message, null);
      }
    }
  },

  onNotification(message) {
    if (message.method !== "session/update") {
      return;
    }
    const { sessionId, update } = message.params ?? {};
    const conv = this.bySession.get(sessionId);
    if (!conv || !update) {
      return;
    }
    if (update.sessionUpdate === "config_option_update") {
      if (update.configOptions?.length) {
        conv.configOptions = update.configOptions;
        this.remember(conv);
        conv.emit({ type: "options" });
      }
      return;
    }
    if (!conv.busy) {
      return;
    }
    switch (update.sessionUpdate) {
      case "agent_message_chunk": {
        const text = update.content?.type === "text" ? update.content.text : "";
        if (!text) {
          return;
        }
        // Text after a tool call starts a new bubble, so the order reads right,
        // and so does another message of the agent: each bubble keeps the
        // agent's id for its message, which editing a question forks at.
        const messageId = update.messageId ?? null;
        let reply = conv.current;
        if (!reply || conv.messages.at(-1) !== reply || (messageId && reply.messageId && messageId !== reply.messageId)) {
          if (reply) {
            reply.state = "done"; // the text before the tool call is complete
            conv.touch(reply);
          }
          reply = conv.push({ role: "assistant", text: "", state: "streaming", session: sessionId, messageId });
          conv.current = reply;
        }
        reply.messageId ??= messageId;
        reply.text += text;
        if (conv.status) {
          conv.setStatus("");
        }
        conv.touch(reply);
        break;
      }
      case "agent_thought_chunk":
        if (conv.status !== "Thinking…") {
          conv.setStatus("Thinking…");
        }
        break;
      case "tool_call":
        conv.push({
          role: "tool",
          id: update.toolCallId,
          title: update.title || update.kind || "Tool",
          kind: update.kind || "other",
          status: update.status || "pending",
        });
        break;
      case "tool_call_update": {
        const tool = conv.messages.findLast(m => m.role === "tool" && m.id === update.toolCallId);
        if (tool) {
          if (update.status) {
            tool.status = update.status;
          }
          if (update.title) {
            tool.title = update.title;
          }
          conv.touch(tool);
        }
        break;
      }
    }
  },

  onRequest(conn, message) {
    if (message.method === "session/request_permission") {
      const conv = this.bySession.get(message.params?.sessionId);
      if (!conv) {
        conn.respond(message.id, { outcome: { outcome: "cancelled" } });
        return;
      }
      const call = message.params.toolCall ?? {};
      conv.push({
        role: "permission",
        rpcId: message.id,
        conn,
        title: call.title || call.kind || "an action",
        options: (message.params.options ?? []).map(o => ({
          id: o.optionId,
          name: o.name,
          kind: o.kind,
        })),
        answer: null,
      });
      return;
    }
    conn.respondError(message.id, -32601, `${message.method} is not supported by this client`);
  },

  onExit(conn, code) {
    if (this.conns.get(conn.harness) === conn) {
      this.conns.delete(conn.harness);
    }
    if (conn.stopping) {
      return;
    }
    const tail = conn.stderrTail(3);
    this.lastExit = tail ? `Exit code ${code}: ${tail}` : `Exit code ${code}.`;
    debug(conn.harness, this.lastExit);
    // The agent's transcripts outlive its process: the next question tries
    // to resume each session.
    for (const conv of this.conversations.values()) {
      if (conv.conn === conn) {
        this.bySession.delete(conv.sessionId);
        conv.parkedId = conv.sessionId;
        conv.sessionId = null;
        conv.conn = null;
        conv.configOptions = null;
        conv.emit({ type: "options" });
      }
    }
  },

  shutdown() {
    if (this.saveTimer) {
      this.saveOptions();
    }
    clearInterval(this.sweeper);
    this.sweeper = null;
    for (const conn of [...this.conns.values()]) {
      this.stop(conn);
    }
  },
};
