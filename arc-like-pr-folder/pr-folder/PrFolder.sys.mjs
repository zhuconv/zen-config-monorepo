// PR Live Folder, the side all windows share. See ../README.md.
/* Makes Zen's GitHub pull-request live folders work like Arc's:
 *  - they list only PRs with activity in the last `uc.pr-folder.recent-days`
 *    days (14), and "Created by Me" and "My Review Requests" start ticked;
 *  - they can also list PRs by others that you haven't reviewed, in the repos
 *    and accounts named in `uc.pr-folder.review-in` (none at first);
 *  - they refresh when Zen comes to the front, when you switch to the
 *    folder's space, when you open a PR page, and every
 *    `uc.pr-folder.poll-seconds` (120) while you use the Mac. The automatic
 *    refreshes come at most once every `uc.pr-folder.min-gap-seconds` (10);
 *  - a PR page you open in the folder's space moves into the folder, in place
 *    of its placeholder (`uc.pr-folder.adopt-opened-prs`);
 *  - the PR on screen stays until you leave it, even when it merged meanwhile;
 *  - a PR that leaves the list and comes back shows again. (In an open
 *    folder, Zen alone counts its own closing of the PR's tab as you
 *    dismissing it, and keeps it hidden.)
 * It builds on Zen's own live folder code (ZenLiveFoldersManager and its
 * GitHub provider) by wrapping three of its methods. detach() puts them back.
 */

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  ZenLiveFoldersManager: "resource:///modules/zen/ZenLiveFoldersManager.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  setInterval: "resource://gre/modules/Timer.sys.mjs",
  clearInterval: "resource://gre/modules/Timer.sys.mjs",
});

export const PREF = "uc.pr-folder.";
const DEFAULTS = {
  "recent-days": 14,
  "poll-seconds": 120,
  "min-gap-seconds": 10,
  "adopt-opened-prs": true,
  "review-in": "",
};
// Where the wrapped methods' originals wait. Symbol.for keys, so that another
// copy of this module (loaded from another address) finds them too.
const ORIGINALS = Symbol.for("arc-like-pr-folder.originals");
export const WRAPPED = Symbol.for("arc-like-pr-folder.wrapper");
const ITEM_ATTR = "zen-live-folder-item-id";
// No polling once the Mac has had no input for this long.
const IDLE_LIMIT_MS = 10 * 60 * 1000;
// A PR page you open asks GitHub again at most this often.
const PR_OPENED_GAP_MS = 3000;
const DAY_MS = 24 * 60 * 60 * 1000;

const prefInt = name => Services.prefs.getIntPref(PREF + name, DEFAULTS[name]);
const prefBool = name => Services.prefs.getBoolPref(PREF + name, DEFAULTS[name]);
const prefString = name => Services.prefs.getStringPref(PREF + name, DEFAULTS[name]);
const sameItem = (a, b) => a.toLowerCase() === b.toLowerCase();
const report = (what, error) => console.error(`[pr-folder] ${what}:`, error);
// The window an element is in (documentGlobal in current Firefox).
const windowOf = element => element.documentGlobal ?? element.ownerGlobal ?? element.ownerDocument?.defaultView;

/**
 * "owner/repo#123" for a page of a github.com pull request (its conversation,
 * files, commits…), else null.
 */
export function prKeyFromUrl(spec) {
  let url;
  try {
    url = new URL(spec);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !["github.com", "www.github.com"].includes(url.hostname)) {
    return null;
  }
  const match = url.pathname.match(/^\/([^/]+\/[^/]+)\/pull\/(\d+)(?:\/|$)/);
  return match ? `${match[1]}#${match[2]}` : null;
}

/**
 * The search URL `href` with `updated:>=<date>` added to its q, the date
 * `days` days before `now`. Unchanged when days is 0, or q has no query or
 * already limits the update time.
 */
