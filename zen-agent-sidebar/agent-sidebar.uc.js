// ==UserScript==
// @name           Agent Sidebar
// @description    Select text, press Cmd+Shift+A, and ask your local Codex or Claude Code about it in a sidebar that sees the page.
// @include        main
// @author         zhuconv
// ==/UserScript==

/* Window side of the agent sidebar:
 *  - registers the sidebar panel (agent-sidebar/panel.html) and the shortcut;
 *  - gives each tab its own conversation in AgentService;
 *  - reads the selection and the page text from the tab when asked.
 * Prefs: uc.agent-sidebar.shortcut ("accel,shift+A"; Zen's own key for it is
 *        switched off while the script runs),
 *        uc.agent-sidebar.max-page-chars (120000),
 *        uc.agent-sidebar.default-question;
 *        AgentService.sys.mjs reads the others.
 */
(function agentSidebar() {
  "use strict";
  if (window.ucAgentSidebar) {
    return;
  }

  const SCRIPT_URL = Components.stack.filename.split(" -> ").pop();
  const BASE = SCRIPT_URL.slice(0, SCRIPT_URL.lastIndexOf("/") + 1) + "agent-sidebar/";
  const { AgentService } = ChromeUtils.importESModule(BASE + "AgentService.sys.mjs");
  AgentService.init(BASE);

  const PANEL = "viewAgentSidebar";
  const ACTOR = "AgentSidebar";
  const PREF_SHORTCUT = "uc.agent-sidebar.shortcut";
  const PREF_MAX_CHARS = "uc.agent-sidebar.max-page-chars";
  const PREF_PLACED = "uc.agent-sidebar.placed-right";
  const PREF_WIDENED = "uc.agent-sidebar.widened";
  // The panel draws its own header, so hide the sidebar's while it is open.
  const STYLE = `data:text/css,${encodeURIComponent(
    "#sidebar-box[agent-sidebar] #sidebar-header { display: none !important; }"
  )}`;

  const conversations = new WeakMap(); // tab -> Conversation
  const cleanups = [];
  let panel = null; // the open panel's API, set by panel.js
  let panelWaiters = [];

  function conversationFor(tab) {
    let conv = conversations.get(tab);
    if (!conv) {
      conv = AgentService.createConversation();
      conversations.set(tab, conv);
    }
    return conv;
  }

  function tabOf(conv) {
    return gBrowser.tabs.find(tab => conversations.get(tab) === conv) ?? null;
  }

  // --- The sidebar panel ----------------------------------------------------

  function registerPanel() {
    const sidebars = SidebarController.sidebars;
    if (!sidebars.has(PANEL)) {
      sidebars.set(
        PANEL,
        SidebarController.makeSidebar(
          {
            elementId: "agent-sidebar-title",
            url: BASE + "panel.html",
            title: "Codex",
            iconUrl: "chrome://global/skin/icons/highlights.svg",
            hasCustomHeader: true,
          },
          PANEL
        )
      );
    }
    // Reading panels belong on the right, away from Zen's tab sidebar. Only
    // once, so a later change back to the left sticks.
    if (!Services.prefs.getBoolPref(PREF_PLACED, false)) {
      Services.prefs.setBoolPref(PREF_PLACED, true);
      Services.prefs.setBoolPref("sidebar.position_start", false);
    }
  }

  function panelIsOpen() {
    return SidebarController.isOpen && SidebarController.currentID === PANEL;
  }

  async function openPanel() {
    if (panel && panelIsOpen()) {
      return panel;
    }
    const ready = new Promise(resolve => panelWaiters.push(resolve));
    if (!panelIsOpen()) {
      await SidebarController.show(PANEL);
    }
    return panel ?? ready;
  }

  // --- Reading the tab --------------------------------------------------------

  function actor(browsingContext) {
    return browsingContext?.currentWindowGlobal?.getActor(ACTOR);
  }

  function withTimeout(promise, ms) {
    return Promise.race([promise, new Promise(resolve => setTimeout(() => resolve(null), ms))]);
  }

  /** The text the user selected in the tab: in the frame they last used (the
   * focused one), else in the page itself. Other frames are not asked: one
   * that is busy (an ad, a tracker, a bot check) held the shortcut up for as
   * long as a second, and a selection is made where the focus is. */
  async function selectionIn(tab) {
    const top = tab.linkedBrowser.browsingContext;
    if (!top) {
      return "";
    }
    const focused = Services.focus.focusedContentBrowsingContext;
    const contexts = focused && focused !== top && focused.top === top ? [focused, top] : [top];
    for (const context of contexts) {
      try {
        const result = await withTimeout(actor(context).sendQuery("GetSelection"), 1000);
        if (result?.selection) {
          return result.selection;
        }
      } catch {}
    }
    return "";
  }

  function pageOf(tab) {
    const browser = tab.linkedBrowser;
    const url = browser.currentURI?.spec ?? "";
    if (!/^(https?|file):/.test(url)) {
      return null;
    }
    return {
      url,
      title: tab.label,
      kind: browser.documentContentType === "application/pdf" ? "pdf" : "html",
      getText: () =>
        actor(browser.browsingContext).sendQuery("GetPageText", {
          maxChars: Services.prefs.getIntPref(PREF_MAX_CHARS, 120000),
        }),
    };
  }

  // --- Actions ----------------------------------------------------------------

  function panelHasFocus() {
    return !!SidebarController.browser?.contentDocument?.hasFocus();
  }

  /**
   * The shortcut opens and closes the panel. With the panel open, newly
   * selected text on the page goes into the composer instead of closing it,
   * so "select, press, ask" works the same whether the panel is open or not.
   */
  async function toggle() {
    const tab = gBrowser.selectedTab;
    const conv = conversationFor(tab);
    if (panel && panelIsOpen()) {
      const selection = panelHasFocus() ? "" : await selectionIn(tab);
      if (selection && selection !== conv.lastSelection) {
        conv.lastSelection = selection;
        getReady(conv, tab);
        panel.compose(conv, { selection });
      } else {
        SidebarController.hide();
        gBrowser.selectedBrowser.focus();
      }
      return;
    }
    // Ask the page for its selection while the panel opens.
    const selecting = selectionIn(tab);
    const api = await openPanel();
    const selection = await selecting;
    getReady(conv, tab);
    conv.lastSelection = selection;
    api.compose(conv, { selection });
  }

  /** While the user types: start the agent and its session, and read the page. */
  function getReady(conv, tab) {
    AgentService.prewarm(conv);
    AgentService.prefetchPage(conv, pageOf(tab));
  }

  /** Called by the panel when the user sends a question, or an edited one
   * (`replace`: the question it replaces). */
  function submit(conv, { question, selection, attachments, replace }) {
    const tab = tabOf(conv) ?? gBrowser.selectedTab;
    return AgentService.send(conv, { question, selection, attachments, replace, page: pageOf(tab) });
  }

  function normalizeModifiers(modifiers) {
    const isMac = AppConstants.platform === "macosx";
    return (modifiers || "")
      .split(/[,\s]+/)
      .filter(Boolean)
      .map(m => (m === "accel" ? (isMac ? "meta" : "control") : m))
      .sort()
      .join(",");
  }

  function shortcutSpec() {
    const spec = Services.prefs.getStringPref(PREF_SHORTCUT, "accel,shift+A");
    const plus = spec.lastIndexOf("+");
    return { modifiers: plus > 0 ? spec.slice(0, plus) : "", key: spec.slice(plus + 1) };
  }

  /** Switch off whatever else has the same keys (Cmd+Shift+A is Zen's
   * "search tabs"); uninit gives it back. Zen adds some keys late in
   * startup, so this runs again then. */
  const disabledKeys = new Set();
  function disableConflictingKeys() {
    const { modifiers, key } = shortcutSpec();
    const wanted = `${normalizeModifiers(modifiers)}+${key.toUpperCase()}`;
    for (const other of document.querySelectorAll("key")) {
      const otherKey = (other.getAttribute("key") || "").toUpperCase();
      if (
        other.id !== "key_agentSidebarToggle" &&
        otherKey &&
        `${normalizeModifiers(other.getAttribute("modifiers"))}+${otherKey}` === wanted &&
        other.getAttribute("disabled") !== "true"
      ) {
        other.setAttribute("disabled", "true");
        disabledKeys.add(other);
      }
    }
  }
  cleanups.push(() => {
    for (const key of disabledKeys) {
      key.removeAttribute("disabled");
    }
    disabledKeys.clear();
  });

  function installShortcut() {
    const { modifiers, key } = shortcutSpec();
    disableConflictingKeys();

    const commandset = document.createXULElement("commandset");
    commandset.id = "agent-sidebar-commands";
    const command = document.createXULElement("command");
    command.id = "cmd_agentSidebarToggle";
    command.addEventListener("command", () => toggle().catch(e => console.error("[agent-sidebar]", e)));
    commandset.append(command);

    const keyset = document.createXULElement("keyset");
    keyset.id = "agent-sidebar-keys";
    const keyElement = document.createXULElement("key");
    keyElement.id = "key_agentSidebarToggle";
    keyElement.setAttribute("modifiers", modifiers);
    keyElement.setAttribute(key.length === 1 ? "key" : "keycode", key);
    keyElement.setAttribute("command", command.id);
    keyset.append(keyElement);

    document.documentElement.append(commandset, keyset);
    cleanups.push(() => {
      commandset.remove();
      keyset.remove();
    });
  }

  function listen(target, type, handler) {
    target.addEventListener(type, handler);
    cleanups.push(() => target.removeEventListener(type, handler));
  }

  function onTabSelect() {
    panel?.show(conversationFor(gBrowser.selectedTab));
  }

  function onTabClose(event) {
    const conv = conversations.get(event.target);
    if (conv) {
      conversations.delete(event.target);
      AgentService.forget(conv);
    }
  }

  function uninit() {
    if (panelIsOpen()) {
      SidebarController.hide();
    }
    SidebarController.sidebars.delete(PANEL);
    for (const cleanup of cleanups.splice(0)) {
      cleanup();
    }
    delete window.ucAgentSidebar;
  }

  window.ucAgentSidebar = {
    AgentService,
    toggle,
    submit,
    uninit,
    attachPanel(api) {
      panel = api;
      const box = document.getElementById("sidebar-box");
      box?.setAttribute("agent-sidebar", "");
      // The default sidebar width is too narrow for answers. Widen it once;
      // after that the width the user drags to is kept.
      if (box && !Services.prefs.getBoolPref(PREF_WIDENED, false)) {
        Services.prefs.setBoolPref(PREF_WIDENED, true);
        if (box.getBoundingClientRect().width < 400) {
          box.style.width = "420px";
        }
      }
      for (const resolve of panelWaiters.splice(0)) {
        resolve(api);
      }
      api.show(conversationFor(gBrowser.selectedTab));
    },
    detachPanel(api) {
      if (panel === api) {
        panel = null;
        document.getElementById("sidebar-box")?.removeAttribute("agent-sidebar");
      }
    },
    closePanel() {
      SidebarController.hide();
    },
    focusPage() {
      gBrowser.selectedBrowser.focus();
    },
    openLink(url) {
      if (/^https?:/.test(url)) {
        openWebLinkIn(url, "tab");
      }
    },
    defaultQuestion() {
      return Services.prefs.getStringPref("uc.agent-sidebar.default-question", "解释一下这段内容。");
    },
    get panel() {
      return panel;
    },
    conversationFor,
  };

  registerPanel();
  installShortcut();
  window.delayedStartupPromise?.then(() => setTimeout(disableConflictingKeys, 1000));
  windowUtils.loadSheetUsingURIString(STYLE, windowUtils.AUTHOR_SHEET);
  cleanups.push(() => windowUtils.removeSheetUsingURIString(STYLE, windowUtils.AUTHOR_SHEET));
  listen(gBrowser.tabContainer, "TabSelect", onTabSelect);
  listen(gBrowser.tabContainer, "TabClose", onTabClose);
})();
