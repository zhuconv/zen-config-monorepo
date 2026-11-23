/* Shared side of Claude Accounts: one copy for all windows.
 *  - reads the account table (~/.config/claude-accounts/accounts.json, or the
 *    file the pref uc.claude-accounts.config names);
 *  - keeps one container per account ("Claude · <alias>");
 *  - opens the links of an account that lives in a Chrome profile (the
 *    table's `chrome` field) in that profile, through /usr/bin/open;
 *  - remembers which account opened which artifact
 *    (<profile>/claude-accounts-memory.json);
 *  - watches claude.ai's artifact requests and tells the windows how each one
 *    went, so a window can offer another account when one fails.
 */

import { clearTimeout, setTimeout } from "resource://gre/modules/Timer.sys.mjs";

const PREF = "uc.claude-accounts.";
// The order new accounts get container colors in. Firefox renamed some colors
// (turquoise is cyan since its color refresh): each name goes through the
// container service, which knows this build's names.
const COLORS = ["orange", "turquoise", "green", "purple", "pink", "blue", "yellow", "red"];
const ALIAS_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
// The request an artifact page makes for its content: 200 when this login can
// open it, 404 when it can't or the artifact doesn't exist (claude.ai answers
// both the same way). Its siblings (/api/frame/member-org, /invitation/...,
// /telemetry) don't match.
const FRAME_RE =
  /^https:\/\/claude\.ai\/api\/frame\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[?#]|$)/;
const SESSION_COOKIES = new Set(["sessionKey", "sessionKeyV3"]);
const MEMORY_FILE = "claude-accounts-memory.json";
const MEMORY_MAX = 500;
const RESPONSE_TOPICS = [
  "http-on-examine-response",
  "http-on-examine-cached-response",
  "http-on-examine-merged-response",
];

/** The uuid an artifact id stands for: claude.ai's short ids are the uuid's
 *  128 bits in base58, so /artifact/<short> and /code/artifact/<uuid> are the
 *  same artifact. */
export function artifactUuid(id) {
  if (typeof id !== "string" || !id) {
    return null;
  }
  const lower = id.toLowerCase();
  if (UUID_RE.test(lower)) {
    return lower;
  }
  let n = 0n;
  for (const ch of id) {
    const digit = B58.indexOf(ch);
    if (digit < 0) {
      return null;
    }
    n = n * 58n + BigInt(digit);
  }
  if (n >= 1n << 128n) {
    return null;
  }
  const h = n.toString(16).padStart(32, "0");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// Takes `acct=<alias>` out of a query or fragment ("a=1&acct=gt" -> "a=1"),
// leaving every other part as it was written.
function takeTag(text) {
  if (!text) {
    return { tag: null, rest: "" };
  }
  let tag = null;
  const rest = text.split("&").filter(part => {
    const m = /^acct=(.*)$/.exec(part);
    if (!m) {
      return true;
    }
    if (tag === null) {
      try {
        tag = decodeURIComponent(m[1].replace(/\+/g, " ")).trim();
      } catch {
        tag = m[1];
      }
    }
    return false;
  });
  return { tag: tag || null, rest: rest.join("&") };
}

/**
 * What a link is to this mod, or null for a link it leaves alone.
 *  kind: "login" for Claude Code's sign-in pages, "claude" for any other
 *        claude.ai page;
 *  url: the link without its account tag;
 *  tag: the alias in `#acct=` (or `?acct=`), put there by the Claude Code mod;
 *  loginHint: the email `claude auth login --email` puts on a sign-in link;
 *  uuid: the artifact's uuid, for an artifact link.
 */
export function parseLink(spec) {
  let url;
  try {
    url = new URL(spec);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port) {
    return null;
  }
  const host = url.hostname.toLowerCase();
  const path = url.pathname;
  let kind = null;
  if (host === "claude.ai") {
    kind = /^\/oauth\/authorize(\/|$)/.test(path) ? "login" : "claude";
  } else if ((host === "claude.com" || host === "www.claude.com") && /^\/cai\/oauth\/authorize(\/|$)/.test(path)) {
    kind = "login";
  } else if (host === "platform.claude.com" && /^\/oauth\/authorize(\/|$)/.test(path)) {
    kind = "login";
  }
  if (!kind) {
    return null;
  }

  const fromHash = takeTag(url.hash.slice(1));
  const fromQuery = takeTag(url.search.slice(1));
  url.hash = fromHash.rest;
  url.search = fromQuery.rest;
  const tag = fromHash.tag ?? fromQuery.tag;

  let uuid = null;
  if (kind === "claude") {
    const m = /^\/(?:code\/)?(?:artifact|artifacts|public\/artifacts)\/([A-Za-z0-9-]+)\/?$/.exec(path);
    uuid = m ? artifactUuid(m[1]) : null;
  }
  const hint = kind === "login" ? url.searchParams.get("login_hint") : null;

  return {
    kind,
    url: url.href,
    tag: tag && ALIAS_RE.test(tag) ? tag : null,
    badTag: tag && !ALIAS_RE.test(tag) ? tag : null,
    loginHint: hint && hint.includes("@") ? hint.trim() : null,
    uuid,
  };
}

function expandHome(path) {
  if (path === "~" || path.startsWith("~/")) {
    // PathUtils.join takes one name per argument and throws on "a/b".
    const names = path.slice(2).split("/").filter(Boolean);
    return PathUtils.join(Services.dirsvc.get("Home", Ci.nsIFile).path, ...names);
  }
  return path;
}

function normalizeAccounts(raw) {
  const list = Array.isArray(raw) ? raw : raw?.accounts;
  if (!Array.isArray(list)) {
    throw new Error('the file needs an "accounts" list');
  }
  const seen = new Set();
  const accounts = [];
  for (const entry of list) {
    const alias = typeof entry?.alias === "string" ? entry.alias.trim() : "";
    if (!ALIAS_RE.test(alias) || seen.has(alias.toLowerCase())) {
      console.warn(`[claude-accounts] skipping an account with a missing or repeated alias: ${JSON.stringify(entry?.alias)}`);
      continue;
    }
    seen.add(alias.toLowerCase());
    const email = typeof entry.email === "string" && entry.email.includes("@") ? entry.email.trim() : null;
    const org = typeof entry.org === "string" && UUID_RE.test(entry.org.trim().toLowerCase()) ? entry.org.trim().toLowerCase() : null;
    const color = typeof entry.color === "string" && entry.color.trim() ? entry.color.trim() : null;
    const container =
      typeof entry.container === "string" && entry.container.trim() ? entry.container.trim() : `Claude · ${alias}`;
    const chrome = typeof entry.chrome === "string" && entry.chrome.trim() ? entry.chrome.trim() : null;
    accounts.push({ alias, email, org, color, container, chrome, userContextId: null });
  }
  return accounts;
}

export const ClaudeAccounts = {
  /** The accounts, in the table's order, each with its container's id. */
  accounts: [],
  /** Why the table couldn't be used, or null. */
  problem: null,

  _cis: null,
  _stamp: null,
  _refreshing: null,
  _memory: null,
  _memoryLoad: null,
  _saveTimer: null,
  _listeners: new Set(),
  _observing: false,

  /** Called by each window; resolves once the table and the memory are read. */
  init({ ContextualIdentityService }) {
    this._cis ??= ContextualIdentityService;
    this._observe();
    return Promise.all([this.refresh(), this._loadMemory()]);
  },

  configPath() {
    const pref = Services.prefs.getStringPref(PREF + "config", "");
    return expandHome(pref || "~/.config/claude-accounts/accounts.json");
  },

  /** Reads the table again when the file changed since the last read. */
  refresh() {
    this._refreshing ??= this._refresh().finally(() => {
      this._refreshing = null;
    });
    return this._refreshing;
  },

  async _refresh() {
    let path;
    try {
      path = this.configPath();
    } catch (e) {
      this._stamp = null;
      this.accounts = [];
      this.problem = `can't find the account table: ${e.message}`;
      console.error("[claude-accounts]", this.problem);
      return;
    }
    let stamp = "missing";
    try {
      const info = await IOUtils.stat(path);
      stamp = `${path}:${info.lastModified}:${info.size}`;
    } catch {}
    if (stamp === this._stamp) {
      return;
    }
    this._stamp = stamp;
    if (stamp === "missing") {
      this.accounts = [];
      this.problem = `no account table at ${path}`;
      return;
    }
    try {
      this.accounts = normalizeAccounts(await IOUtils.readJSON(path));
      this.problem = this.accounts.length ? null : `no accounts in ${path}`;
      this._ensureContainers();
    } catch (e) {
      this.accounts = [];
      this.problem = `can't use ${path}: ${e.message}`;
      console.error("[claude-accounts]", this.problem);
    }
  },

  // Finds or makes each account's container. A container the person renamed
  // or recolored stays as they left it: the pref remembers it by id.
  _ensureContainers() {
    const cis = this._cis;
    if (!cis) {
      throw new Error("ContextualIdentityService is not available");
    }
    cis.ensureDataReady?.();
    let map = {};
    try {
      map = JSON.parse(Services.prefs.getStringPref(PREF + "containers", "{}")) || {};
    } catch {}
    const known = cis.getPublicIdentities();
    const resolve = c => cis.resolveContainerColor?.(c) ?? c;
    const valid = new Set((cis.containerColors ?? COLORS).map(resolve));
    const palette = COLORS.map(resolve).filter(c => valid.has(c));
    this.accounts.forEach((account, i) => {
      const asked = account.color && resolve(account.color);
      account.color = asked && valid.has(asked) ? asked : palette[i % palette.length];
    });
    for (const account of this.accounts) {
      let identity = Number.isInteger(map[account.alias]) ? cis.getPublicIdentityFromId(map[account.alias]) : null;
      identity ??= known.find(i => i.name === account.container) ?? null;
      identity ??= cis.create(account.container, "circle", account.color);
      account.userContextId = identity.userContextId;
      account.color = resolve(identity.color || account.color);
      account.container = identity.name || account.container;
      map[account.alias] = identity.userContextId;
    }
    Services.prefs.setStringPref(PREF + "containers", JSON.stringify(map));
  },

  byAlias(alias) {
    if (!alias) {
      return null;
    }
    const lower = String(alias).toLowerCase();
    return this.accounts.find(a => a.alias.toLowerCase() === lower) ?? null;
  },

  byEmail(email) {
    if (!email) {
      return null;
    }
    const lower = String(email).toLowerCase();
    return this.accounts.find(a => a.email?.toLowerCase() === lower) ?? null;
  },

  byUserContextId(id) {
    if (!id) {
      return null;
    }
    return this.accounts.find(a => a.userContextId === id) ?? null;
  },

  /** The link to load for an account: an artifact gets the account's
   *  organization (claude.ai's own `?org=`), so an account in several
   *  organizations opens it in the right one. */
  urlFor(link, account) {
    const url = new URL(link.url);
    if (link.uuid && account?.org && !url.searchParams.has("org")) {
      url.searchParams.set("org", account.org);
    }
    return url.href;
  },

  /** true / false when the account's container has / lacks a claude.ai
   *  login, null when that can't be told. */
  isSignedIn(account) {
    if (!Number.isInteger(account?.userContextId)) {
      return null;
    }
    try {
      return Services.cookies
        .getCookiesFromHost("claude.ai", { userContextId: account.userContextId })
        .some(c => SESSION_COOKIES.has(c.name));
    } catch {
      return null;
    }
  },

  // --- Accounts that live in a Chrome profile --------------------------------

  /** Chrome's list of its profiles. Tests point this at a file of their own. */
  chromeStatePath: null,

  /** The folder of the Chrome profile an account's `chrome` value names: the
   *  folder itself ("Profile 10", as chrome://version shows it) or the
   *  profile's name ("JIANING"). Null when Chrome has no such profile. */
  async chromeProfileDir(account) {
    const wanted = account?.chrome;
    if (!wanted) {
      return null;
    }
    let profiles = {};
    try {
      const path = this.chromeStatePath ?? expandHome("~/Library/Application Support/Google/Chrome/Local State");
      profiles = (await IOUtils.readJSON(path))?.profile?.info_cache ?? {};
    } catch {}
    if (Object.hasOwn(profiles, wanted)) {
      return wanted;
    }
    const lower = wanted.toLowerCase();
    const named = Object.keys(profiles).filter(dir => String(profiles[dir]?.name ?? "").toLowerCase() === lower);
    return named.length === 1 ? named[0] : null;
  },

  /** Opens `url` in Google Chrome, in the account's profile. Resolves to the
   *  profile's folder; rejects when Chrome has no such profile. */
  async openInChrome(account, url) {
    const dir = await this.chromeProfileDir(account);
    if (!dir) {
      throw new Error(`Chrome has no profile "${account.chrome}"`);
    }
    // -n starts a second Chrome even while one runs; it hands its arguments
    // to the running one and quits, so the profile and link get through.
    await this.launch(["-na", "Google Chrome", "--args", `--profile-directory=${dir}`, url]);
    return dir;
  },

  /** Runs /usr/bin/open with these arguments. Tests replace it. */
  async launch(args) {
    const { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
    const proc = await Subprocess.call({ command: "/usr/bin/open", arguments: args, stderr: "stdout" });
    let output = "";
    for (let chunk; (chunk = await proc.stdout.readString()); ) {
      output += chunk;
    }
    const { exitCode } = await proc.wait();
    if (exitCode !== 0) {
      throw new Error(`open exited with ${exitCode}${output.trim() ? `: ${output.trim()}` : ""}`);
    }
  },

  lastAlias() {
    return Services.prefs.getStringPref(PREF + "last-alias", "") || null;
  },

  setLastAlias(alias) {
    Services.prefs.setStringPref(PREF + "last-alias", alias);
  },

  // --- Which account opened which artifact ----------------------------------

  _memoryPath() {
    return PathUtils.join(PathUtils.profileDir, MEMORY_FILE);
  },

  _loadMemory() {
    this._memoryLoad ??= (async () => {
      let data = null;
      try {
        data = await IOUtils.readJSON(this._memoryPath());
      } catch {}
      this._memory = data && typeof data.artifacts === "object" && data.artifacts ? data : { version: 1, artifacts: {} };
    })();
    return this._memoryLoad;
  },

  /** The alias that last opened this artifact, or null. */
  memoryFor(uuid) {
    const entry = uuid && this._memory?.artifacts[uuid];
    return entry && this.byAlias(entry.alias) ? entry.alias : null;
  },

  /** Notes that `alias` could (ok) or couldn't open the artifact. */
  remember(uuid, alias, ok) {
    if (!uuid || !alias || !this._memory) {
      return;
    }
    const artifacts = this._memory.artifacts;
    if (ok) {
      artifacts[uuid] = { alias, at: Date.now() };
      const keys = Object.keys(artifacts);
      if (keys.length > MEMORY_MAX) {
        keys
          .sort((a, b) => artifacts[a].at - artifacts[b].at)
          .slice(0, keys.length - MEMORY_MAX)
          .forEach(k => delete artifacts[k]);
      }
    } else if (artifacts[uuid]?.alias === alias) {
      delete artifacts[uuid];
    } else {
      return;
    }
    clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this.saveMemory(), 1000);
  },

  async saveMemory() {
    clearTimeout(this._saveTimer);
    this._saveTimer = null;
    if (!this._memory) {
      return;
    }
    try {
      await IOUtils.writeJSON(this._memoryPath(), this._memory, { tmpPath: this._memoryPath() + ".tmp" });
    } catch (e) {
      console.error("[claude-accounts] could not save what opened which artifact", e);
    }
  },

  // --- Links from other apps --------------------------------------------------

  /**
   * Every link another app opens reaches a window as
   * browserDOMWindow.openURI(uri, null, where, OPEN_EXTERNAL, principal). The
   * window's object is an XPConnect wrapper whose methods are read-only, so
   * the hook goes on the class: once, for every window, asking the window the
   * call is for (`this.win`) whether its Claude Accounts takes the link. A
   * window without one (or after it unloads) gets the link as before.
   */
  hookOpenURI() {
    const MARK = Symbol.for("zen-claude-accounts-openURI");
    const INSTANCE = Symbol.for("zen-claude-accounts");
    let proto = null;
    for (const url of ["resource:///modules/BrowserDOMWindow.sys.mjs", "moz-src:///browser/components/BrowserDOMWindow.sys.mjs"]) {
      try {
        proto = ChromeUtils.importESModule(url).BrowserDOMWindow?.prototype ?? null;
      } catch {}
      if (proto) {
        break;
      }
    }
    if (typeof proto?.openURI !== "function") {
      throw new Error("BrowserDOMWindow.prototype.openURI not found");
    }
    if (proto.openURI[MARK]) {
      return;
    }
    const original = proto.openURI;
    const hook = function (aURI, aOpenWindowInfo, aWhere, aFlags, ...rest) {
      try {
        if (aURI && !aOpenWindowInfo && aFlags & Ci.nsIBrowserDOMWindow.OPEN_EXTERNAL) {
          if (this.win?.[INSTANCE]?.takeExternal?.(aURI.spec)) {
            return null;
          }
        }
      } catch (e) {
        console.error("[claude-accounts] could not take the link; it opens as usual", e);
      }
      return original.call(this, aURI, aOpenWindowInfo, aWhere, aFlags, ...rest);
    };
    hook[MARK] = true;
    proto.openURI = hook;
  },

  // --- How each artifact request went ----------------------------------------

  /** fn(browser, uuid, status) runs for each artifact request a tab makes. */
  addListener(fn) {
    this._listeners.add(fn);
    return () => this._listeners.delete(fn);
  },

  _observe() {
    if (this._observing) {
      return;
    }
    this._observing = true;
    for (const topic of RESPONSE_TOPICS) {
      Services.obs.addObserver(this, topic);
    }
  },

  observe(subject) {
    let channel;
    try {
      channel = subject.QueryInterface(Ci.nsIHttpChannel);
    } catch {
      return;
    }
    let match;
    try {
      if (channel.URI.asciiHost !== "claude.ai") {
        return;
      }
      match = FRAME_RE.exec(channel.URI.spec);
    } catch {
      return;
    }
    if (!match) {
      return;
    }
    let status;
    try {
      status = channel.responseStatus;
    } catch {
      return;
    }
    const browser = channel.loadInfo?.browsingContext?.top?.embedderElement;
    if (browser) {
      this.notify(browser, match[1], status);
    }
  },

  /** Hands one artifact request's outcome to the windows. */
  notify(browser, uuid, status) {
    for (const fn of this._listeners) {
      try {
        fn(browser, uuid, status);
      } catch (e) {
        console.error("[claude-accounts]", e);
      }
    }
  },

  QueryInterface: ChromeUtils.generateQI(["nsIObserver"]),
};