export function withRecentWindow(href, days, now = Date.now()) {
  if (!(days > 0)) {
    return href;
  }
  const url = new URL(href);
  const query = url.searchParams.get("q");
  if (!query || /(^|\s)updated:/.test(query)) {
    return href;
  }
  const since = new Date(now - days * DAY_MS).toISOString().slice(0, 10);
  url.searchParams.set("q", `${query} updated:>=${since}`);
  return url.href;
}

/**
 * The repos ("owner/repo") and accounts ("owner") named in `text`, separated
 * by commas or spaces. Anything else is left out.
 */
export function reviewScope(text) {
  const names = (text || "").split(/[\s,]+/).filter(name => /^[\w.-]+(\/[\w.-]+)?$/.test(name));
  return [...new Set(names)];
}

/**
 * The search for open, ready PRs by others that you haven't reviewed, in
 * `scope` (see reviewScope) and outside the repos in `excludes`.
 */
export function reviewQuery(scope, excludes = []) {
  const where = scope.map(name => (name.includes("/") ? `repo:${name}` : `user:${name}`));
  return [
    "is:pr",
    "is:open",
    "archived:false",
    "draft:false",
    "sort:updated-desc",
    "-author:@me",
    "-reviewed-by:@me",
    ...excludes.filter(repo => repo?.trim()).map(repo => `-repo:${repo.trim()}`),
    where.length > 1 ? `(${where.join(" OR ")})` : where[0],
  ].join(" ");
}

