#!/usr/bin/env python3
"""End-to-end test for pr-folder.uc.js against the Zen you have installed.

Starts Zen headless with a throwaway profile (your own profile is never
touched), drives it over Marionette, loads the script, makes a GitHub
pull-request live folder and checks what the mod does with it. The folder's
requests are answered by a stand-in for github.com inside the browser, and
every other web request goes to a dead proxy, so nothing reaches GitHub.

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
SCRIPT = MOD / "pr-folder.uc.js"
DEFAULT_ZEN = "/Applications/Zen.app/Contents/MacOS/zen"


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
const PF = () => window[Symbol.for("arc-like-pr-folder")];
const PrFolder = () => PF()?.PrFolder;
// Pure helpers from the test's own copy of the module (the running script's
// copy may sit at another address).
const { prKeyFromUrl, withRecentWindow, reviewScope, reviewQuery, WRAPPED } = ChromeUtils.importESModule(
  "chrome://prfoldertest/content/pr-folder/PrFolder.sys.mjs");
const { ZenLiveFoldersManager: LFM } = ChromeUtils.importESModule("resource:///modules/zen/ZenLiveFoldersManager.sys.mjs");
const ITEM = "zen-live-folder-item-id";
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
// The date `n` days ago, as GitHub's search writes it.
const day = n => new Date(Date.now() - n * 86400000).toISOString().slice(0, 10);
// A stand-in for github.com: live folder requests get answers made from
// FAKE.prs, filtered the way GitHub filters them.
const FAKE = (window.__prFolderFake ??= { prs: [], requests: [] });
FAKE.fetch ??= async function (url) {
  const u = new URL(url);
  const q = u.searchParams.get("q");
  FAKE.requests.push({ path: u.pathname, q, at: Date.now() });
  if (u.pathname === "/pulls" && q === null) {
    return { status: 200, text: "{}", contentType: "application/json" };
  }
  if (u.pathname === "/pulls") {
    const since = q.match(/updated:>=(\d{4}-\d{2}-\d{2})/)?.[1];
    const excluded = [...q.matchAll(/-repo:(\S+)/g)].map(m => m[1].toLowerCase());
    let match;
    if (/(^|\s)-author:@me/.test(q)) {
      // The search for PRs by others you haven't reviewed, in some repos or accounts.
      const repos = [...q.matchAll(/(?:^|[\s(])repo:([^\s)]+)/g)].map(m => m[1].toLowerCase());
      const owners = [...q.matchAll(/(?:^|[\s(])(?:user|org):([^\s)]+)/g)].map(m => m[1].toLowerCase());
      const inScope = pr => repos.includes(pr.repo.toLowerCase()) || owners.includes(pr.repo.split("/")[0].toLowerCase());
      match = pr => pr.byOthers && !pr.reviewedByMe && !(pr.draft && q.includes("draft:false")) && inScope(pr);
    } else {
      const want = { mine: /author:@me/.test(q), review: /user-review-requested:@me/.test(q), assigned: /assignee:@me/.test(q) };
      match = pr => (want.mine && pr.mine) || (want.review && pr.review) || (want.assigned && pr.assigned);
    }
    const results = FAKE.prs
      .filter(pr => (!since || pr.updated >= since) && !excluded.includes(pr.repo.toLowerCase()) && match(pr))
      .map(pr => ({ repoNameWithOwner: pr.repo, number: pr.number, title: pr.title,
                    author: { displayLogin: pr.mine ? "me" : pr.byOthers ? "an-outside-contributor" : "a-teammate" },
                    permalink: `https://github.com/${pr.repo}/pull/${pr.number}` }));
    return { status: 200, text: JSON.stringify({ payload: { pullsDashboardSurfaceContentRoute: { results } } }), contentType: "application/json" };
  }
  return { status: 200, text: "<html><body></body></html>", contentType: "text/html" };
};
const Provider = () => LFM.registry.get("github");
const prLiveFolder = () => [...LFM.liveFolders.values()].find(lf => lf.state?.type === "pull-requests");
const folderOf = lf => LFM.getFolderForLiveFolder(lf);
// The folder's items, as "owner/repo#n", sorted.
const itemsOf = lf => folderOf(lf).tabs.map(t => t.getAttribute(ITEM)).filter(Boolean).map(id => id.slice(lf.id.length + 1)).sort();
const tabFor = (lf, rawId) => folderOf(lf).tabs.find(t => t.getAttribute(ITEM) === `${lf.id}:${rawId}`);
const tabsWithItem = (lf, rawId) => gBrowser.tabs.filter(t => t.getAttribute(ITEM) === `${lf.id}:${rawId}`);
const pullRequests = () => FAKE.requests.filter(r => r.path === "/pulls" && r.q !== null);
const lastQuery = () => pullRequests().at(-1)?.q ?? "";
function snap() {
  const lf = prLiveFolder();
  return {
    items: lf && folderOf(lf) ? itemsOf(lf) : null,
    lastQuery: lastQuery(),
    lastRefresh: PrFolder()?.lastRefresh,
    requests: FAKE.requests.length,
    tabs: gBrowser.tabs.map(t => [t.linkedBrowser?.currentURI?.spec ?? "(lazy)", t.getAttribute(ITEM), t.pinned, !!t.group, t.selected]),
  };
}
// Refreshes the PR folder now and lets Zen add the new tabs.
async function refreshNow(lf) {
  await PrFolder().refreshFolder(lf, { reason: "test", force: true });
  await sleep(100);
}
// Waits until the gap between automatic refreshes has passed.
async function pastTheGap(lf) {
  const gap = Services.prefs.getIntPref("uc.pr-folder.min-gap-seconds", 10) * 1000;
  const wait = (lf.state.lastFetched || 0) + gap - Date.now();
  if (wait > 0) await sleep(wait + 50);
}
// An ordinary tab at `url`, selected, in the current space.
async function openTab(url) {
  const tab = gBrowser.addTrustedTab(url, { inBackground: false });
  gBrowser.selectedTab = tab;
  await waitFor(() => tab.linkedBrowser?.currentURI?.spec.startsWith(url), "tab at " + url);
  return tab;
}
// Selects a plain about:blank tab, so no folder tab is on screen.
async function selectBlank() {
  let tab = gBrowser.tabs.find(t => !t.pinned && !t.group && t.linkedBrowser?.currentURI?.spec === "about:blank");
  tab ??= gBrowser.addTrustedTab("about:blank");
  gBrowser.selectedTab = tab;
  await sleep(50);
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
SCRIPT_URL = "chrome://prfoldertest/content/" + SCRIPT.name

LOAD_JS = r"""
const [url] = arguments;
Services.scriptloader.loadSubScriptWithOptions(url, { target: window, ignoreCache: true });
await waitFor(() => PF()?.initialized, "script init", 20000);
return true;
"""

LOADED_BY_LOADER_JS = r"""
await waitFor(() => PF()?.initialized, "the fx-autoconfig loader to start the script", 20000);
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
def reads_pr_links(m, shot):
    r = m.run(
        r"""
        const now = Date.UTC(2026, 10, 25, 12);
        const added = href => new URL(withRecentWindow(href, 14, now)).searchParams.get("q");
        return [
          ["a PR's conversation", prKeyFromUrl("https://github.com/Acme/app/pull/44") === "Acme/app#44"],
          ["a PR's files, with a query and an anchor", prKeyFromUrl("https://github.com/o/r/pull/7/files?w=1#diff") === "o/r#7"],
          ["not the list of PRs", prKeyFromUrl("https://github.com/o/r/pulls") === null],
          ["not an issue", prKeyFromUrl("https://github.com/o/r/issues/7") === null],
          ["not another site", prKeyFromUrl("https://gitlab.com/o/r/pull/7") === null],
          ["not a non-address", prKeyFromUrl("not a url") === null],
          ["the window goes on the search", added("https://github.com/pulls?q=is%3Apr+is%3Aopen") === "is:pr is:open updated:>=2026-11-11"],
          ["0 days adds nothing", withRecentWindow("https://github.com/pulls?q=is%3Apr", 0, now) === "https://github.com/pulls?q=is%3Apr"],
          ["a search with its own time limit stays as is", withRecentWindow("https://github.com/pulls?q=updated%3A%3E2026-01-01", 14, now) === "https://github.com/pulls?q=updated%3A%3E2026-01-01"],
          ["a request without a search stays as is", withRecentWindow("https://github.com/pulls", 14, now) === "https://github.com/pulls"],
          ["review-in reads repos and accounts", JSON.stringify(reviewScope(" Acme/app, zhuconv  Acme/app bad!name ")) === JSON.stringify(["Acme/app", "zhuconv"])],
          ["an empty review-in names nothing", reviewScope("").length === 0],
          ["one repo: no parentheses", reviewQuery(["o/r"]) === "is:pr is:open archived:false draft:false sort:updated-desc -author:@me -reviewed-by:@me repo:o/r", reviewQuery(["o/r"])],
          ["several, with an excluded repo", reviewQuery(["o/r", "acme"], ["o/old"]).endsWith("-repo:o/old (repo:o/r OR user:acme)"), reviewQuery(["o/r", "acme"], ["o/old"])],
        ];
        """
    )
    return checks_from(r)


