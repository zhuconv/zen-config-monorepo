// ==UserScript==
// @name           PR Live Folder
// @description    Makes Zen's GitHub pull-request live folder work like Arc's: only PRs with recent activity, refreshed when you come back to Zen, and a PR you open lands in the folder.
// @include        main
// @ignorecache
// @version        0.1.0
// ==/UserScript==

/* Window side of PR Live Folder: tells pr-folder/PrFolder.sys.mjs, which all
 * windows share and which does the work, when this window comes to the front,
 * switches space, changes tab, or a tab's page changes address.
 * Prefs (about:config): uc.pr-folder.recent-days (14),
 * uc.pr-folder.poll-seconds (120), uc.pr-folder.min-gap-seconds (10),
 * uc.pr-folder.adopt-opened-prs (true).
 */
(() => {
  "use strict";

  if (window.location.href !== "chrome://browser/content/browser.xhtml") {
    return;
  }

  const INSTANCE = Symbol.for("arc-like-pr-folder");
  // A second load (an update, or a loader running the file twice) replaces the
  // first instance instead of stacking on top of it.
  window[INSTANCE]?.destroy();

  const SCRIPT_URL = Components.stack.filename.split(" -> ").pop();
  const BASE = SCRIPT_URL.slice(0, SCRIPT_URL.lastIndexOf("/") + 1) + "pr-folder/";
  const { PrFolder } = ChromeUtils.importESModule(BASE + "PrFolder.sys.mjs");

  let listening = false;
  const onActivate = () => PrFolder.onWindowActivated(window);
  const onTabSelect = event => PrFolder.onTabSelect(window, event);
  const onSpaceChange = ({ workspace } = {}) => PrFolder.onSpaceChanged(window, workspace);
  const progressListener = {
    onLocationChange(browser, webProgress, request, location) {
      if (!webProgress?.isTopLevel || !location) {
        return;
      }
      const tab = gBrowser.getTabForBrowser(browser);
      if (tab) {
        PrFolder.onTabLocation(window, tab, location.spec);
      }
    },
  };

  const instance = { initialized: false, PrFolder, onSpaceChange, destroy };
  window[INSTANCE] = instance;

  function destroy() {
    if (window[INSTANCE] === instance) {
      delete window[INSTANCE];
    }
    if (listening) {
      listening = false;
      window.removeEventListener("activate", onActivate);
      gBrowser.tabContainer.removeEventListener("TabSelect", onTabSelect);
      gBrowser.removeTabsProgressListener(progressListener);
      gZenWorkspaces.removeChangeListeners(onSpaceChange);
    }
    window.removeEventListener("unload", destroy);
    PrFolder.detach(window);
  }

  window.addEventListener("unload", destroy, { once: true });
  // Sine's way to turn a mod off without a restart.
  window.addUnloadListener?.(destroy);

  (async () => {
    await window.delayedStartupPromise;
    if (window[INSTANCE] !== instance) {
      return;
    }
    window.addEventListener("activate", onActivate);
    gBrowser.tabContainer.addEventListener("TabSelect", onTabSelect);
    gBrowser.addTabsProgressListener(progressListener);
    gZenWorkspaces.addChangeListeners(onSpaceChange);
    listening = true;
    await PrFolder.attach(window);
    instance.initialized = window[INSTANCE] === instance;
  })().catch(e => console.error("[pr-folder] start failed:", e));
})();
