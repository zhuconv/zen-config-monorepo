// ==UserScript==
// @name           Claude Accounts
// @description    Opens claude.ai links from other apps in the right Claude account: one container per account, and a quick picker when the link doesn't say which.
// @include        main
// @ignorecache
// @version        0.1.0
// ==/UserScript==

/* Window side of Claude Accounts:
 *  - takes over claude.ai links (and Claude Code's sign-in links) that other
 *    apps open, and opens each in its account's container: the one the link's
 *    `#acct=<alias>` tag or `login_hint` names, else the one picked in a small
 *    panel (Enter for the preselected one, 1-9 for another, Esc to cancel);
 *    an account the table gives a Chrome profile opens in Chrome instead;
 *  - when an artifact doesn't open in an account (claude.ai's 404), shows a
 *    bar with buttons to open it in another;
 *  - adds "Open in Another Claude Account…" to the tab menu of claude.ai tabs
 *    and a toolbar button that does the same, or opens claude.ai in an account.
 * claude-accounts/ClaudeAccounts.sys.mjs keeps what all windows share.
 * Prefs: uc.claude-accounts.enabled (true),
 *        uc.claude-accounts.ask-for-tagged (false: a tagged link opens at once),
 *        uc.claude-accounts.config (the account table's path).
 */
(() => {
  "use strict";

  if (window.location.href !== "chrome://browser/content/browser.xhtml") {
    return;
  }

  const INSTANCE = Symbol.for("zen-claude-accounts");
  // A second load (an update, or a loader running the file twice) replaces the
  // first instance instead of stacking on top of it.
  window[INSTANCE]?.destroy();

  const SCRIPT_URL = Components.stack.filename.split(" -> ").pop();
  const BASE = SCRIPT_URL.slice(0, SCRIPT_URL.lastIndexOf("/") + 1) + "claude-accounts/";
  const { ClaudeAccounts, parseLink } = ChromeUtils.importESModule(BASE + "ClaudeAccounts.sys.mjs");

  const HTML = "http://www.w3.org/1999/xhtml";
  const PREF = "uc.claude-accounts.";
  const NOTICE = "claude-accounts-failed";
  const CHROME_NOTICE = "claude-accounts-chrome";
  const PANEL_ID = "claude-accounts-panel";
  const MENU_ID = "claude-accounts-reopen";
  const WIDGET_ID = "claude-accounts-button";
  const PANEL_WIDTH = 460;
  const ICON =
    "data:image/svg+xml," +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="context-fill">' +
        '<circle cx="6" cy="5" r="2.6"/><path d="M1.3 13.8c0-2.7 2.1-4.5 4.7-4.5s4.7 1.8 4.7 4.5z"/>' +
        '<path d="M10.6 3.4h4M13 1.6l1.8 1.8L13 5.2M14.6 7.6h-4m1.8-1.8-1.8 1.8 1.8 1.8" fill="none" ' +
        'stroke="context-fill" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    );
  const CSS = `
    #${PANEL_ID} {
      --panel-padding: 0;
      --panel-border-radius: 12px;
    }
    #${PANEL_ID} .ca-box {
      width: ${PANEL_WIDTH}px;
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 10px;
      font: menu;
    }
    #${PANEL_ID} .ca-title { font-weight: 600; font-size: 1.05em; padding: 2px 8px; }
    #${PANEL_ID} .ca-url {
      padding: 0 8px 6px; font-size: 0.9em; opacity: 0.65;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    #${PANEL_ID} .ca-row {
      appearance: none; display: flex; align-items: center; gap: 9px;
      margin: 0; padding: 7px 8px; border: none; border-radius: 8px;
      background: transparent; color: inherit; font: inherit; text-align: start;
    }
    #${PANEL_ID} .ca-row:not([disabled]):is(:hover, :focus) {
      background: color-mix(in srgb, currentColor 13%, transparent);
      outline: none;
    }
    #${PANEL_ID} .ca-row[disabled] { opacity: 0.45; }
    #${PANEL_ID} .ca-key { width: 1.1em; text-align: center; opacity: 0.6; font-family: ui-monospace, monospace; }
    #${PANEL_ID} .ca-dot {
      flex: none; width: 10px; height: 10px; border-radius: 50%;
      background: var(--identity-icon-color, currentColor);
    }
    #${PANEL_ID} .ca-alias { font-weight: 600; min-width: 3.2em; }
    #${PANEL_ID} .ca-email { flex: 1; opacity: 0.7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #${PANEL_ID} .ca-note { font-size: 0.85em; opacity: 0.85; white-space: nowrap; }
    #${PANEL_ID} .ca-note.ca-warn { color: #d97706; }
    #${PANEL_ID} .ca-foot { padding: 6px 8px 0; font-size: 0.85em; opacity: 0.55; }
    #${WIDGET_ID} { list-style-image: url("${ICON}"); }
  `;
  const SHEET = "data:text/css," + encodeURIComponent(CSS);

  const cleanups = [];
  let destroyed = false;
  let initialized = false;
  let queue = Promise.resolve();
  let openPanel = null; // { hide() } while the picker is open

  function destroy() {
    if (destroyed) {
      return;
    }
    destroyed = true;
    openPanel?.hide();
    for (const cleanup of cleanups.reverse()) {
      try {
        cleanup();
      } catch (e) {
        console.error("[claude-accounts] cleanup failed", e);
      }
    }
    if (window[INSTANCE]?.destroy === destroy) {
      delete window[INSTANCE];
    }
  }

  window[INSTANCE] = {
    destroy,
    takeExternal,
    handleLink,
    switcher,
    pickForTab,
    openIn,
    ClaudeAccounts,
    get initialized() {
      return initialized;
    },
  };
  // Sine calls this when the mod is disabled, updated or removed.
  window.addUnloadListener?.(destroy);
  window.addEventListener("unload", destroy, { once: true });
  cleanups.push(() => window.removeEventListener("unload", destroy));

  const enabled = () => Services.prefs.getBoolPref(PREF + "enabled", true);
  const isClaudeTab = tab => !!(tab?.linkedBrowser && parseLink(tab.linkedBrowser.currentURI?.spec ?? ""));

  // Runs one link or picker at a time, in the order they came.
  function enqueue(fn) {
    const run = queue.then(fn);
    queue = run.catch(e => console.error("[claude-accounts]", e));
    return run;
  }

  // --- Taking over links from other apps -------------------------------------

  // Called for each link another app opens in this window (the hook on
  // BrowserDOMWindow.prototype.openURI, in ClaudeAccounts.sys.mjs). True when
  // this script takes it; anything else goes on as before.
  function takeExternal(spec) {
    if (destroyed || !initialized || !enabled() || !ClaudeAccounts.accounts.length) {
      return false;
    }
    const link = parseLink(spec);
    if (!link) {
      return false;
    }
    enqueue(() => handleLink(link));
    return true;
  }

  /**
   * Opens a Claude link in the account it names, or in the one picked.
   * opts.ask: always ask; opts.replaceTab: open in place of that tab.
   * Resolves to the new tab, or null when nothing was opened.
   */
  async function handleLink(link, opts = {}) {
    await ClaudeAccounts.refresh();
    if (!ClaudeAccounts.accounts.length) {
      openPlain(link.url);
      return null;
    }
    const tagged = ClaudeAccounts.byAlias(link.tag);
    const hinted = ClaudeAccounts.byEmail(link.loginHint);
    let account = null;
    if (!opts.ask && !Services.prefs.getBoolPref(PREF + "ask-for-tagged", false)) {
      account = tagged ?? hinted;
    }
    account ??= await pick(link, { tagged, hinted });
    if (!account || destroyed) {
      return null;
    }
    ClaudeAccounts.setLastAlias(account.alias);
    return openIn(link, account, opts);
  }

  async function openIn(link, account, { replaceTab = null } = {}) {
    const url = ClaudeAccounts.urlFor(link, account);
    if (account.chrome) {
      await openInChrome(url, account, replaceTab);
      return null;
    }
    return openTab(url, account, replaceTab);
  }

  function openTab(url, account, replaceTab) {
    const params = {
      userContextId: account.userContextId,
      triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
      // Not "fromExternal": for an external tab Zen puts in the Space's own
      // container instead of the one given here.
      fromExternal: false,
    };
    const replacing = replaceTab?.isConnected ? replaceTab : null;
    const tab = replacing ? gBrowser.addAdjacentTab(replacing, url, params) : gBrowser.addTab(url, params);
    gBrowser.selectedTab = tab;
    if (replacing) {
      gBrowser.removeTab(replacing);
    }
    window.focus();
    return tab;
  }

  // An account that lives in a Chrome profile: Chrome opens the link. A tab
  // being reopened as that account moves there, as it would to a container.
  async function openInChrome(url, account, replaceTab) {
    try {
      await ClaudeAccounts.openInChrome(account, url);
    } catch (e) {
      console.error("[claude-accounts]", e);
      const box = gBrowser.getNotificationBox(gBrowser.selectedBrowser);
      const old = box.getNotificationWithValue(CHROME_NOTICE);
      if (old) {
        box.removeNotification(old);
      }
      await box.appendNotification(
        CHROME_NOTICE,
        { label: `Couldn't open ${account.alias}'s link in Chrome: ${e.message}.`, priority: box.PRIORITY_WARNING_MEDIUM },
        [{ label: `Open in Zen as ${account.alias}`, accessKey: "", callback: () => void openTab(url, account, null) }]
      );
      return;
    }
    if (replaceTab?.isConnected && gBrowser.tabs.length > 1) {
      gBrowser.removeTab(replaceTab);
    }
  }

  function openPlain(url) {
    const tab = gBrowser.addTab(url, {
      triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
      fromExternal: true,
    });
    gBrowser.selectedTab = tab;
    window.focus();
  }

  // --- The picker ------------------------------------------------------------

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) {
      return panel;
    }
    panel = document.createXULElement("panel");
    panel.id = PANEL_ID;
    panel.setAttribute("noautofocus", "true");
    panel.setAttribute("consumeoutsideclicks", "true");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", PANEL_ID + "-title");
    const box = document.createElementNS(HTML, "div");
    box.className = "ca-box";
    panel.append(box);
    (document.getElementById("mainPopupSet") ?? document.documentElement).append(panel);
    cleanups.push(() => panel.remove());
    return panel;
  }

  function span(className, text) {
    const el = document.createElementNS(HTML, "span");
    el.className = className;
    if (text) {
      el.textContent = text;
    }
    return el;
  }

  function shortUrl(href) {
    try {
      const u = new URL(href);
      return u.host + (u.pathname === "/" ? "" : u.pathname);
    } catch {
      return href;
    }
  }

  /**
   * Asks which account to use. Resolves to the account, or null when the
   * person cancels. `current` is shown but can't be picked (the tab's own).
   */
  function pick(link, { tagged = null, hinted = null, current = null } = {}) {
    const accounts = ClaudeAccounts.accounts;
    const remembered = ClaudeAccounts.byAlias(ClaudeAccounts.memoryFor(link.uuid));
    const last = ClaudeAccounts.byAlias(ClaudeAccounts.lastAlias());
    const usable = a => a && a !== current;
    const preselected =
      [tagged, hinted, remembered, last].find(usable) ?? accounts.find(usable) ?? null;
    if (!preselected) {
      return Promise.resolve(null);
    }

    const panel = ensurePanel();
    const box = panel.firstElementChild;
    const title = document.createElementNS(HTML, "div");
    title.className = "ca-title";
    title.id = PANEL_ID + "-title";
    title.textContent =
      link.kind === "login"
        ? "Sign in with which Claude account?"
        : link.uuid
          ? "Open this artifact as which Claude account?"
          : "Open Claude as which account?";
    const where = document.createElementNS(HTML, "div");
    where.className = "ca-url";
    where.textContent = shortUrl(link.url) + (link.badTag ? `  (unknown account "${link.badTag}")` : "");

    const rows = accounts.map((account, i) => {
      const row = document.createElementNS(HTML, "button");
      row.className = "ca-row";
      row.dataset.alias = account.alias;
      if (account === current) {
        row.disabled = true;
      }
      const notes = [];
      if (account === current) {
        notes.push("this tab");
      }
      if (account === tagged) {
        notes.push("published it");
      }
      if (account === hinted) {
        notes.push("this login's email");
      }
      if (account === remembered && account !== tagged) {
        notes.push("opened it before");
      }
      if (!notes.length && account === last) {
        notes.push("last used");
      }
      if (account.chrome) {
        notes.push("opens in Chrome");
      }
      const note = span("ca-note", notes.join(" · "));
      if (!account.chrome && ClaudeAccounts.isSignedIn(account) === false) {
        note.textContent = [...notes, "not signed in"].join(" · ");
        note.classList.add("ca-warn");
      }
      const dot = span("ca-dot");
      dot.classList.add("identity-color-" + account.color);
      row.append(
        span("ca-key", i < 9 ? String(i + 1) : ""),
        dot,
        span("ca-alias", account.alias),
        span("ca-email", account.email ?? "no email set"),
        note
      );
      return row;
    });
    const foot = document.createElementNS(HTML, "div");
    foot.className = "ca-foot";
    foot.textContent = `Enter opens · 1–${Math.min(accounts.length, 9)} picks · Esc cancels`;
    box.replaceChildren(title, where, ...rows, foot);

    return new Promise(resolve => {
      let chosen = null;
      let settled = false;
      let watchdog = null;
      const choose = account => {
        if (account && account !== current) {
          chosen = account;
          panel.hidePopup();
        }
      };
      const focusRow = step => {
        const live = rows.filter(r => !r.disabled);
        const at = live.indexOf(document.activeElement);
        live[(at + step + live.length) % live.length]?.focus();
      };
      const onKey = e => {
        if (e.altKey || e.ctrlKey || e.metaKey) {
          return;
        }
        if (/^[1-9]$/.test(e.key)) {
          e.preventDefault();
          choose(accounts[Number(e.key) - 1]);
        } else if (e.key === "Enter") {
          e.preventDefault();
          const row = rows.includes(document.activeElement) ? document.activeElement : rows[accounts.indexOf(preselected)];
          choose(ClaudeAccounts.byAlias(row?.dataset.alias));
        } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          focusRow(e.key === "ArrowDown" ? 1 : -1);
        }
      };
      const onClick = e => {
        const row = e.target.closest?.(".ca-row");
        if (row && !row.disabled) {
          choose(ClaudeAccounts.byAlias(row.dataset.alias));
        }
      };
      const onShown = () => rows[accounts.indexOf(preselected)]?.focus();
      const finish = () => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(watchdog);
        panel.removeEventListener("keydown", onKey, true);
        panel.removeEventListener("click", onClick);
        panel.removeEventListener("popupshown", onShown);
        panel.removeEventListener("popuphidden", finish);
        openPanel = null;
        resolve(destroyed ? null : chosen);
      };
      panel.addEventListener("keydown", onKey, true);
      panel.addEventListener("click", onClick);
      panel.addEventListener("popupshown", onShown);
      panel.addEventListener("popuphidden", finish);
      openPanel = {
        hide: () => {
          panel.hidePopup();
          finish();
        },
      };

      if (window.windowState === window.STATE_MINIMIZED) {
        window.restore();
      }
      window.focus();
      const anchor = gBrowser.tabpanels ?? document.documentElement;
      const x = Math.max(0, Math.round((anchor.clientWidth - PANEL_WIDTH) / 2));
      panel.openPopup(anchor, { position: "overlap", x, y: 64 });
      // A popup that can't open (no visible window) would hold every later
      // link in the queue: give up on it.
      watchdog = setTimeout(() => {
        if (panel.state === "closed") {
          console.warn("[claude-accounts] the picker did not open; leaving the link alone.");
          finish();
        }
      }, 3000);
    });
  }

  /** Lets the person open the tab's Claude page in another account. */
  function pickForTab(tab) {
    return enqueue(async () => {
      const link = parseLink(tab?.linkedBrowser?.currentURI?.spec ?? "");
      if (!link) {
        return null;
      }
      await ClaudeAccounts.refresh();
      const current = ClaudeAccounts.byUserContextId(tab.userContextId);
      const account = await pick(link, { current });
      if (!account || destroyed) {
        return null;
      }
      ClaudeAccounts.setLastAlias(account.alias);
      return openIn(link, account, { replaceTab: tab });
    });
  }

  /** The toolbar button: this Claude page in another account, else Claude in one. */
  function switcher() {
    const tab = gBrowser.selectedTab;
    if (isClaudeTab(tab)) {
      return pickForTab(tab);
    }
    return enqueue(() => handleLink(parseLink("https://claude.ai/new"), { ask: true }));
  }

  // --- An artifact that didn't open ------------------------------------------

  function onArtifactResponse(browser, uuid, status) {
    if (destroyed) {
      return;
    }
    // Null for another window's tab (each window hears every tab's requests).
    const tab = gBrowser.getTabForBrowser(browser);
    // Only the artifact the tab shows: a page that loads other frames on the
    // side says nothing about this one.
    if (!tab || parseLink(browser.currentURI?.spec ?? "")?.uuid !== uuid) {
      return;
    }
    const account = ClaudeAccounts.byUserContextId(tab.userContextId);
    if (status >= 200 && status < 300) {
      if (account) {
        ClaudeAccounts.remember(uuid, account.alias, true);
      }
      const box = gBrowser.getNotificationBox(browser);
      const old = box.getNotificationWithValue(NOTICE);
      if (old) {
        box.removeNotification(old);
      }
    } else if (status === 401 || status === 403 || status === 404) {
      if (account) {
        ClaudeAccounts.remember(uuid, account.alias, false);
      }
      showNotice(tab, account).catch(e => console.error("[claude-accounts]", e));
    }
  }

  async function showNotice(tab, account) {
    const box = gBrowser.getNotificationBox(tab.linkedBrowser);
    const old = box.getNotificationWithValue(NOTICE);
    if (old) {
      box.removeNotification(old);
    }
    const others = ClaudeAccounts.accounts.filter(a => a !== account && Number.isInteger(a.userContextId));
    const label = account
      ? `Claude account ${account.alias} can't open this artifact: no access, or it doesn't exist.`
      : "The Claude login in this tab can't open this artifact: no access, or it doesn't exist.";
    const buttons = others.slice(0, 5).map(other => ({
      label: `Open as ${other.alias}`,
      accessKey: "",
      callback: () => {
        const link = parseLink(tab.linkedBrowser.currentURI.spec);
        if (link) {
          enqueue(() => openIn(link, other, { replaceTab: tab }));
        }
        return false;
      },
    }));
    if (others.length > 5) {
      buttons.push({ label: "Other…", accessKey: "", callback: () => void pickForTab(tab) });
    }
    await box.appendNotification(NOTICE, { label, priority: box.PRIORITY_WARNING_LOW }, buttons);
  }

  // --- Tab menu and toolbar button --------------------------------------------

  function addMenuItem() {
    const menu = document.getElementById("tabContextMenu");
    if (!menu) {
      return;
    }
    const item = document.createXULElement("menuitem");
    item.id = MENU_ID;
    item.setAttribute("label", "Open in Another Claude Account…");
    item.hidden = true;
    // The tab the menu opened on, found the way the menu finds it
    // (TabContextMenu.updateContextMenu), since its own listener may run
    // after this one.
    let menuTab = null;
    const onCommand = () => {
      if (menuTab?.isConnected) {
        pickForTab(menuTab);
      }
    };
    const onShowing = e => {
      // The menu itself, not one of its submenus.
      if (e.target !== e.currentTarget) {
        return;
      }
      const trigger = menu.triggerNode;
      menuTab = trigger?.tab || trigger?.closest?.("tab") || gBrowser.selectedTab;
      item.hidden = !isClaudeTab(menuTab);
    };
    item.addEventListener("command", onCommand);
    menu.addEventListener("popupshowing", onShowing);
    const neighbor = document.getElementById("context_reopenInContainer");
    if (neighbor) {
      neighbor.after(item);
    } else {
      menu.append(item);
    }
    cleanups.push(() => {
      menu.removeEventListener("popupshowing", onShowing);
      item.remove();
    });
  }

  // The widget is shared by all windows and stays across reloads; its command
  // finds this window's current instance.
  function ensureWidget() {
    try {
      if (CustomizableUI.getWidget(WIDGET_ID)?.provider === CustomizableUI.PROVIDER_API) {
        return;
      }
      CustomizableUI.createWidget({
        id: WIDGET_ID,
        type: "button",
        defaultArea: CustomizableUI.AREA_NAVBAR,
        label: "Claude Accounts",
        tooltiptext: "Open this Claude page in another account, or open Claude in one",
        onCommand(event) {
          event.target.ownerGlobal[Symbol.for("zen-claude-accounts")]?.switcher();
        },
      });
    } catch (e) {
      console.warn("[claude-accounts] no toolbar button:", e);
    }
  }

  // --- A link that started Zen -------------------------------------------------

  // When Zen wasn't running, the link is the new window's first argument and
  // has a tab of its own before this script can step in. Take it over: open it
  // as usual for the account, then close that tab.
  async function takeStartupLink() {
    const [arg, options] = window.arguments ?? [];
    if (typeof arg !== "string" || !enabled()) {
      return;
    }
    let fromExternal = false;
    try {
      fromExternal = options.QueryInterface(Ci.nsIPropertyBag2).getPropertyAsBool("fromExternal");
    } catch {}
    const link = fromExternal ? parseLink(arg) : null;
    if (!link || !ClaudeAccounts.accounts.length) {
      return;
    }
    const strip = s => String(s ?? "").replace(/#.*$/, "");
    let tab = null;
    for (let i = 0; i < 50 && !tab && !destroyed; i++) {
      tab = gBrowser.tabs.find(t => strip(t.linkedBrowser?.currentURI?.spec) === strip(arg)) ?? null;
      if (!tab) {
        await new Promise(r => setTimeout(r, 100));
      }
    }
    enqueue(() => handleLink(link, { replaceTab: tab }));
  }

  // --- Start -----------------------------------------------------------------

  // Each part on its own: one that fails leaves the others working.
  function step(name, fn) {
    try {
      fn();
    } catch (e) {
      console.error(`[claude-accounts] ${name} failed`, e);
    }
  }

  async function init() {
    if (destroyed) {
      return;
    }
    step("style", () => {
      windowUtils.loadSheetUsingURIString(SHEET, windowUtils.AUTHOR_SHEET);
      cleanups.push(() => windowUtils.removeSheetUsingURIString(SHEET, windowUtils.AUTHOR_SHEET));
    });
    await ClaudeAccounts.init({ ContextualIdentityService });
    if (ClaudeAccounts.problem) {
      console.warn(`[claude-accounts] ${ClaudeAccounts.problem}; Claude links open as usual.`);
    }
    if (destroyed) {
      return;
    }
    step("artifact watch", () => cleanups.push(ClaudeAccounts.addListener(onArtifactResponse)));
    step("link hook", () => ClaudeAccounts.hookOpenURI());
    step("tab menu", addMenuItem);
    step("toolbar button", ensureWidget);
    initialized = true;
    takeStartupLink().catch(e => console.error("[claude-accounts]", e));
  }

  // browserDOMWindow and the tab strip exist once the window's delayed
  // startup has run.
  (window.delayedStartupPromise ?? Promise.resolve()).then(init).catch(e => console.error("[claude-accounts]", e));
})();