@test
def shows_your_recent_prs_and_review_requests(m, shot):
    r = m.run(
        r"""
        Provider().prototype.fetch = FAKE.fetch;
        FAKE.prs = [
          { repo: "zhuconv/demo", number: 1, title: "Mine, from yesterday", mine: true, updated: day(1) },
          { repo: "zhuconv/demo", number: 2, title: "Mine, three days old", mine: true, updated: day(3) },
          { repo: "Acme/app", number: 3, title: "Waiting for my review", review: true, updated: day(2) },
          { repo: "zhuconv/old", number: 4, title: "Mine, a month old", mine: true, updated: day(30) },
          { repo: "someone/else", number: 5, title: "Neither mine nor for me", updated: day(1) },
        ];
        const id = await LFM.createFolder("github:pull-requests");
        const lf = LFM.getFolder(id);
        await waitFor(() => itemsOf(lf).length === 3, "three PRs in the new folder");
        const items = itemsOf(lf);
        return [
          ["the folder lists yours and the one waiting for your review", JSON.stringify(items) === JSON.stringify(["Acme/app#3", "zhuconv/demo#1", "zhuconv/demo#2"]), items],
          ["'Created by Me' starts ticked", lf.state.options.authorMe === true, lf.state.options],
          ["'My Review Requests' starts ticked", lf.state.options.reviewRequested === true, lf.state.options],
          ["'Assigned to Me' keeps Zen's default", lf.state.options.assignedMe === undefined, lf.state.options],
        ];
        """
    )
    shot("folder")
    return checks_from(r)