class PrFolderController {
  #windows = new Set();
  #wrapped = false;
  #pollTimer = null;
  // Per live folder id: the refresh under way, one owed after it, and a
  // refresh waiting for its gap to pass.
  #inflight = new Map();
  #owed = new Set();
  #waiting = new Map();
  // Item ids ("<folder id>:<owner/repo#n>") the folder keeps only because
  // their tab is on screen, to the live folder they belong to.
  #keptForNow = new Map();
  #lastPrByTab = new WeakMap();
  #prefObserver = {
    observe: (subject, topic, name) => {
      if (name === PREF + "poll-seconds") {
        this.#startPolling();
      } else if (name === PREF + "recent-days" || name === PREF + "review-in") {
        this.refreshAll({ reason: "settings", force: true });
      }
    },
  };

  // The latest refresh this module started: { folderId, reason, at }.
  lastRefresh = null;

  get manager() {
    return lazy.ZenLiveFoldersManager;
  }

  // How long the Mac has had no keyboard or mouse input, in ms.
  idleMs() {
    try {
      return Cc["@mozilla.org/widget/useridleservice;1"].getService(Ci.nsIUserIdleService).idleTime;
    } catch {
      return 0;
    }
  }

  /** Starts working for a browser window; resolves to whether it could. */
  async attach(win) {
    this.#windows.add(win);
    await this.manager.stateRestored.promise;
    if (!this.#windows.has(win)) {
      return false;
    }
    this.#wrap();
    if (this.#wrapped) {
      this.#startPolling();
      this.refreshAll({ reason: "startup" });
    }
    return this.#wrapped;
  }

  /** Stops working for a window; with no window left, puts Zen back as it was. */
  detach(win) {
    this.#windows.delete(win);
    if (this.#windows.size) {
      return;
    }
    this.#stopPolling();
    for (const timer of this.#waiting.values()) {
      lazy.clearTimeout(timer);
    }
    this.#waiting.clear();
    this.#owed.clear();
    this.#keptForNow.clear();
    this.#unwrap();
  }

  /** The GitHub pull-request live folders. */
  prFolders() {
    return [...this.manager.liveFolders.values()].filter(
      liveFolder => liveFolder.constructor?.type === "github" && liveFolder.state?.type === "pull-requests"
    );
  }

  /**
   * Refreshes one live folder, unless it fetched less than `gapMs` ago. With
   * `trailing`, a refresh that comes too soon waits for the gap instead of
   * being dropped. `force` ignores the gap. One refresh per folder at a time:
   * a forced or trailing one asked for meanwhile runs right after.
   */
  refreshFolder(liveFolder, { reason = "manual", force = false, trailing = false, gapMs } = {}) {
    if (!this.#wrapped) {
      return null;
    }
    const id = liveFolder.id;
    if (this.#inflight.has(id)) {
      if (force || trailing) {
        this.#owed.add(id);
      }
      return this.#inflight.get(id);
    }
    gapMs ??= prefInt("min-gap-seconds") * 1000;
    const wait = (liveFolder.state?.lastFetched || 0) + gapMs - Date.now();
    if (!force && wait > 0) {
      if (trailing && !this.#waiting.has(id)) {
        const timer = lazy.setTimeout(() => {
          this.#waiting.delete(id);
          this.refreshFolder(liveFolder, { reason, force: true });
        }, wait);
        this.#waiting.set(id, timer);
      }
      return null;
    }
    const waiting = this.#waiting.get(id);
    if (waiting) {
      lazy.clearTimeout(waiting);
      this.#waiting.delete(id);
    }
    this.lastRefresh = { folderId: id, reason, at: Date.now() };
    const run = (async () => {
      try {
        await liveFolder.refresh();
      } catch (e) {
        report("refresh failed", e);
      }
    })().finally(() => {
      this.#inflight.delete(id);
      if (this.#owed.delete(id)) {
        // A tick later: Zen puts the tabs of a fetch in the folder a tick
        // after it makes them.
        lazy.setTimeout(() => {
          if (this.manager.getFolder(id) === liveFolder) {
            this.refreshFolder(liveFolder, { reason: "owed", force: true });
          }
        }, 0);
      }
    });
    this.#inflight.set(id, run);
    return run;
  }

  refreshAll(options = {}, which = () => true) {
    return Promise.all(this.prFolders().filter(which).map(liveFolder => this.refreshFolder(liveFolder, options)));
  }

  // --- What the windows report -----------------------------------------------

  onWindowActivated() {
    this.refreshAll({ reason: "focus" });
  }

  onSpaceChanged(win, workspace) {
    if (!workspace?.uuid) {
      return;
    }
    this.refreshAll(
      { reason: "space" },
      liveFolder => this.manager.getFolderForLiveFolder(liveFolder)?.getAttribute("zen-workspace-id") === workspace.uuid
    );
  }

  onTabSelect(win, event) {
    const itemId = event.detail?.previousTab?.getAttribute?.(ITEM_ATTR);
    const folderId = itemId && this.#keptForNow.get(itemId);
    if (!folderId) {
      return;
    }
    this.#keptForNow.delete(itemId);
    const liveFolder = this.manager.getFolder(folderId);
    if (liveFolder) {
      this.refreshFolder(liveFolder, { reason: "left-item", force: true });
    }
  }

  /** A tab's page changed address: if it's a PR, fold it in or ask GitHub. */
  onTabLocation(win, tab, spec) {
    if (!this.#wrapped) {
      return;
    }
    const key = prKeyFromUrl(spec);
    const last = this.#lastPrByTab.get(tab);
    this.#lastPrByTab.set(tab, key);
    // GitHub's own in-page navigation (conversation → files) is the same PR.
    if (!key || (last && sameItem(last, key)) || tab.hasAttribute(ITEM_ATTR)) {
      return;
    }
    for (const liveFolder of this.prFolders()) {
      const folder = this.manager.getFolderForLiveFolder(liveFolder);
      if (!folder) {
        continue;
      }
      const itemId = `${liveFolder.id}:${key}`;
      const placeholder = folder.tabs.find(t => t !== tab && sameItem(t.getAttribute(ITEM_ATTR) || "", itemId));
      if (placeholder) {
        if (prefBool("adopt-opened-prs") && windowOf(folder) === win && !placeholder.selected && this.#adoptable(tab, folder)) {
          this.#swap(placeholder, tab, folder);
        }
        continue;
      }
      // Not in the folder yet: perhaps you just opened it. Ask GitHub soon.
      this.refreshFolder(liveFolder, { reason: "pr-opened", trailing: true, gapMs: PR_OPENED_GAP_MS });
    }
  }

  // --- Zen's methods, wrapped --------------------------------------------------

  #wrap() {
    if (this.#wrapped) {
      return;
    }
    const manager = this.manager;
    const Provider = manager.registry.get("github");
    if (!Provider) {
      report("no GitHub live folders in this Zen", new Error("registry has no github provider"));
      return;
    }
    const self = this;
    const proto = Provider.prototype;
    const originals = proto[ORIGINALS] ?? {
      fetchItems: proto.fetchItems,
      parsePullRequests: proto.parsePullRequests,
    };
    proto[ORIGINALS] = originals;

    // Ticks the Arc defaults just before a fetch builds its query, and adds
    // the PRs waiting in the repos of `review-in` to what it finds.
    const fetchItems = async function (...args) {
      try {
        self.#applyDefaultFilters(this);
      } catch (e) {
        report("setting the default filters failed", e);
      }
      const items = await originals.fetchItems.apply(this, args);
      if (this.state?.type !== "pull-requests" || !Array.isArray(items)) {
        return items;
      }
      try {
        return await self.#withReviewItems(this, items);
      } catch (e) {
        report("the search in review-in failed", e);
        return items;
      }
    };
    // Adds the time window to each search the folder sends.
    const parsePullRequests = function (url, ...rest) {
      let href = url;
      try {
        if (this.state?.type === "pull-requests") {
          href = withRecentWindow(url, prefInt("recent-days"));
        }
      } catch (e) {
        report("adding the time window failed", e);
      }
      return originals.parsePullRequests.call(this, href, ...rest);
    };
    fetchItems[WRAPPED] = parsePullRequests[WRAPPED] = true;
    proto.fetchItems = fetchItems;
    proto.parsePullRequests = parsePullRequests;

    // Sees each fetch's items before Zen updates the folder from them.
    const onFetchOriginal = manager[ORIGINALS] ?? manager.onLiveFolderFetch;
    manager[ORIGINALS] = onFetchOriginal;
    const onLiveFolderFetch = function (liveFolder, items) {
      try {
        items = self.#beforeFolderUpdate(liveFolder, items);
      } catch (e) {
        report("preparing the folder update failed", e);
      }
      return onFetchOriginal.call(this, liveFolder, items);
    };
    onLiveFolderFetch[WRAPPED] = true;
    manager.onLiveFolderFetch = onLiveFolderFetch;

    Services.prefs.addObserver(PREF, this.#prefObserver);
    this.#wrapped = true;
  }

  #unwrap() {
    if (!this.#wrapped) {
      return;
    }
    this.#wrapped = false;
    Services.prefs.removeObserver(PREF, this.#prefObserver);
    const manager = this.manager;
    const proto = manager.registry.get("github")?.prototype;
    const originals = proto?.[ORIGINALS];
    if (originals) {
      proto.fetchItems = originals.fetchItems;
      proto.parsePullRequests = originals.parsePullRequests;
      delete proto[ORIGINALS];
    }
    const onFetchOriginal = manager[ORIGINALS];
    if (onFetchOriginal) {
      // The original lives on the manager's class; dropping the wrapper
      // uncovers it.
      delete manager.onLiveFolderFetch;
      if (manager.onLiveFolderFetch !== onFetchOriginal) {
        manager.onLiveFolderFetch = onFetchOriginal;
      }
      delete manager[ORIGINALS];
    }
  }

  // "Created by Me" and "My Review Requests" start ticked, as in Arc. A box
  // you untick stays unticked.
  #applyDefaultFilters(liveFolder) {
    if (liveFolder.state?.type !== "pull-requests") {
      return;
    }
    const options = (liveFolder.state.options ??= {});
    let changed = false;
    for (const key of ["authorMe", "reviewRequested"]) {
      if (typeof options[key] !== "boolean") {
        options[key] = true;
        changed = true;
      }
    }
    if (changed) {
      liveFolder.requestSave();
    }
  }

  // Adds the open, ready PRs by others that you haven't reviewed, in the
  // repos and accounts of `review-in`, to a fetch's items. A failed search
  // fails the whole fetch, which leaves the folder as it is until the next one.
  async #withReviewItems(liveFolder, items) {
    const scope = reviewScope(prefString("review-in"));
    if (!scope.length) {
      return items;
    }
    const url = new URL(liveFolder.state.url);
    url.searchParams.set("q", reviewQuery(scope, [...(liveFolder.state.options?.repoExcludes ?? [])]));
    const found = await liveFolder.parsePullRequests(url.href);
    if (found.status === 404) {
      return "zen-live-folder-github-no-auth";
    }
    if (found.status !== 200) {
      return "zen-live-folder-failed-fetch";
    }
    const known = new Set(items.map(item => item.id));
    const added = (found.items ?? []).filter(item => !known.has(item.id));
    for (const repo of found.activeRepos ?? []) {
      liveFolder.state.repos?.add?.(repo);
    }
    return items.concat(added);
  }

  // Runs before Zen updates a PR folder from a fetch's items. Closes the tabs
  // of PRs that left the list, except the one on screen, and moves open PR
  // tabs into the folder for new items, so Zen makes no second tab for them.
  #beforeFolderUpdate(liveFolder, items) {
    if (!Array.isArray(items) || liveFolder.state?.type !== "pull-requests") {
      return items;
    }
    const folder = this.manager.getFolderForLiveFolder(liveFolder);
    const win = folder && windowOf(folder);
    if (!win?.gBrowser) {
      return items;
    }
    const prefix = `${liveFolder.id}:`;
    // Tabs Zen made for an earlier fetch but puts in the folder only a tick
    // later. In the folder now, so that this update doesn't make them again.
    const pending = win.gBrowser.tabs.filter(
      tab => !tab.closing && tab.group !== folder && tab.getAttribute(ITEM_ATTR)?.startsWith(prefix)
    );
    if (pending.length) {
      folder.addTabs(pending);
    }
    const listed = new Set(items.map(item => prefix + item.id));
    const result = items.slice();
    for (const [itemId, folderId] of this.#keptForNow) {
      if (folderId === liveFolder.id) {
        this.#keptForNow.delete(itemId);
      }
    }
    const inFolder = new Set();
    const gone = [];
    for (const tab of folder.tabs) {
      const itemId = tab.getAttribute(ITEM_ATTR);
      if (!itemId || tab.closing) {
        continue;
      }
      if (listed.has(itemId)) {
        inFolder.add(itemId);
      } else if (tab.selected) {
        // Merged, closed or quiet for too long, but on screen: it goes once
        // you leave it (onTabSelect).
        inFolder.add(itemId);
        result.push({ id: itemId.slice(prefix.length), url: tab.linkedBrowser?.currentURI?.spec, title: tab.label });
        this.#keptForNow.set(itemId, liveFolder.id);
      } else {
        gone.push(tab);
      }
    }
    // Zen would close these tabs itself. In an open folder it animates the
    // close, and the close then counts as you dismissing the PR: one that
    // comes back (a box ticked again, a search that missed it once) would stay
    // hidden. Closed here without their item ids, they don't count as
    // dismissed.
    if (gone.length) {
      const goneIds = gone.map(tab => tab.getAttribute(ITEM_ATTR));
      for (const tab of gone) {
        tab.removeAttribute(ITEM_ATTR);
      }
      try {
        win.gBrowser.removeTabs(gone, { skipSessionStore: true, animate: !folder.collapsed });
      } catch (e) {
        // Leave them to Zen, item ids back on, rather than in the folder
        // without them.
        gone.forEach((tab, i) => tab.isConnected && !tab.closing && tab.setAttribute(ITEM_ATTR, goneIds[i]));
        report("closing the tabs of PRs that left the list failed", e);
      }
    }
    if (prefBool("adopt-opened-prs")) {
      const dismissed = this.manager.dismissedItems;
      for (const item of items) {
        const itemId = prefix + item.id;
        if (inFolder.has(itemId) || dismissed.has(itemId)) {
          continue;
        }
        const tab = this.#openTabFor(folder, item.id);
        if (tab) {
          this.#adopt(tab, folder, itemId, item.subtitle);
          inFolder.add(itemId);
        }
      }
    }
    return result;
  }

  // An ordinary tab of the folder's space and container showing the PR
  // `rawId` ("owner/repo#n"); the selected one if there are several.
  #openTabFor(folder, rawId) {
    let found = null;
    for (const tab of windowOf(folder).gBrowser.tabs) {
      const key = prKeyFromUrl(tab.linkedBrowser?.currentURI?.spec ?? "");
      if (!key || !sameItem(key, rawId) || !this.#adoptable(tab, folder)) {
        continue;
      }
      if (tab.selected) {
        return tab;
      }
      found ??= tab;
    }
    return found;
  }

  #adoptable(tab, folder) {
    return (
      !tab.closing &&
      !tab.pinned &&
      !tab.group &&
      !tab.splitview &&
      !tab.hasAttribute(ITEM_ATTR) &&
      !tab.hasAttribute("zen-essential") &&
      !tab.hasAttribute("zen-glance-tab") &&
      !tab.hasAttribute("zen-empty-tab") &&
      !tab.hasAttribute("split-view") &&
      tab.getAttribute("zen-workspace-id") === folder.getAttribute("zen-workspace-id") &&
      (tab.userContextId || 0) === this.#containerOf(folder)
    );
  }

  // The container the folder's space opens its tabs in (0: none).
  #containerOf(folder) {
    const space = windowOf(folder).gZenWorkspaces.getWorkspaceFromId(folder.getAttribute("zen-workspace-id"));
    return space?.containerTabId || 0;
  }

  // Makes `tab` the folder's tab for `itemId`, as Zen makes its own.
  #adopt(tab, folder, itemId, subtitle) {
    const win = windowOf(folder);
    tab.setAttribute(ITEM_ATTR, itemId);
    if (!tab.pinned) {
      win.gBrowser.pinTab(tab);
    }
    folder.addTabs([tab]);
    if (this.#containerOf(folder)) {
      tab.setAttribute("zenDefaultUserContextId", "true");
    }
    if (subtitle) {
      tab.setAttribute("zen-show-sublabel", subtitle);
      const label = tab.querySelector(".zen-tab-sublabel");
      if (label) {
        win.document.l10n.setArgs(label, { tabSubtitle: subtitle });
      }
    }
  }

  // You opened a PR the folder already has: your tab takes the place of the
  // folder's own one.
  #swap(placeholder, tab, folder) {
    const itemId = placeholder.getAttribute(ITEM_ATTR);
    this.#adopt(tab, folder, itemId, placeholder.getAttribute("zen-show-sublabel"));
    // Without its item id, closing it doesn't count as dismissing the PR.
    placeholder.removeAttribute(ITEM_ATTR);
    windowOf(folder).gBrowser.removeTab(placeholder, { animate: false, skipSessionStore: true });
    this.manager.saveState();
  }

  // --- Polling -------------------------------------------------------------------

  #startPolling() {
    this.#stopPolling();
    const seconds = prefInt("poll-seconds");
    if (!this.#wrapped || seconds <= 0) {
      return;
    }
    this.#pollTimer = lazy.setInterval(() => {
      if (this.idleMs() <= IDLE_LIMIT_MS) {
        this.refreshAll({ reason: "poll" });
      }
    }, seconds * 1000);
  }

  #stopPolling() {
    if (this.#pollTimer) {
      lazy.clearInterval(this.#pollTimer);
      this.#pollTimer = null;
    }
  }
}

export const PrFolder = new PrFolderController();
