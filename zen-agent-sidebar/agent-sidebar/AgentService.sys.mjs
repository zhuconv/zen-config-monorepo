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
const CLIENT = { name: "zen-agent-sidebar", title: "Zen Agent Sidebar", version: "0.3.0" };

/** Session settings the panel header offers: the model and the effort. */
export const MODEL_OPTION = "model";
export const EFFORT_OPTIONS = ["reasoning_effort", "effort"];

const INSTRUCTIONS = `You are a reading assistant inside the user's web browser (Zen).
The user is reading a web page or a PDF. With their question, the browser attaches:
- the page's text, as an embedded resource whose URI is the page URL. It is attached once per page in a conversation, so look back at it for follow-up questions.
- the passage they selected, inside <selection> tags, when they selected one.
Answer from the attached page first: it is exactly what the user is looking at. Search the web only when the question needs information the page does not contain.
Be concise and direct, and reply in the language of the user's question. Use Markdown, and write math in LaTeX between $...$ or $$...$$.
This is not a coding task: do not run shell commands, read local files, or edit anything unless the user explicitly asks you to.`;

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
  sentPages = new Set(); // page URLs whose text this session already has
  pageText = null; // { url, promise }: the page's text, read ahead while the user types
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

function resourceBlock(page, info) {
  const header = [`Title: ${page.title}`, `URL: ${page.url}`];
  if (info.kind === "pdf") {
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

  /** Resume the conversation's closed session, or start a new one. */
  async openSession(conv, conn) {
    const params = { cwd: conn.config.cwd || PathUtils.tempDir, mcpServers: [] };
    if (conn.config.sessionMeta) {
      params._meta = conn.config.sessionMeta;
    }
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
        params.systemPrompt = { mode: "append", content: [{ type: "text", text: INSTRUCTIONS }] };
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
    }
    conv.sessionId = sessionId;
    conv.conn = conn;
    conv.configOptions = result.configOptions ?? null;
    this.remember(conv);
    this.bySession.set(sessionId, conv);
    await this.applyPreferredOptions(conv);
    conv.emit({ type: "session" });
    this.limitOpenSessions(conv);
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

  /** The page's text, read once per page and shared by the read-ahead and send(). */
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

  /** The shortcut opened the panel: read the page while the user types. */
  prefetchPage(conv, page) {
    if (page?.url && page.getText && !conv.sentPages.has(page.url)) {
      this.readPage(conv, page);
    }
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

  /** The panel switched agents: a session cannot move, so start over. */
  setHarness(conv, harness) {
    Services.prefs.setStringPref(PREF_HARNESS, harness);
    if (conv.harness === harness) {
      return;
    }
    this.reset(conv);
    conv.harness = harness;
    conv.emit({ type: "session" });
    this.prewarm(conv);
  },

  /**
   * Ask a question. `page` is { url, title, getText } for the tab's page;
   * its text goes along the first time this conversation sees that URL.
   */
  async send(conv, { question, selection = "", page = null }) {
    if (conv.busy) {
      return;
    }
    conv.busy = true;
    conv.current = null;
    const user = conv.push({
      role: "user",
      text: question,
      selection,
      page: page ? { title: page.title, url: page.url } : null,
      attached: null,
    });
    conv.emit({ type: "busy" });
    let pageUrl = null;
    let conn = null;
    try {
      const name = this.config?.agents?.[conv.harness]?.name || conv.harness;
      conv.setStatus(conv.sessionId ? "" : `Starting ${name}…`);
      // Read the page while the session starts (the shortcut usually started
      // both already).
      const wantsText = () => page?.url && page.getText && !conv.sentPages.has(page.url);
      let text = wantsText() ? this.readPage(conv, page) : null;
      conn = await this.ensureSession(conv);
      if (!text && wantsText()) {
        text = this.readPage(conv, page); // the session started over
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
          prompt.push(resourceBlock(page, info));
          user.attached = { kind: info.kind, truncated: !!info.truncated };
          conv.touch(user);
          pageUrl = page.url;
        }
      }
      prompt.push({ type: "text", text: promptText(question, selection) });
      conv.setStatus("Thinking…");
      const result = await conn.request("session/prompt", {
        sessionId: conv.sessionId,
        prompt,
      });
      conv.instructed = true;
      if (pageUrl) {
        conv.sentPages.add(pageUrl);
      }
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

  /** New chat: drop the session and the messages. */
  reset(conv) {
    this.cancel(conv);
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
    conv.instructed = false;
    conv.instructedBy = null;
    conv.messages = [];
    conv.current = null;
    conv.draft = null;
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
        // Text after a tool call starts a new bubble, so the order reads right.
        let reply = conv.current;
        if (!reply || conv.messages.at(-1) !== reply) {
          reply = conv.push({ role: "assistant", text: "", state: "streaming" });
          conv.current = reply;
        }
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