@test
def asks_github_for_the_last_two_weeks(m, shot):
    r = m.run(
        r"""
        const q = lastQuery();
        return [
          ["the search has the 14-day window", q.includes(`updated:>=${day(14)}`), q],
          ["it asks for PRs you created", q.includes("author:@me"), q],
          ["it asks for PRs waiting for your review", q.includes("user-review-requested:@me"), q],
          ["it asks for PRs assigned to you", q.includes("assignee:@me"), q],
          ["it asks for open PRs only", q.includes("is:open"), q],
        ];
        """
    )
    return checks_from(r)


@test
def recent_days_setting(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const PREF = "uc.pr-folder.recent-days";
        try {
          Services.prefs.setIntPref(PREF, 0);
          await refreshNow(lf);
          await waitFor(() => itemsOf(lf).includes("zhuconv/old#4"), "the old PR with no window");
          const noLimit = itemsOf(lf), qNoLimit = lastQuery();
          Services.prefs.setIntPref(PREF, 2);
          await refreshNow(lf);
          await waitFor(() => !itemsOf(lf).includes("zhuconv/demo#2"), "the 3-day-old PR to leave a 2-day window");
          const twoDays = itemsOf(lf);
          Services.prefs.clearUserPref(PREF);
          await refreshNow(lf);
          await waitFor(() => itemsOf(lf).length === 3, "back to three PRs");
          return [
            ["0 days: no time limit in the search", !qNoLimit.includes("updated:"), qNoLimit],
            ["0 days: the month-old PR comes back", noLimit.includes("zhuconv/old#4"), noLimit],
            ["2 days: only the last two days", JSON.stringify(twoDays) === JSON.stringify(["Acme/app#3", "zhuconv/demo#1"]), twoDays],
            ["the default is 14 days again", JSON.stringify(itemsOf(lf)) === JSON.stringify(["Acme/app#3", "zhuconv/demo#1", "zhuconv/demo#2"]), itemsOf(lf)],
          ];
        } finally {
          Services.prefs.clearUserPref(PREF);
        }
        """
    )
    return checks_from(r)


@test
def a_box_you_untick_stays_unticked(m, shot):
    # With the folder open: there Zen alone would count the closing of the
    # PR's tab as a dismissal and not bring the PR back.
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const folder = folderOf(lf);
        const wasCollapsed = folder.collapsed;
        folder.collapsed = false;
        try {
          await sleep(300);
          lf.state.options.reviewRequested = false;
          await refreshNow(lf);
          await waitFor(() => !itemsOf(lf).includes("Acme/app#3"), "the review request to leave");
          await sleep(1000);  // Zen records a dismissal a moment after the close
          const q = lastQuery(), unticked = lf.state.options.reviewRequested;
          const dismissed = LFM.dismissedItems.has(`${lf.id}:Acme/app#3`);
          lf.state.options.reviewRequested = true;
          await refreshNow(lf);
          await waitFor(() => itemsOf(lf).includes("Acme/app#3"), "the review request to come back");
          return [
            ["the search no longer asks for review requests", !q.includes("user-review-requested"), q],
            ["the box stays unticked", unticked === false, unticked],
            ["the PR leaving the list is no dismissal", !dismissed],
            ["ticked again, the PR comes back", itemsOf(lf).includes("Acme/app#3"), itemsOf(lf)],
          ];
        } finally {
          folder.collapsed = wasCollapsed;
          await sleep(300);
        }
        """
    )
    return checks_from(r)


