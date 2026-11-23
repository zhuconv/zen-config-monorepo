#!/usr/bin/env python3
"""End-to-end test for claude-accounts.uc.js against the Zen you have installed.

Starts Zen headless with a throwaway profile (your own profile is never
touched) and a made-up account table, drives it over Marionette, loads the
script and checks what it does with Claude links handed over the way other apps
hand them over: through Zen's command-line handler. Every web request goes to a
dead proxy, so nothing reaches claude.ai; tabs show an error page at the right
address, which is all the checks need.

    python3 test/run_test.py            # headless
    python3 test/run_test.py --headful  # watch it run in a visible window
    python3 test/run_test.py --via-loader PROFILE/chrome
                                        # let the installed fx-autoconfig loader
                                        # start the script, as in daily use

Run it again after each Zen update to see whether the mod still works.
Standard library only.
"""

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
SCRIPT = MOD / "claude-accounts.uc.js"
DEFAULT_ZEN = "/Applications/Zen.app/Contents/MacOS/zen"

ARTIFACT = "https://claude.ai/artifact/8zf8puxuk6P54wRxzJPFh3"
ARTIFACT_UUID = "40bcddf3-8633-4d1c-aa87-4dc0daebf6ea"
UT_ORG = "11111111-2222-4333-8444-555555555555"
ACCOUNTS = {
    "accounts": [
        {"alias": "ut", "email": "ut@example.com", "org": UT_ORG},
        {"alias": "gt", "email": "gt@example.com"},
        {"alias": "sr"},
    ]
}


# --- Marionette -------------------------------------------------------------


