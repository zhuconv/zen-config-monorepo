// ==UserScript==
// @name           Zen Tab Autofill
// @description    Tab accepts the URL bar's inline completion, and the URL bar icon shows the target site's favicon while you type.
// @include        main
// @ignorecache
// @version        0.1.0
// ==/UserScript==

// Runs as a Sine mod or under a plain fx-autoconfig loader. Everything lives
// inside one function so the script can be loaded again without errors.
(() => {
  "use strict";

  if (window.location.href !== "chrome://browser/content/browser.xhtml") {
    return;
  }

  const INSTANCE = Symbol.for("arc-like-cmdT");
  const PREF_BRANCH = "uc.tab-autofill.";
  const PREF_DEFAULTS = {
    "tab-accepts": true,
    favicon: true,
    "favicon-after-accept": false,
  };
  // UrlbarShared.RESULT_TYPE values whose result opens a page:
  // TAB_SWITCH = 1, URL = 3, KEYWORD = 4, REMOTE_TAB = 6.
  const PAGE_RESULT_TYPES = new Set([1, 3, 4, 6]);
  const FAVICON_ATTR = "tab-autofill-favicon";

  // A second load (an update, or a loader running the file twice) replaces the
  // first instance instead of stacking on top of it.
  window[INSTANCE]?.destroy();

  const cleanups = [];
  const prefs = {};
  let destroyed = false;

  function destroy() {
    if (destroyed) {
      return;
    }
    destroyed = true;
    for (const cleanup of cleanups.reverse()) {
      try {
        cleanup();
      } catch (e) {
        console.error("[tab-autofill] cleanup failed", e);
      }
    }
    if (window[INSTANCE]?.destroy === destroy) {
      delete window[INSTANCE];
    }
  }

  window[INSTANCE] = { destroy };
  // Sine calls this when the mod is disabled, updated or removed.
  window.addUnloadListener?.(destroy);
  // The pref observer is held by a global service, so drop it when the window
  // closes even when no mod manager is around to do it.
  window.addEventListener("unload", destroy, { once: true });
  cleanups.push(() => window.removeEventListener("unload", destroy));

  function readPrefs() {
    for (const [name, fallback] of Object.entries(PREF_DEFAULTS)) {
      prefs[name] = Services.prefs.getBoolPref(PREF_BRANCH + name, fallback);
    }
  }

  // True while the URL bar shows an inline completion the user has not
  // touched yet: the completed part is still selected after the typed text.
  function hasPendingAutofill(urlbar) {
    const placeholder = urlbar._autofillPlaceholder;
    const input = urlbar.inputField;
    return (
      !!placeholder &&
      placeholder.selectionStart < placeholder.selectionEnd &&
      placeholder.value === urlbar.value &&
      input.selectionStart === placeholder.selectionStart &&
      input.selectionEnd === placeholder.selectionEnd
    );
  }

  function faviconFor(result) {
    if (!result || !PAGE_RESULT_TYPES.has(result.type)) {
      return null;
    }
    const url = result.payload?.url;
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) {
      return null;
    }
    // page-icon: serves the icon from the history database, so nothing is
    // fetched from the network. Unknown pages get the default globe icon.
    const icon = result.payload.icon;
    return typeof icon === "string" && icon.startsWith("page-icon:")
      ? icon
      : "page-icon:" + url;
  }

  function init() {
    if (destroyed) {
      return;
    }
    const urlbar = window.gURLBar;
    const input = urlbar?.inputField;
    const identityBox = document.getElementById("identity-box");
    const identityIcon = document.getElementById("identity-icon");
    if (!input) {
      console.warn("[tab-autofill] URL bar not found; doing nothing.");
      return;
    }
    readPrefs();

    // --- Tab accepts the inline completion --------------------------------

    function onKeyDown(event) {
      if (
        !prefs["tab-accepts"] ||
        event.key !== "Tab" ||
        event.shiftKey ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey ||
        event.isComposing ||
        event.target !== input ||
        urlbar.view?.isResultMenuOpen?.() ||
        !hasPendingAutofill(urlbar)
      ) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      // Same as pressing Right or End: collapsing the selection makes the URL
      // bar's own selectionchange handler confirm the completion as typed text.
      const end = urlbar.value.length;
      if (typeof urlbar.setSelectionRange === "function") {
        urlbar.setSelectionRange(end, end);
      } else {
        input.setSelectionRange(end, end);
      }
    }
    // Capture phase on the URL bar runs before the URL bar's own key handling
    // on the input field, which would otherwise move to the next result.
    urlbar.addEventListener("keydown", onKeyDown, true);
    cleanups.push(() => urlbar.removeEventListener("keydown", onKeyDown, true));

    // --- Favicon instead of the search glass ------------------------------

    if (!identityBox || !identityIcon) {
      console.warn("[tab-autofill] identity icon not found; favicon disabled.");
      return;
    }

    let shownIcon = null;
    // False from a keystroke until the URL bar picks the result for the new
    // text. Keeping the current icon meanwhile avoids flicker while typing.
    let resultKnown = true;

    function showIcon(icon) {
      if (icon === shownIcon) {
        return;
      }
      shownIcon = icon;
      if (icon) {
        identityIcon.style.setProperty(
          "list-style-image",
          `url(${JSON.stringify(icon)})`,
          "important"
        );
        identityBox.setAttribute(FAVICON_ATTR, "true");
      } else {
        identityIcon.style.removeProperty("list-style-image");
        identityBox.removeAttribute(FAVICON_ATTR);
      }
    }
    cleanups.push(() => showIcon(null));

    function wantedIcon() {
      if (
        !prefs.favicon ||
        urlbar.getAttribute("pageproxystate") === "valid" ||
        urlbar.searchMode ||
        !urlbar.view?.isOpen ||
        !urlbar.value
      ) {
        return null;
      }
      if (!resultKnown) {
        return shownIcon;
      }
      if (prefs["favicon-after-accept"] && hasPendingAutofill(urlbar)) {
        return null;
      }
      return faviconFor(urlbar._resultForCurrentValue);
    }

    let updateQueued = false;
    function scheduleUpdate() {
      if (updateQueued || destroyed) {
        return;
      }
      updateQueued = true;
      // Run after the URL bar finishes its current change (value, selection
      // and result can update in several steps).
      Promise.resolve().then(() => {
        updateQueued = false;
        if (!destroyed) {
          showIcon(wantedIcon());
        }
      });
    }

    // The view reports every change of the selected result, including the
    // first result of each query, through setResultForCurrentValue.
    const hadOwnSetter = Object.hasOwn(urlbar, "setResultForCurrentValue");
    const originalSetter = urlbar.setResultForCurrentValue;
    if (typeof originalSetter === "function") {
      const patchedSetter = function (...args) {
        const ret = originalSetter.apply(this, args);
        resultKnown = true;
        scheduleUpdate();
        return ret;
      };
      urlbar.setResultForCurrentValue = patchedSetter;
      cleanups.push(() => {
        // If another script wrapped ours since, leave the chain alone; our
        // wrapper only schedules no-op updates once destroyed.
        if (urlbar.setResultForCurrentValue !== patchedSetter) {
          return;
        }
        if (hadOwnSetter) {
          urlbar.setResultForCurrentValue = originalSetter;
        } else {
          delete urlbar.setResultForCurrentValue;
        }
      });
    } else {
      console.warn("[tab-autofill] result hook not found; favicon disabled.");
      return;
    }

    function onInput() {
      resultKnown = false;
      scheduleUpdate();
    }
    // Capture phase, so this runs before the URL bar handles the keystroke
    // and before any result it might pick synchronously.
    urlbar.addEventListener("input", onInput, true);
    input.addEventListener("selectionchange", scheduleUpdate);
    cleanups.push(() => {
      urlbar.removeEventListener("input", onInput, true);
      input.removeEventListener("selectionchange", scheduleUpdate);
    });

    // View opening/closing, page proxy state (typing vs. showing the page's
    // URL) and search mode all change what the icon should be.
    const observer = new MutationObserver(scheduleUpdate);
    observer.observe(urlbar, {
      attributes: true,
      attributeFilter: ["open", "pageproxystate", "searchmode"],
    });
    cleanups.push(() => observer.disconnect());

    const prefObserver = {
      observe() {
        readPrefs();
        scheduleUpdate();
      },
    };
    Services.prefs.addObserver(PREF_BRANCH, prefObserver);
    cleanups.push(() => Services.prefs.removeObserver(PREF_BRANCH, prefObserver));
  }

  if (window.delayedStartupPromise) {
    window.delayedStartupPromise.then(init);
  } else {
    init();
  }
})();