@test
def lists_prs_waiting_for_you_in_repos_you_name(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const PREF = "uc.pr-folder.review-in";
        FAKE.prs.push(
          { repo: "Acme/app", number: 45, title: "A contributor's PR nobody asked you about", byOthers: true, updated: day(12) },
          { repo: "Acme/app", number: 46, title: "One you already reviewed", byOthers: true, reviewedByMe: true, updated: day(1) },
          { repo: "Acme/app", number: 47, title: "A draft", byOthers: true, draft: true, updated: day(1) },
          { repo: "Acme/app", number: 48, title: "Quiet for a month", byOthers: true, updated: day(30) },
          { repo: "Acme/tools", number: 9, title: "In another repo of the account", byOthers: true, updated: day(1) },
          { repo: "elsewhere/lib", number: 3, title: "Outside what you named", byOthers: true, updated: day(1) },
        );
        try {
          const before = itemsOf(lf);
          Services.prefs.setStringPref(PREF, "Acme/app");
          await waitFor(() => itemsOf(lf).includes("Acme/app#45"), "the waiting PR after setting review-in");
          await sleep(200);
          const oneRepo = itemsOf(lf);
          const q = pullRequests().map(r => r.q).reverse().find(q => q.includes("-author:@me")) ?? "";
          Services.prefs.setStringPref(PREF, "Acme");
          await waitFor(() => itemsOf(lf).includes("Acme/tools#9"), "the account-wide search");
          const account = itemsOf(lf);
          Services.prefs.clearUserPref(PREF);
          await waitFor(() => !itemsOf(lf).includes("Acme/app#45"), "the waiting PRs to leave with review-in cleared");
          return [
            ["the repo's PR by someone else shows, though nobody asked for your review", oneRepo.includes("Acme/app#45"), oneRepo],
            ["not one you already reviewed", !oneRepo.includes("Acme/app#46"), oneRepo],
            ["not a draft", !oneRepo.includes("Acme/app#47"), oneRepo],
            ["not one quiet for a month", !oneRepo.includes("Acme/app#48"), oneRepo],
            ["not other repos", !oneRepo.includes("Acme/tools#9") && !oneRepo.includes("elsewhere/lib#3"), oneRepo],
            ["your own PRs stay", before.every(id => oneRepo.includes(id)), [before, oneRepo]],
            ["the search skips you and what you reviewed", q.includes("-author:@me") && q.includes("-reviewed-by:@me") && q.includes("repo:Acme/app"), q],
            ["it has the 14-day window too", q.includes(`updated:>=${day(14)}`), q],
            ["an account name covers all its repos", account.includes("Acme/tools#9") && !account.includes("elsewhere/lib#3"), account],
            ["cleared, only your own are left", JSON.stringify(itemsOf(lf)) === JSON.stringify(before), itemsOf(lf)],
          ];
        } finally {
          Services.prefs.clearUserPref(PREF);
        }
        """
    )
    shot("review-in")
    return checks_from(r)


@test
def refreshes_when_zen_comes_to_the_front(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const GAP = "uc.pr-folder.min-gap-seconds";
        Services.prefs.setIntPref(GAP, 1);
        try {
          await pastTheGap(lf);
          let n = pullRequests().length;
          window.dispatchEvent(new Event("activate"));
          await waitFor(() => pullRequests().length > n, "a refresh when the window comes to the front");
          const reason = PrFolder().lastRefresh?.reason;
          await waitFor(() => !PrFolder().lastRefresh || Date.now() - lf.state.lastFetched < 1000, "the fetch to finish");
          n = pullRequests().length;
          window.dispatchEvent(new Event("activate"));
          await sleep(400);
          const tooSoon = pullRequests().length - n;
          await pastTheGap(lf);
          n = pullRequests().length;
          window.dispatchEvent(new Event("activate"));
          await waitFor(() => pullRequests().length > n, "a refresh once the gap has passed");
          return [
            ["coming to the front refreshes the folder", reason === "focus", reason],
            ["not again within the gap", tooSoon === 0, tooSoon],
            ["again once the gap has passed", pullRequests().length > n],
          ];
        } finally {
          Services.prefs.clearUserPref(GAP);
        }
        """
    )
    return checks_from(r)