class Marionette:
    """Minimal Marionette client: length-prefixed JSON over TCP."""

    def __init__(self, port, timeout=60):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=120)
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("Zen did not open the Marionette port")
                time.sleep(0.5)
        self.msg_id = 0
        self._recv()  # server hello

    def _recv(self):
        header = b""
        while not header.endswith(b":"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise RuntimeError("Marionette connection closed")
            header += chunk
        length = int(header[:-1])
        data = b""
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError("Marionette connection closed")
            data += chunk
        return json.loads(data)

    def send(self, command, params=None):
        self.msg_id += 1
        payload = json.dumps([0, self.msg_id, command, params or {}])
        self.sock.sendall(f"{len(payload)}:{payload}".encode())
        while True:
            msg = self._recv()
            if msg[0] == 1 and msg[1] == self.msg_id:
                if msg[2]:
                    error = msg[2]
                    raise RuntimeError(
                        f"{command}: {error.get('error')}: {error.get('message')}\n"
                        f"{error.get('stacktrace', '')}"
                    )
                return msg[3]

    def run(self, body, *args):
        """Run an async chrome-context script; `done(value)` returns value."""
        script = COMMON_JS + "\n(async () => {\n" + body + "\n})().then(done, e => done({__error: String(e) + '\\n' + (e && e.stack)}));"
        result = self.send(
            "WebDriver:ExecuteAsyncScript",
            {"script": "const done = arguments[arguments.length - 1];\n" + script, "args": list(args)},
        )["value"]
        if isinstance(result, dict) and "__error" in result:
            raise RuntimeError("script failed: " + result["__error"])
        return result


# Helpers available to every test script. They run in the browser window.
COMMON_JS = r"""
const CA = () => window[Symbol.for("multi-cc-switcher")];
const MODULE = "chrome://claudeaccountstest/content/claude-accounts/ClaudeAccounts.sys.mjs";
const mod = ChromeUtils.importESModule(MODULE);
const { parseLink, artifactUuid } = mod;
// The running script's own copy: loaded by the loader, the module has another
// address and so another instance.
const ClaudeAccounts = CA()?.ClaudeAccounts ?? mod.ClaudeAccounts;
// A notification's buttons and text, wherever this build draws them.
const barButtons = bar => [...bar.querySelectorAll("button, moz-button"), ...(bar.shadowRoot?.querySelectorAll("button, moz-button") ?? [])];
const buttonLabel = b => b.label || b.getAttribute("label") || b.textContent.trim();
const barText = bar => [bar.label, bar.message, bar.messageText?.textContent, bar.textContent, bar.shadowRoot?.textContent].filter(v => typeof v === "string").join(" ");
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(fn, what, timeout = 8000) {
  const start = Date.now();
  for (;;) {
    let v;
    try { v = fn(); } catch (e) {}
    if (v) return v;
    if (Date.now() - start > timeout) throw new Error("timed out waiting for " + what + "; state: " + JSON.stringify(snap()));
    await sleep(25);
  }
}
const KEYS = {
  Enter: ["Enter", KeyEvent.DOM_VK_RETURN],
  Escape: ["Escape", KeyEvent.DOM_VK_ESCAPE],
  ArrowDown: ["ArrowDown", KeyEvent.DOM_VK_DOWN],
  ArrowUp: ["ArrowUp", KeyEvent.DOM_VK_UP],
};
function press(key) {
  const tip = Cc["@mozilla.org/text-input-processor;1"].createInstance(Ci.nsITextInputProcessor);
  if (!tip.beginInputTransactionForTests(window)) throw new Error("TextInputProcessor unavailable");
  const init = KEYS[key] ? { key, code: KEYS[key][0], keyCode: KEYS[key][1] } : { key, code: /^[0-9]$/.test(key) ? "Digit" + key : "" };
  const e = new KeyboardEvent("", init);
  tip.keydown(e);
  tip.keyup(e);
}
const panelEl = () => document.getElementById("claude-accounts-panel");
const panelState = () => panelEl()?.state ?? "none";
function snap() {
  return {
    panel: panelState(),
    focused: document.activeElement?.dataset?.alias ?? document.activeElement?.localName,
    tabs: gBrowser.tabs.map(t => [t.linkedBrowser?.currentURI?.spec ?? "(no browser)", t.userContextId, t.selected]),
  };
}
const ucid = alias => ClaudeAccounts.byAlias(alias)?.userContextId;
// Hands a link to Zen the way another app does: through the command-line
// handler, which calls browserDOMWindow.openURI with OPEN_EXTERNAL.
function externalOpen(url) {
  for (const spec of ["moz-src:///browser/components/BrowserContentHandler.sys.mjs", "resource:///modules/BrowserContentHandler.sys.mjs"]) {
    let Handler = null;
    try { Handler = ChromeUtils.importESModule(spec).nsDefaultCommandLineHandler; } catch (e) {}
    if (Handler) {
      const cmd = Cu.createCommandLine([url], null, Ci.nsICommandLine.STATE_REMOTE_EXPLICIT);
      new Handler().handle(cmd);
      return "command line";
    }
  }
  window.browserDOMWindow.openURI(Services.io.newURI(url), null, Ci.nsIBrowserDOMWindow.OPEN_DEFAULTWINDOW,
    Ci.nsIBrowserDOMWindow.OPEN_EXTERNAL, Services.scriptSecurityManager.getSystemPrincipal());
  return "openURI";
}
// Closes every tab but a blank one, the picker and the last-used account.
async function reset() {
  if (panelState() === "open" || panelState() === "showing") { panelEl().hidePopup(); await sleep(50); }
  const keep = gBrowser.addTrustedTab("about:blank");
  gBrowser.selectedTab = keep;
  for (const t of [...gBrowser.tabs]) if (t !== keep && t.linkedBrowser) gBrowser.removeTab(t, { animate: false });
  Services.prefs.clearUserPref("uc.claude-accounts.last-alias");
  Services.prefs.clearUserPref("uc.claude-accounts.ask-for-tagged");
  await sleep(50);
}
// The tab that appears after `count` tabs, once its address is set.
async function nextTab(count, what) {
  const tab = await waitFor(() => gBrowser.tabs.length > count && gBrowser.tabs.find(t => t.selected && t.linkedBrowser && t.linkedBrowser.currentURI.spec !== "about:blank"), what);
  return { url: tab.linkedBrowser.currentURI.spec, ucid: tab.userContextId, tab };
}
// A tab already at `url` in an account's container, as if the person had it open.
async function openAt(url, alias) {
  const tab = gBrowser.addTab(url, { userContextId: alias ? ucid(alias) : 0, triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal() });
  gBrowser.selectedTab = tab;
  await waitFor(() => tab.linkedBrowser.currentURI.spec.startsWith(url.replace(/#.*/, "")), "tab at " + url);
  return tab;
}
"""

REGISTER_JS = r"""
const [manifestPath] = arguments;
const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
file.initWithPath(manifestPath);
Components.manager.QueryInterface(Ci.nsIComponentRegistrar).autoRegister(file);
return true;
"""
SCRIPT_URL = "chrome://claudeaccountstest/content/" + SCRIPT.name

LOAD_JS = r"""
const [url] = arguments;
Services.scriptloader.loadSubScriptWithOptions(url, { target: window, ignoreCache: true });
await waitFor(() => CA()?.initialized, "script init");
return true;
"""

LOADED_BY_LOADER_JS = r"""
await waitFor(() => CA()?.initialized, "the fx-autoconfig loader to start the script", 20000);
return true;
"""


# --- Test cases ---------------------------------------------------------------

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def checks_from(result):
    return [(c[0], bool(c[1]), c[2] if len(c) > 2 else None) for c in result]


@test
def reads_links(m, shot):
    r = m.run(
        r"""
        const [artifact, uuid] = arguments;
        const tagged = parseLink(artifact + "#acct=gt");
        const queried = parseLink(artifact + "?acct=ut&x=1#section");
        const other = parseLink("https://claude.ai/chat/abc#acct=sr&keep");
        const code = parseLink("https://claude.ai/code/artifact/" + uuid.toUpperCase());
        const login = parseLink("https://claude.com/cai/oauth/authorize?code=true&client_id=c&login_hint=gt%40example.com&state=s");
        const console_ = parseLink("https://platform.claude.com/oauth/authorize?client_id=c");
        return [
          ["#acct tag read and taken off", tagged.tag === "gt" && tagged.url === artifact && tagged.uuid === uuid, tagged],
          ["?acct tag read, other parts kept", queried.tag === "ut" && queried.url === artifact + "?x=1#section", queried],
          ["fragment's other parts kept", other.tag === "sr" && other.url === "https://claude.ai/chat/abc#keep" && other.uuid === null, other],
          ["uuid link is the same artifact", code.uuid === uuid, code],
          ["short id decodes to the uuid", artifactUuid("V8n53oVvkdbSvFZSga3hkz") === "e3d5dd36-f4bf-49f0-bfe2-1211d98a5697"],
          ["sign-in link read with its email", login.kind === "login" && login.loginHint === "gt@example.com", login],
          ["Console sign-in link read", console_?.kind === "login", console_],
          ["other sites left alone", parseLink("https://claude.com/pricing") === null && parseLink("https://example.com/#acct=gt") === null
            && parseLink("http://claude.ai/") === null],
          ["a bad tag is not an alias", parseLink(artifact + "#acct=a%20b").tag === null],
        ];
        """,
        ARTIFACT,
        ARTIFACT_UUID,
    )
    return checks_from(r)


@test
def makes_a_container_per_account(m, shot):
    r = m.run(
        r"""
        const accounts = ClaudeAccounts.accounts;
        const names = ContextualIdentityService.getPublicIdentities().map(i => i.name);
        const ids = accounts.map(a => a.userContextId);
        const saved = JSON.parse(Services.prefs.getStringPref("uc.claude-accounts.containers"));
        // The color a picker dot gets from the container color's class.
        const drawn = color => {
          const el = document.createElementNS("http://www.w3.org/1999/xhtml", "div");
          el.className = "identity-color-" + color;
          document.documentElement.append(el);
          const value = getComputedStyle(el).getPropertyValue("--identity-icon-color").trim();
          el.remove();
          return value;
        };
        return [
          ["three accounts read", accounts.map(a => a.alias).join() === "ut,gt,sr", accounts],
          ["a container each", ["Claude · ut", "Claude · gt", "Claude · sr"].every(n => names.includes(n)), names],
          ["distinct containers", new Set(ids).size === 3 && ids.every(Number.isInteger), ids],
          ["the containers are remembered", saved.ut === ids[0] && saved.gt === ids[1] && saved.sr === ids[2], saved],
          ["colors given in order", accounts.map(a => a.color).join() === ["orange", ContextualIdentityService.resolveContainerColor?.("turquoise") ?? "turquoise", "green"].join(), accounts.map(a => a.color)],
          ["each color is one this build draws", accounts.every(a => drawn(a.color)), accounts.map(a => [a.color, drawn(a.color)])],
        ];
        """
    )
    return checks_from(r)


@test
def finds_the_table_in_the_home_folder(m, shot):
    # The other tests name their table by its full path; daily use leaves the
    # pref empty and gets ~/.config/claude-accounts/accounts.json. This only
    # works the path out: the person's own table is never read.
    r = m.run(
        r"""
        const PREF = "uc.claude-accounts.config";
        const had = Services.prefs.prefHasUserValue(PREF);
        const given = Services.prefs.getStringPref(PREF, "");
        const home = Services.dirsvc.get("Home", Ci.nsIFile).path;
        const pathFor = value => {
          try {
            if (value === null) Services.prefs.clearUserPref(PREF);
            else Services.prefs.setStringPref(PREF, value);
            return ClaudeAccounts.configPath();
          } catch (e) {
            return "threw: " + e.message;
          } finally {
            if (had) Services.prefs.setStringPref(PREF, given);
            else Services.prefs.clearUserPref(PREF);
          }
        };
        const byDefault = pathFor(null);
        const tilde = pathFor("~/tables//claude.json");
        return [
          ["the default table is ~/.config/claude-accounts/accounts.json",
            byDefault === PathUtils.join(home, ".config", "claude-accounts", "accounts.json"), byDefault],
          ["a ~/ path in the pref is under the home folder", tilde === PathUtils.join(home, "tables", "claude.json"), tilde],
        ];
        """
    )
    return checks_from(r)


@test
def tagged_link_opens_in_its_account(m, shot):
    r = m.run(
        r"""
        const [artifact] = arguments;
        await reset();
        const n = gBrowser.tabs.length;
        const via = externalOpen(artifact + "#acct=ut");
        const t = await nextTab(n, "the tagged artifact's tab");
        await sleep(150);
        return [
          ["opened in ut's container (via " + via + ")", t.ucid === ucid("ut"), t],
          ["tag gone, ut's organization added", t.url === artifact + "?org=" + ClaudeAccounts.byAlias("ut").org, t.url],
          ["no picker for a tagged link", panelState() !== "open", panelState()],
          ["one new tab", gBrowser.tabs.length === n + 1, snap()],
        ];
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def untagged_link_asks_and_a_digit_picks(m, shot):
    r = m.run(
        r"""
        const [artifact] = arguments;
        await reset();
        Services.prefs.setStringPref("uc.claude-accounts.last-alias", "ut");
        const n = gBrowser.tabs.length;
        externalOpen(artifact);
        await waitFor(() => panelState() === "open", "the picker");
        await waitFor(() => document.activeElement?.dataset?.alias, "a focused row");
        const rows = [...panelEl().querySelectorAll(".ca-row")].map(r => r.textContent);
        const focused = document.activeElement.dataset.alias;
        const title = panelEl().querySelector(".ca-title").textContent;
        press("2");
        const t = await nextTab(n, "gt's tab");
        await waitFor(() => panelState() === "closed", "the picker to close");
        return [
          ["picker lists the three accounts", rows.length === 3, rows],
          ["last used account preselected", focused === "ut", focused],
          ["artifact title", title.includes("artifact"), title],
          ["sr shows it has no email", rows[2].includes("no email set"), rows[2]],
          ["2 opened gt", t.ucid === ucid("gt") && t.url === artifact, t],
          ["gt is now the last used", Services.prefs.getStringPref("uc.claude-accounts.last-alias") === "gt"],
        ];
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def enter_takes_the_preselected_and_arrows_move(m, shot):
    r = m.run(
        r"""
        const [artifact] = arguments;
        await reset();
        Services.prefs.setStringPref("uc.claude-accounts.last-alias", "gt");
        let n = gBrowser.tabs.length;
        externalOpen("https://claude.ai/new");
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker");
        const first = document.activeElement.dataset.alias;
        press("Enter");
        const a = await nextTab(n, "the preselected account's tab");
        n = gBrowser.tabs.length;
        externalOpen("https://claude.ai/new");
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker again");
        press("ArrowDown");
        const moved = document.activeElement.dataset.alias;
        press("Enter");
        const b = await nextTab(n, "the account below's tab");
        return [
          ["gt preselected", first === "gt", first],
          ["Enter opened gt", a.ucid === ucid("gt"), a],
          ["down arrow moved to sr", moved === "sr", moved],
          ["Enter opened sr", b.ucid === ucid("sr"), b],
        ];
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def escape_opens_nothing(m, shot):
    r = m.run(
        r"""
        const [artifact] = arguments;
        await reset();
        const n = gBrowser.tabs.length;
        externalOpen(artifact);
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker");
        press("Escape");
        await waitFor(() => panelState() === "closed", "the picker to close");
        await sleep(300);
        // The queue is free again: the next link is handled.
        externalOpen(artifact + "#acct=gt");
        const t = await nextTab(n, "the next link's tab");
        return [
          ["no tab for the cancelled link", gBrowser.tabs.length === n + 1, snap()],
          ["the next link still works", t.ucid === ucid("gt"), t],
        ];
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def sign_in_link_opens_in_the_account_of_its_email(m, shot):
    r = m.run(
        r"""
        await reset();
        const n = gBrowser.tabs.length;
        const url = "https://claude.com/cai/oauth/authorize?code=true&client_id=c&response_type=code&login_hint=GT%40example.com&state=s";
        externalOpen(url);
        const t = await nextTab(n, "the sign-in tab");
        const m2 = gBrowser.tabs.length;
        externalOpen("https://claude.com/cai/oauth/authorize?code=true&client_id=c&state=s2");
        await waitFor(() => panelState() === "open", "the picker for a sign-in link without an email");
        const title = panelEl().querySelector(".ca-title").textContent;
        press("Escape");
        await waitFor(() => panelState() === "closed", "the picker to close");
        return [
          ["opened in gt's container", t.ucid === ucid("gt"), t],
          ["address unchanged", t.url === url, t.url],
          ["without an email it asks", title.startsWith("Sign in"), title],
          ["nothing opened after Esc", gBrowser.tabs.length === m2, snap()],
        ];
        """
    )
    return checks_from(r)


@test
def other_links_open_as_before(m, shot):
    r = m.run(
        r"""
        await reset();
        const n = gBrowser.tabs.length;
        externalOpen("https://example.com/#acct=gt");
        const t = await nextTab(n, "example.com's tab");
        await sleep(200);
        return [
          ["opened in the default container", t.ucid === 0, t],
          ["address untouched", t.url === "https://example.com/#acct=gt", t.url],
          ["no picker", panelState() !== "open", panelState()],
        ];
        """
    )
    return checks_from(r)


@test
def remembers_what_opened_an_artifact(m, shot):
    r = m.run(
        r"""
        const [artifact, uuid] = arguments;
        await reset();
        const tab = await openAt(artifact, "sr");
        ClaudeAccounts.notify(tab.linkedBrowser, uuid, 200);
        const remembered = ClaudeAccounts.memoryFor(uuid);
        Services.prefs.setStringPref("uc.claude-accounts.last-alias", "ut");
        externalOpen(artifact);
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker");
        const focused = document.activeElement.dataset.alias;
        const note = panelEl().querySelector('.ca-row[data-alias="sr"] .ca-note').textContent;
        press("Escape");
        await waitFor(() => panelState() === "closed", "the picker to close");
        await ClaudeAccounts.saveMemory();
        const onDisk = await IOUtils.readJSON(PathUtils.join(PathUtils.profileDir, "claude-accounts-memory.json"));
        ClaudeAccounts.notify(tab.linkedBrowser, uuid, 404);
        const forgotten = ClaudeAccounts.memoryFor(uuid);
        return [
          ["a 200 remembers sr", remembered === "sr", remembered],
          ["the picker preselects it", focused === "sr", focused],
          ["and says why", note.includes("opened it before"), note],
          ["kept on disk", onDisk.artifacts[uuid]?.alias === "sr", onDisk],
          ["a 404 in sr forgets it", forgotten === null, forgotten],
        ];
        """,
        ARTIFACT,
        ARTIFACT_UUID,
    )
    return checks_from(r)


@test
def failed_artifact_offers_other_accounts(m, shot):
    r = m.run(
        r"""
        const [artifact, uuid] = arguments;
        await reset();
        const tab = await openAt(artifact, "ut");
        ClaudeAccounts.notify(tab.linkedBrowser, uuid, 404);
        const box = gBrowser.getNotificationBox(tab.linkedBrowser);
        const bar = await waitFor(() => box.getNotificationWithValue("claude-accounts-failed"), "the bar");
        const labels = barButtons(bar).map(buttonLabel).filter(Boolean);
        const button = barButtons(bar).find(b => buttonLabel(b) === "Open as gt");
        const n = gBrowser.tabs.length;
        button.click();
        const t = await waitFor(() => gBrowser.selectedTab !== tab && gBrowser.selectedTab.userContextId === ucid("gt")
          && gBrowser.selectedTab.linkedBrowser.currentURI.spec !== "about:blank" && gBrowser.selectedTab, "gt's tab");
        const countAfter = gBrowser.tabs.length;
        // Another artifact's request in the same tab says nothing about this one.
        const other = await openAt(artifact, "gt");
        ClaudeAccounts.notify(other.linkedBrowser, "00000000-0000-4000-8000-000000000000", 404);
        await sleep(200);
        const quiet = !gBrowser.getNotificationBox(other.linkedBrowser).getNotificationWithValue("claude-accounts-failed");
        return [
          ["bar names the account", barText(bar).includes("Claude account ut can't open"), barText(bar).slice(0, 300)],
          ["buttons for the other accounts", labels.some(l => l === "Open as gt") && labels.some(l => l === "Open as sr") && !labels.includes("Open as ut"), labels],
          ["Open as gt reopened it in gt", t.linkedBrowser.currentURI.spec === artifact && !tab.isConnected, snap()],
          ["the old tab is gone", countAfter === n, snap()],
          ["another artifact's 404 shows no bar", quiet],
        ];
        """,
        ARTIFACT,
        ARTIFACT_UUID,
    )
    return checks_from(r)


@test
def reopen_in_another_account(m, shot):
    r = m.run(
        r"""
        const [artifact] = arguments;
        await reset();
        const tab = await openAt(artifact, "gt");
        const menu = document.getElementById("tabContextMenu");
        const item = document.getElementById("claude-accounts-reopen");
        // The real tab menu, opened as a right-click on the tab opens it: the
        // event's target becomes the menu's triggerNode.
        async function menuOn(t) {
          const click = new MouseEvent("contextmenu", { bubbles: false, cancelable: true });
          t.dispatchEvent(click);
          menu.openPopupAtScreen(20, 20, true, click);
          await waitFor(() => menu.state === "open", "the tab menu");
          const hidden = item.hidden;
          menu.hidePopup();
          await waitFor(() => menu.state === "closed", "the tab menu to close");
          return hidden;
        }
        const shownOnClaude = !(await menuOn(tab));
        const blank = gBrowser.addTrustedTab("about:blank");
        const hiddenElsewhere = await menuOn(blank);
        gBrowser.removeTab(blank);
        gBrowser.selectedTab = tab;
        const n = gBrowser.tabs.length;
        CA().pickForTab(tab);
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker");
        const disabled = [...panelEl().querySelectorAll(".ca-row[disabled]")].map(r => r.dataset.alias);
        press("2"); // gt, this tab's own: nothing happens
        await sleep(200);
        const stillOpen = panelState() === "open";
        press("1");
        const t = await waitFor(() => gBrowser.selectedTab.userContextId === ucid("ut") && gBrowser.selectedTab.linkedBrowser.currentURI.spec !== "about:blank" && gBrowser.selectedTab, "ut's tab");
        return [
          ["menu item on a claude.ai tab", shownOnClaude],
          ["no menu item elsewhere", hiddenElsewhere],
          ["the tab's own account can't be picked", disabled.join() === "gt", disabled],
          ["picking it does nothing", stillOpen],
          ["1 reopened it as ut with ut's organization", t.linkedBrowser.currentURI.spec.startsWith(artifact + "?org="), snap()],
          ["in place of the old tab", !tab.isConnected && gBrowser.tabs.length === n, snap()],
        ];
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def toolbar_button_opens_claude_in_an_account(m, shot):
    r = m.run(
        r"""
        await reset();
        const widget = CustomizableUI.getWidget("claude-accounts-button");
        const n = gBrowser.tabs.length;
        CA().switcher();
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker");
        const title = panelEl().querySelector(".ca-title").textContent;
        press("3");
        const t = await nextTab(n, "sr's claude.ai tab");
        return [
          ["the button exists", widget?.provider === CustomizableUI.PROVIDER_API, widget?.provider],
          ["asks which account", title === "Open Claude as which account?", title],
          ["3 opened claude.ai as sr", t.ucid === ucid("sr") && t.url === "https://claude.ai/new", t],
        ];
        """
    )
    return checks_from(r)


@test
def ask_for_tagged_pref(m, shot):
    r = m.run(
        r"""
        const [artifact] = arguments;
        await reset();
        Services.prefs.setBoolPref("uc.claude-accounts.ask-for-tagged", true);
        const n = gBrowser.tabs.length;
        externalOpen(artifact + "#acct=gt");
        await waitFor(() => panelState() === "open" && document.activeElement?.dataset?.alias, "the picker");
        const focused = document.activeElement.dataset.alias;
        const note = panelEl().querySelector('.ca-row[data-alias="gt"] .ca-note').textContent;
        press("Enter");
        const t = await nextTab(n, "gt's tab");
        return [
          ["tagged account preselected", focused === "gt", focused],
          ["marked as the publisher", note.includes("published it"), note],
          ["Enter opened gt", t.ucid === ucid("gt"), t],
        ];
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def account_in_a_chrome_profile_opens_in_chrome(m, shot):
    # Chrome never starts: the launcher is swapped for a recorder, and Chrome's
    # list of profiles is a file of the test's own.
    r = m.run(
        r"""
        const [artifact] = arguments;
        const PREF = "uc.claude-accounts.config";
        const given = Services.prefs.getStringPref(PREF);
        const tablePath = PathUtils.join(PathUtils.profileDir, "accounts-with-chrome.json");
        const statePath = PathUtils.join(PathUtils.profileDir, "chrome-local-state.json");
        const table = await IOUtils.readJSON(given);
        table.accounts = table.accounts.map(a => (a.alias === "gt" ? { ...a, chrome: "Work" } : a));
        await IOUtils.writeJSON(tablePath, table);
        await IOUtils.writeJSON(statePath, { profile: { info_cache: { Default: { name: "Person 1" }, "Profile 3": { name: "Work" } } } });
        const launched = [];
        const saved = { launch: ClaudeAccounts.launch, chromeStatePath: ClaudeAccounts.chromeStatePath };
        ClaudeAccounts.launch = async args => { launched.push(args); };
        ClaudeAccounts.chromeStatePath = statePath;
        Services.prefs.setStringPref(PREF, tablePath);
        const box = () => gBrowser.getNotificationBox(gBrowser.selectedBrowser);
        try {
          await ClaudeAccounts.refresh();
          const gt = ClaudeAccounts.byAlias("gt");
          const read = gt.chrome;
          await reset();
          const n = gBrowser.tabs.length;
          externalOpen(artifact + "#acct=gt");
          await waitFor(() => launched.length === 1, "the link to go to Chrome");
          await sleep(300);
          const tabsAfter = gBrowser.tabs.length;

          // Reopening a tab as gt moves it to Chrome.
          const tab = await openAt(artifact, "ut");
          await CA().openIn(parseLink(artifact), gt, { replaceTab: tab });
          await waitFor(() => !tab.isConnected, "the reopened tab to leave Zen");

          // The picker says where gt opens.
          externalOpen(artifact);
          await waitFor(() => panelState() === "open", "the picker");
          const gtRow = [...panelEl().querySelectorAll(".ca-row")].find(r => r.dataset.alias === "gt")?.textContent ?? "";
          press("Escape");
          await waitFor(() => panelState() === "closed", "the picker to close");

          // A profile Chrome doesn't have: nothing starts, a bar says so.
          gt.chrome = "Nobody";
          externalOpen(artifact + "#acct=gt");
          const bar = await waitFor(() => box().getNotificationWithValue("claude-accounts-chrome"), "the bar about the missing profile");
          const text = barText(bar);
          const buttons = barButtons(bar).map(buttonLabel);
          box().removeNotification(bar);

          // The real launcher, on an app that doesn't exist: open's failure
          // comes back as an error.
          let launchError = "";
          try {
            await saved.launch.call(ClaudeAccounts, ["-a", "No Such App (multi-cc-switcher test)"]);
          } catch (e) {
            launchError = e.message;
          }
          return [
            ["the table's chrome field is read", read === "Work", read],
            ["a tagged link goes to Chrome, in the profile named Work",
              JSON.stringify(launched[0]) === JSON.stringify(["-na", "Google Chrome", "--args", "--profile-directory=Profile 3", artifact]), launched[0]],
            ["no Zen tab for it", tabsAfter === n, snap()],
            ["a tab reopened as gt goes to Chrome and leaves Zen", launched[1]?.at(-1) === artifact, launched[1]],
            ["the picker says gt opens in Chrome", gtRow.includes("opens in Chrome"), gtRow],
            ["an unknown profile starts nothing", launched.length === 2, launched],
            ["a bar names the missing profile", text.includes('no profile "Nobody"'), text],
            ["the bar offers Zen instead", buttons.some(b => b.includes("Open in Zen as gt")), buttons],
            ["the real launcher runs open and reports its failure", /^open exited with [1-9]/.test(launchError), launchError],
          ];
        } finally {
          ClaudeAccounts.launch = saved.launch;
          ClaudeAccounts.chromeStatePath = saved.chromeStatePath;
          Services.prefs.setStringPref(PREF, given);
          await ClaudeAccounts.refresh();
        }
        """,
        ARTIFACT,
    )
    return checks_from(r)


@test
def unload_restores_everything(m, shot):
    r = m.run(
        r"""
        const [artifact, url] = arguments;
        await reset();
        CA().destroy();
        const restored = !CA();
        const n = gBrowser.tabs.length;
        externalOpen(artifact + "#acct=gt");
        const t = await nextTab(n, "the link opened as usual");
        const gone = !document.getElementById("claude-accounts-panel") && !document.getElementById("claude-accounts-reopen");
        // Load it again for any later test.
        Services.scriptloader.loadSubScriptWithOptions(url, { target: window, ignoreCache: true });
        await waitFor(() => CA()?.initialized, "script init again");
        return [
          ["instance gone", restored],
          ["links open as usual once unloaded", t.ucid === 0 && t.url === artifact + "#acct=gt", t],
          ["panel and menu item removed", gone],
          ["loads again", !!CA()],
        ];
        """,
        ARTIFACT,
        SCRIPT_URL,
    )
    return checks_from(r)


@test
def link_that_opens_a_window(m, shot):
    r = m.run(
        r"""
        const [artifact, url] = arguments;
        await reset();
        // What Zen's command-line handler does when no window is open: a new
        // window whose first argument is the link, marked as from another app.
        const bag = Cc["@mozilla.org/hash-property-bag;1"].createInstance(Ci.nsIWritablePropertyBag2);
        bag.setPropertyAsBool("fromExternal", true);
        const win = window.openDialog(AppConstants.BROWSER_CHROME_URL, "_blank", "chrome,all,dialog=no", artifact + "#acct=gt", bag);
        await new Promise(r => win.addEventListener("load", r, { once: true }));
        // Under the loader the script starts in the new window by itself.
        if (!win[Symbol.for("multi-cc-switcher")]) {
          Services.scriptloader.loadSubScriptWithOptions(url, { target: win, ignoreCache: true });
        }
        const inGt = () => win.gBrowser?.tabs.find(t => t.userContextId === ucid("gt") && t.linkedBrowser?.currentURI?.spec.startsWith(artifact));
        let t = null, err = null;
        try { t = await waitFor(inGt, "the link in gt's container in the new window", 20000); } catch (e) { err = String(e).slice(0, 600); }
        await sleep(300);
        const tabs = win.gBrowser?.tabs.map(x => [x.linkedBrowser?.currentURI?.spec ?? "(no browser)", x.userContextId]) ?? [];
        const leftover = tabs.filter(([u, id]) => id === 0 && u.startsWith(artifact));
        win.close();
        return [
          ["the link opened in gt", !!t, err ?? tabs],
          ["its first tab was closed", !!t && leftover.length === 0, tabs],
        ];
        """,
        ARTIFACT,
        SCRIPT_URL,
    )
    return checks_from(r)


# --- Runner -------------------------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_prefs(profile, port, accounts_path):
    prefs = {
        "marionette.port": port,
        "zen.welcome-screen.seen": True,
        "zen.urlbar.open-on-startup": False,
        "zen.updates.show-update-notification": False,
        "browser.shell.checkDefaultBrowser": False,
        "devtools.console.stdout.chrome": True,
        "app.update.disabledForTesting": True,
        "uc.claude-accounts.config": str(accounts_path),
        # Every web request fails at once on a dead proxy: no traffic leaves.
        "network.proxy.type": 1,
        "network.proxy.http": "127.0.0.1",
        "network.proxy.http_port": 9,
        "network.proxy.ssl": "127.0.0.1",
        "network.proxy.ssl_port": 9,
        "network.proxy.no_proxies_on": "",
        "network.proxy.allow_hijacking_localhost": True,
        "browser.sessionstore.resume_from_crash": False,
    }
    lines = [f'user_pref("{k}", {json.dumps(v)});' for k, v in prefs.items()]
    (Path(profile) / "user.js").write_text("\n".join(lines) + "\n")


def run_tests(m, out, only):
    """Runs the test cases; returns the number of failed checks."""

    def shot_for(test_name):
        def shot(name):
            try:
                png = m.send("WebDriver:TakeScreenshot", {"full": False, "hash": False, "scroll": False})
                (out / f"{test_name}-{name}.png").write_bytes(base64.b64decode(png["value"]))
            except Exception as e:  # screenshots are a bonus, not a test
                print(f"  (screenshot {name} failed: {str(e).splitlines()[0]})")

        return shot

    failures = 0
    for fn in TESTS:
        if only and only not in fn.__name__:
            continue
        print(f"\n{fn.__name__}")
        try:
            checks = fn(m, shot_for(fn.__name__))
        except Exception as e:
            failures += 1
            print(f"  ERROR {e}")
            continue
        for label, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'} {label}")
            if not ok:
                failures += 1
                print(f"       state: {json.dumps(detail, default=str)[:1500]}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zen", default=os.environ.get("ZEN_BINARY", DEFAULT_ZEN))
    parser.add_argument("--headful", action="store_true", help="show the browser window")
    parser.add_argument("--keep-profile", action="store_true")
    parser.add_argument("-k", dest="only", help="run tests whose name contains this")
    parser.add_argument(
        "--via-loader",
        metavar="CHROME_DIR",
        help="copy utils/, JS/claude-accounts.uc.js and JS/claude-accounts/ from this profile chrome "
        "folder into the test profile and let fx-autoconfig (installed in Zen.app) load the script",
    )
    args = parser.parse_args()

    out = HERE / "output"
    out.mkdir(exist_ok=True)
    profile = tempfile.mkdtemp(prefix="multi-cc-switcher-test-")
    accounts_path = Path(profile) / "accounts.json"
    accounts_path.write_text(json.dumps(ACCOUNTS, indent=2))
    port = free_port()
    write_prefs(profile, port, accounts_path)
    if args.via_loader:
        source = Path(args.via_loader).expanduser()
        target = Path(profile) / "chrome"
        shutil.copytree(source / "utils", target / "utils")
        (target / "JS").mkdir(parents=True)
        shutil.copy2(source / "JS" / SCRIPT.name, target / "JS" / SCRIPT.name)
        shutil.copytree(source / "JS" / "claude-accounts", target / "JS" / "claude-accounts")

    env = dict(os.environ)
    # Lets Marionette run scripts in the browser's own (chrome) window.
    env["MOZ_REMOTE_ALLOW_SYSTEM_ACCESS"] = "1"
    if not args.headful:
        env["MOZ_HEADLESS"] = "1"
        env["MOZ_HEADLESS_WIDTH"] = "1280"
        env["MOZ_HEADLESS_HEIGHT"] = "800"
    cmd = [args.zen, "--marionette", "--remote-allow-system-access", "--no-remote", "--profile", profile]
    if not args.headful:
        cmd.append("--headless")
    log_path = out / "zen.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)

    failures = 0
    m = None
    try:
        try:
            m = Marionette(port)
            m.send("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {}}})
            m.send("WebDriver:SetTimeouts", {"script": 60000})
            m.send("Marionette:SetContext", {"value": "chrome"})
            # The test's own chrome:// address for the mod; registered before
            # COMMON_JS (which imports the module through it) first runs.
            manifest = Path(profile) / "claudeaccountstest.manifest"
            manifest.write_text(f"content claudeaccountstest {MOD.as_uri()}/\n")
            m.send(
                "WebDriver:ExecuteAsyncScript",
                {"script": "const done = arguments[arguments.length - 1];\n(async () => {\n" + REGISTER_JS + "\n})().then(done);", "args": [str(manifest)]},
            )
            info = m.run(
                "await window.delayedStartupPromise; return { version: Services.appinfo.version,"
                " app: Services.appinfo.name + ' ' + (globalThis.AppConstants?.MOZ_APP_VERSION_DISPLAY ?? '') };"
            )
            print(f"{info['app']} (Firefox {info['version']}), profile {profile}")
            if args.via_loader:
                print("script started by the fx-autoconfig loader")
                m.run(LOADED_BY_LOADER_JS)
            else:
                m.run(LOAD_JS, SCRIPT_URL)
        except Exception as e:
            failures += 1
            print(f"FAIL setup: {e}")
        else:
            failures += run_tests(m, out, args.only)
        if m:
            try:
                m.send("Marionette:Quit", {"flags": ["eForceQuit"]})
            except Exception:
                pass
    finally:
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if not args.keep_profile:
            shutil.rmtree(profile, ignore_errors=True)

    errors = [l for l in log_path.read_text(errors="replace").splitlines() if "[claude-accounts]" in l]
    if errors:
        print("\nscript messages in the browser log:")
        print("\n".join("  " + l for l in errors[:20]))
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}; log in {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