@test
def refreshes_on_switching_to_the_folders_space(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const GAP = "uc.pr-folder.min-gap-seconds";
        Services.prefs.setIntPref(GAP, 1);
        try {
          const listening = (gZenWorkspaces._changeListeners ?? []).some(l => l.func === PF().onSpaceChange);
          await pastTheGap(lf);
          let n = pullRequests().length;
          await PF().onSpaceChange({ workspace: { uuid: "{another-space}" } });
          await sleep(300);
          const forOtherSpace = pullRequests().length - n;
          await PF().onSpaceChange({ workspace: gZenWorkspaces.getActiveWorkspace() });
          await waitFor(() => pullRequests().length > n, "a refresh for the folder's space");
          return [
            ["the script listens to space switches", listening],
            ["switching to another space leaves the folder alone", forOtherSpace === 0, forOtherSpace],
            ["switching to the folder's space refreshes it", PrFolder().lastRefresh?.reason === "space", PrFolder().lastRefresh],
          ];
        } finally {
          Services.prefs.clearUserPref(GAP);
        }
        """
    )
    return checks_from(r)


@test
def polls_while_you_use_the_mac(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const GAP = "uc.pr-folder.min-gap-seconds", POLL = "uc.pr-folder.poll-seconds";
        const pf = PrFolder();
        Services.prefs.setIntPref(GAP, 1);
        try {
          pf.idleMs = () => 0;
          Services.prefs.setIntPref(POLL, 1);
          const n = pullRequests().length;
          await waitFor(() => pullRequests().length > n && pf.lastRefresh?.reason === "poll", "a poll", 6000);
          pf.idleMs = () => 60 * 60 * 1000;
          await sleep(1200);
          const m0 = pullRequests().length;
          await sleep(2500);
          const whileAway = pullRequests().length - m0;
          Services.prefs.setIntPref(POLL, 0);
          pf.idleMs = () => 0;
          await sleep(1200);
          const m1 = pullRequests().length;
          await sleep(2500);
          const turnedOff = pullRequests().length - m1;
          return [
            ["it polls while the Mac is in use", true],
            ["no polling while you are away", whileAway === 0, whileAway],
            ["poll-seconds 0 turns polling off", turnedOff === 0, turnedOff],
          ];
        } finally {
          delete pf.idleMs;
          Services.prefs.clearUserPref(POLL);
          Services.prefs.clearUserPref(GAP);
        }
        """
    )
    return checks_from(r)


@test
def a_pr_you_open_moves_into_the_folder(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        FAKE.prs.push({ repo: "zhuconv/demo", number: 6, title: "Just opened", mine: true, updated: day(0) });
        const tab = await openTab("https://github.com/zhuconv/demo/pull/6");
        await waitFor(() => tab.getAttribute(ITEM) === `${lf.id}:zhuconv/demo#6`, "the opened tab to join the folder");
        await sleep(200);
        return [
          ["opening the PR page asked GitHub again", ["pr-opened", "owed"].includes(PrFolder().lastRefresh?.reason), PrFolder().lastRefresh],
          ["your tab is now the folder's tab for it", tab.group === folderOf(lf) && tab.pinned, snap()],
          ["the folder made no second tab for it", tabsWithItem(lf, "zhuconv/demo#6").length === 1, snap()],
          ["your tab stays selected", tab.selected],
        ];
        """
    )
    shot("adopted")
    return checks_from(r)


@test
def opening_a_pr_the_folder_has_uses_your_tab(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        await selectBlank();
        const placeholder = tabFor(lf, "zhuconv/demo#1");
        const tab = await openTab("https://github.com/zhuconv/demo/pull/1/files");
        await waitFor(() => tab.getAttribute(ITEM) === `${lf.id}:zhuconv/demo#1`, "your tab to take the placeholder's place");
        await waitFor(() => !placeholder.isConnected, "the placeholder to close");
        await sleep(500);  // Zen records a dismissal a moment after the close
        const dismissed = LFM.dismissedItems.has(`${lf.id}:zhuconv/demo#1`);
        await refreshNow(lf);
        return [
          ["your tab is the folder's tab for the PR", tab.group === folderOf(lf) && tab.pinned, snap()],
          ["the folder's own tab for it closed", !placeholder.isConnected],
          ["that doesn't count as dismissing the PR", !dismissed],
          ["one tab for the PR, also after a refresh", tabsWithItem(lf, "zhuconv/demo#1").length === 1, snap()],
        ];
        """
    )
    return checks_from(r)


@test
def a_pr_you_dismissed_stays_out(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        await selectBlank();
        gBrowser.removeTab(tabFor(lf, "zhuconv/demo#2"), { animate: false });
        const dismissed = await waitFor(() => LFM.dismissedItems.has(`${lf.id}:zhuconv/demo#2`), "Zen to record the dismissal")
          .catch(() => false);
        const n = pullRequests().length;
        const tab = await openTab("https://github.com/zhuconv/demo/pull/2");
        await waitFor(() => pullRequests().length > n, "the opened page to ask GitHub again");
        await sleep(300);
        return [
          ["closing its tab dismissed the PR", dismissed],
          ["opening its page leaves it an ordinary tab", !tab.pinned && !tab.group && !tab.hasAttribute(ITEM), snap()],
          ["the folder doesn't bring it back", !itemsOf(lf).includes("zhuconv/demo#2"), itemsOf(lf)],
        ];
        """
    )
    return checks_from(r)


@test
def the_pr_on_screen_stays_until_you_leave_it(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        const tab = tabFor(lf, "Acme/app#3");
        gBrowser.selectedTab = tab;
        await sleep(100);
        FAKE.prs = FAKE.prs.filter(pr => pr.number !== 3);   // merged
        await refreshNow(lf);
        const stayed = tab.isConnected && tabFor(lf, "Acme/app#3") === tab;
        await selectBlank();
        await waitFor(() => !tab.isConnected, "the merged PR's tab to go once you leave it");
        await sleep(500);  // Zen records a dismissal a moment after the close
        return [
          ["merged while on screen, it stays", stayed, snap()],
          ["leaving it refreshed the folder", PrFolder().lastRefresh?.reason === "left-item", PrFolder().lastRefresh],
          ["then it goes", !itemsOf(lf).includes("Acme/app#3"), itemsOf(lf)],
          ["and doesn't count as dismissed", !LFM.dismissedItems.has(`${lf.id}:Acme/app#3`)],
        ];
        """
    )
    return checks_from(r)


@test
def issues_folders_stay_as_zen_makes_them(m, shot):
    r = m.run(
        r"""
        const n = FAKE.requests.length;
        const id = await LFM.createFolder("github:issues");
        const lf = LFM.getFolder(id);
        await waitFor(() => FAKE.requests.slice(n).some(r => r.path.startsWith("/issues")), "the issues folder's request");
        const q = FAKE.requests.slice(n).find(r => r.path.startsWith("/issues")).q;
        const options = { ...lf.state.options };
        LFM.deleteFolder(id);
        return [
          ["no time window on an issues search", !q.includes("updated:"), q],
          ["no filters ticked for it", options.authorMe === undefined && options.reviewRequested === undefined, options],
        ];
        """
    )
    return checks_from(r)


@test
def unloading_puts_zen_back(m, shot):
    r = m.run(
        r"""
        const lf = prLiveFolder();
        PF().destroy();
        const proto = Provider().prototype;
        const unwrapped = !proto.fetchItems[WRAPPED] && !proto.parsePullRequests[WRAPPED]
          && !LFM.onLiveFolderFetch[WRAPPED] && !Object.hasOwn(LFM, "onLiveFolderFetch");
        await sleep(1100);
        const n = pullRequests().length;
        window.dispatchEvent(new Event("activate"));
        await sleep(500);
        const afterFocus = pullRequests().length - n;
        await lf.refresh();
        return [
          ["Zen's own methods are back", unwrapped],
          ["coming to the front no longer refreshes", afterFocus === 0, afterFocus],
          ["Zen's search has no time window again", !lastQuery().includes("updated:"), lastQuery()],
          ["no instance left on the window", !PF()],
        ];
        """
    )
    return checks_from(r)


# --- Runner -------------------------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_prefs(profile, port):
    prefs = {
        "marionette.port": port,
        "zen.welcome-screen.seen": True,
        "zen.urlbar.open-on-startup": False,
        "zen.updates.show-update-notification": False,
        "zen.live-folders.promotion.shown": True,
        "browser.shell.checkDefaultBrowser": False,
        "devtools.console.stdout.chrome": True,
        "app.update.disabledForTesting": True,
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
        help="copy utils/, JS/pr-folder.uc.js and JS/pr-folder/ from this profile chrome "
        "folder into the test profile and let fx-autoconfig (installed in Zen.app) load the script",
    )
    args = parser.parse_args()

    out = HERE / "output"
    out.mkdir(exist_ok=True)
    profile = tempfile.mkdtemp(prefix="arc-like-pr-folder-test-")
    port = free_port()
    write_prefs(profile, port)
    if args.via_loader:
        source = Path(args.via_loader).expanduser()
        target = Path(profile) / "chrome"
        shutil.copytree(source / "utils", target / "utils")
        (target / "JS").mkdir(parents=True)
        shutil.copy2(source / "JS" / SCRIPT.name, target / "JS" / SCRIPT.name)
        shutil.copytree(source / "JS" / "pr-folder", target / "JS" / "pr-folder")

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
            m.send("WebDriver:SetTimeouts", {"script": 90000})
            m.send("Marionette:SetContext", {"value": "chrome"})
            # The test's own chrome:// address for the mod; registered before
            # COMMON_JS (which imports the module through it) first runs.
            manifest = Path(profile) / "prfoldertest.manifest"
            manifest.write_text(f"content prfoldertest {MOD.as_uri()}/\n")
            m.send(
                "WebDriver:ExecuteAsyncScript",
                {"script": "const done = arguments[arguments.length - 1];\n(async () => {\n" + REGISTER_JS + "\n})().then(done);", "args": [str(manifest)]},
            )
            info = m.run(
                "await window.delayedStartupPromise; await gZenWorkspaces.promiseInitialized;"
                " return { version: Services.appinfo.version,"
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

    errors = [l for l in log_path.read_text(errors="replace").splitlines() if "[pr-folder]" in l]
    if errors:
        print("\nscript messages in the browser log:")
        print("\n".join("  " + l for l in errors[:20]))
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}; log in {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
