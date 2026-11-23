#!/usr/bin/env python3
"""End-to-end test for tab-autofill.uc.js against the Zen you have installed.

Starts Zen headless with a throwaway profile (your own profile is never
touched), drives it over Marionette, seeds a little fake history, loads the
script and checks the Tab and favicon behavior. Screenshots of the URL bar go
to test/output/.

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
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "tab-autofill.uc.js"
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
const u = window.gURLBar;
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
  Tab: ["Tab", KeyEvent.DOM_VK_TAB],
  Shift: ["ShiftLeft", KeyEvent.DOM_VK_SHIFT],
  ArrowDown: ["ArrowDown", KeyEvent.DOM_VK_DOWN],
  ArrowRight: ["ArrowRight", KeyEvent.DOM_VK_RIGHT],
  Escape: ["Escape", KeyEvent.DOM_VK_ESCAPE],
  Backspace: ["Backspace", KeyEvent.DOM_VK_BACK_SPACE],
};
function keyEvent(key) {
  if (KEYS[key]) return new KeyboardEvent("", { key, code: KEYS[key][0], keyCode: KEYS[key][1] });
  return new KeyboardEvent("", { key });
}
function press(key, { shift = false } = {}) {
  const tip = Cc["@mozilla.org/text-input-processor;1"].createInstance(Ci.nsITextInputProcessor);
  if (!tip.beginInputTransactionForTests(window)) throw new Error("TextInputProcessor unavailable");
  if (shift) tip.keydown(keyEvent("Shift"));
  const e = keyEvent(key);
  tip.keydown(e);
  tip.keyup(e);
  if (shift) tip.keyup(keyEvent("Shift"));
}
function type(text) { for (const ch of text) press(ch); }
const settle = () => sleep(200);
function snap() {
  const i = u.inputField;
  const box = document.getElementById("identity-box");
  const icon = document.getElementById("identity-icon");
  const r = u._resultForCurrentValue;
  return {
    value: u.value,
    sel: [i.selectionStart, i.selectionEnd],
    viewOpen: u.view.isOpen,
    selectedRow: u.view.selectedRowIndex,
    rows: u.view.visibleRowCount,
    pendingAutofill: !!u._autofillPlaceholder,
    favicon: box.hasAttribute("tab-autofill-favicon"),
    icon: icon.style.getPropertyValue("list-style-image"),
    computedIcon: getComputedStyle(icon).listStyleImage,
    result: r ? { type: r.type, heuristic: !!r.heuristic, autofill: !!r.autofill, url: r.payload?.url ?? null } : null,
  };
}
// After a step closes the URL bar (Escape, a picked result), Zen can reset the
// URL bar a little later. Wait until its value stops changing.
async function quiesce() {
  let last = u.value;
  let stableSince = Date.now();
  const start = Date.now();
  while (Date.now() - stableSince < 500 && Date.now() - start < 5000) {
    await sleep(50);
    if (u.value !== last) {
      last = u.value;
      stableSince = Date.now();
    }
  }
}
// Clear the URL bar the way a person would: select all, Backspace.
async function clearUrlbar() {
  await quiesce();
  u.focus();
  await waitFor(() => u.focused, "URL bar focus");
  if (u.value) {
    u.select();
    press("Backspace");
    await waitFor(() => u.value === "", "empty URL bar");
  }
  await settle();
}
// Types into an empty URL bar and waits for results. If a late reset from Zen
// wipes the text, starts over (up to three tries).
async function startTyping(text) {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await clearUrlbar();
      type(text);
      await waitFor(() => u.view.isOpen && u._resultForCurrentValue, "results for " + JSON.stringify(text));
      await settle();
      if (u.value.toLowerCase().startsWith(text.toLowerCase())) return;
      lastError = new Error("URL bar was reset after typing " + JSON.stringify(text) + "; state: " + JSON.stringify(snap()));
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError;
}
async function typeAndWaitValue(text, expected) {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      await startTyping(text);
      await waitFor(() => u.value === expected, "value " + JSON.stringify(expected), 4000);
      await settle();
      if (u.value === expected) return;
      lastError = new Error("value " + JSON.stringify(expected) + " did not stay; state: " + JSON.stringify(snap()));
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError;
}
"""


# --- Test data ----------------------------------------------------------------


def solid_png(rgb, size=16):
    """A size x size PNG of one color, as a data: URL (a fake favicon)."""
    row = b"\x00" + bytes(rgb) * size
    raw = row * size

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


SEED_JS = r"""
const [icons] = arguments;
const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
const now = Date.now();
const visits = n => Array.from({ length: n }, (_, k) => ({
  date: new Date(now - k * 3600 * 1000),
  transition: PlacesUtils.history.TRANSITIONS.TYPED,
}));
await PlacesUtils.history.insertMany([
  { url: "https://www.reddit.com/", title: "Reddit", visits: visits(10) },
  { url: "https://www.reddit.com/r/zen_browser/", title: "r/zen_browser", visits: visits(5) },
  { url: "https://github.com/", title: "GitHub", visits: visits(10) },
  { url: "https://github.com/zen-browser/desktop", title: "zen-browser/desktop", visits: visits(5) },
]);
const recalculator = Cc["@mozilla.org/places/frecency-recalculator;1"].getService(Ci.nsIObserver).wrappedJSObject;
await recalculator.recalculateAnyOutdatedFrecencies();
const iconErrors = [];
for (const [page, data] of Object.entries(icons)) {
  try {
    await PlacesUtils.favicons.setFaviconForPage(
      Services.io.newURI(page),
      Services.io.newURI(new URL("/favicon.ico", page).href),
      Services.io.newURI(data));
  } catch (e) {
    iconErrors.push(page + ": " + e);
  }
}
return { iconErrors };
"""

# Sine loads mod scripts from a chrome:// URL; register the mod folder the same
# way (the subscript loader refuses plain file:// URLs).
REGISTER_JS = r"""
const [manifestPath] = arguments;
const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
file.initWithPath(manifestPath);
Components.manager.QueryInterface(Ci.nsIComponentRegistrar).autoRegister(file);
return true;
"""
SCRIPT_URL = "chrome://tabautofilltest/content/" + SCRIPT.name

LOAD_JS = r"""
const [url] = arguments;
Services.scriptloader.loadSubScriptWithOptions(url, { target: window, ignoreCache: true });
await waitFor(() => Object.hasOwn(u, "setResultForCurrentValue"), "script init");
return !!window[Symbol.for("arc-like-cmdT")];
"""


LOADED_BY_LOADER_JS = r"""
await waitFor(() => window[Symbol.for("arc-like-cmdT")] && Object.hasOwn(u, "setResultForCurrentValue"),
  "the fx-autoconfig loader to start the script", 20000);
return true;
"""


# --- Test cases ---------------------------------------------------------------

REDDIT_ICON = "page-icon:https://www.reddit.com/"
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


@test
def inline_completion_shows_favicon(m, shots):
    s = m.run('await typeAndWaitValue("red", "reddit.com/"); return snap();')
    shots("1-typed-red", "Typed 'red': completion pending, favicon shown")
    return [
        ("value is the completion", s["value"] == "reddit.com/", s),
        ("completed part is selected", s["sel"] == [3, 11], s),
        ("favicon replaces the search glass", s["favicon"] and REDDIT_ICON in s["icon"], s),
    ]


@test
def tab_accepts_completion(m, shots):
    s = m.run('await typeAndWaitValue("red", "reddit.com/"); press("Tab"); await settle(); return snap();')
    shots("2-after-tab", "After Tab: completion accepted, caret at end")
    return [
        ("value kept", s["value"] == "reddit.com/", s),
        ("caret at the end, nothing selected", s["sel"] == [11, 11], s),
        ("completion confirmed as typed text", not s["pendingAutofill"], s),
        ("results stay open on the same row", s["viewOpen"] and s["selectedRow"] == 0, s),
        ("Enter would open the completed site", (s["result"] or {}).get("url") == "https://www.reddit.com/", s),
        ("favicon still shown", s["favicon"] and REDDIT_ICON in s["icon"], s),
    ]


@test
def keep_typing_after_tab(m, shots):
    s = m.run(
        'await typeAndWaitValue("red", "reddit.com/"); press("Tab"); await settle();'
        'type("r/zen"); await waitFor(() => u.value.startsWith("reddit.com/r/zen") && u._resultForCurrentValue, "path");'
        'await settle(); const a = snap(); press("Tab"); await settle(); return [a, snap()];'
    )
    a, b = s
    shots("3-path-accepted", "Typed 'r/zen' after Tab, then Tab again")
    return [
        ("path completes to the next slash", a["value"] == "reddit.com/r/zen_browser/" and a["sel"] == [16, 25], a),
        ("second Tab accepts the path completion", b["value"] == "reddit.com/r/zen_browser/" and b["sel"] == [25, 25], b),
        ("favicon follows the page", b["favicon"] and "reddit.com/r/zen_browser" in b["icon"], b),
    ]


@test
def path_completion_on_another_site(m, shots):
    s = m.run(
        'await typeAndWaitValue("github.com/z", "github.com/zen-browser/"); const a = snap();'
        'press("Tab"); await settle(); return [a, snap()];'
    )
    a, b = s
    return [
        ("github.com/z completes to github.com/zen-browser/", a["sel"] == [12, 23], a),
        ("Tab accepts it", b["value"] == "github.com/zen-browser/" and b["sel"] == [23, 23], b),
    ]


@test
def second_tab_moves_into_results(m, shots):
    s = m.run(
        'await typeAndWaitValue("red", "reddit.com/"); press("Tab"); await settle();'
        'press("Tab"); await settle(); return snap();'
    )
    return [("Tab after accepting selects the next row", s["selectedRow"] == 1, s)]


@test
def tab_without_completion_is_unchanged(m, shots):
    s = m.run(
        'await startTyping("zen"); await waitFor(() => u.view.visibleRowCount >= 2, "rows");'
        'await settle(); const a = snap(); press("Tab"); await settle(); return [a, snap()];'
    )
    a, b = s
    shots("4-search-text", "Typed 'zen' then Tab: search text, normal Tab")
    return [
        ("no completion for 'zen'", a["value"] == "zen" and not a["pendingAutofill"], a),
        ("search text keeps the search glass", not a["favicon"], a),
        ("Tab selects the next row as before", b["selectedRow"] == 1, b),
        ("a history row shows its favicon", b["favicon"], b),
    ]


@test
def shift_tab_is_unchanged(m, shots):
    s = m.run('await typeAndWaitValue("red", "reddit.com/"); press("Tab", { shift: true }); await settle(); return snap();')
    return [("Shift+Tab is not intercepted", s["selectedRow"] != 0, s)]


@test
def arrow_down_shows_row_favicon(m, shots):
    s = m.run('await typeAndWaitValue("red", "reddit.com/"); press("ArrowDown"); await settle(); return snap();')
    return [
        ("ArrowDown selects a history row", s["selectedRow"] == 1, s),
        (
            "icon is that row's favicon",
            s["favicon"] and s["result"] and s["result"]["url"] and s["result"]["url"] in s["icon"],
            s,
        ),
    ]


@test
def escape_restores_icon(m, shots):
    s = m.run('await typeAndWaitValue("red", "reddit.com/"); press("Escape"); await settle(); return snap();')
    return [("closing the results restores the icon", not s["viewOpen"] and not s["favicon"] and not s["icon"], s)]


@test
def pref_tab_accepts_off(m, shots):
    s = m.run(
        'Services.prefs.setBoolPref("uc.tab-autofill.tab-accepts", false);'
        'try { await typeAndWaitValue("red", "reddit.com/"); press("Tab"); await settle(); return snap(); }'
        'finally { Services.prefs.clearUserPref("uc.tab-autofill.tab-accepts"); }'
    )
    return [("with the pref off, Tab moves to the next row", s["selectedRow"] == 1, s)]


@test
def pref_favicon_after_accept(m, shots):
    s = m.run(
        'Services.prefs.setBoolPref("uc.tab-autofill.favicon-after-accept", true);'
        'try { await typeAndWaitValue("red", "reddit.com/"); const a = snap();'
        'press("Tab"); await settle(); return [a, snap()]; }'
        'finally { Services.prefs.clearUserPref("uc.tab-autofill.favicon-after-accept"); }'
    )
    a, b = s
    return [
        ("no favicon while the completion is pending", not a["favicon"], a),
        ("favicon once accepted", b["favicon"] and REDDIT_ICON in b["icon"], b),
    ]


@test
def pref_favicon_off(m, shots):
    s = m.run(
        'Services.prefs.setBoolPref("uc.tab-autofill.favicon", false);'
        'try { await typeAndWaitValue("red", "reddit.com/"); return snap(); }'
        'finally { Services.prefs.clearUserPref("uc.tab-autofill.favicon"); }'
    )
    return [("with the pref off, no favicon", not s["favicon"] and not s["icon"], s)]


@test
def unload_restores_everything(m, shots):
    s = m.run(
        'await typeAndWaitValue("red", "reddit.com/"); const before = snap();'
        'window[Symbol.for("arc-like-cmdT")].destroy(); await settle(); const after = snap();'
        'press("Tab"); await settle(); const tab = snap();'
        'return { before, after, tab, ownSetter: Object.hasOwn(u, "setResultForCurrentValue"),'
        ' instance: !!window[Symbol.for("arc-like-cmdT")] };'
    )
    return [
        ("favicon shown before unloading", s["before"]["favicon"], s["before"]),
        ("unloading removes the favicon", not s["after"]["favicon"] and not s["after"]["icon"], s["after"]),
        ("unloading restores the URL bar method", not s["ownSetter"], s),
        ("unloading clears the instance", not s["instance"], s),
        ("Tab is back to Zen's behavior", s["tab"]["selectedRow"] == 1, s["tab"]),
    ]


@test
def loads_again_after_unload(m, shots):
    loaded = m.run(LOAD_JS, SCRIPT_URL)
    m.run(LOAD_JS, SCRIPT_URL)  # a second load replaces the first
    s = m.run('await typeAndWaitValue("red", "reddit.com/"); press("Tab"); await settle(); return snap();')
    return [
        ("script loads again", loaded, loaded),
        ("Tab accepts after loading twice", s["sel"] == [11, 11] and s["selectedRow"] == 0, s),
    ]


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
        "browser.shell.checkDefaultBrowser": False,
        "browser.search.suggest.enabled": False,
        "browser.urlbar.suggest.searches": False,
        "browser.urlbar.suggest.quicksuggest.sponsored": False,
        "browser.urlbar.suggest.quicksuggest.nonsponsored": False,
        "browser.urlbar.suggest.trending": False,
        "browser.urlbar.suggest.topsites": False,
        "devtools.console.stdout.chrome": True,
        "app.update.disabledForTesting": True,
    }
    lines = [f'user_pref("{k}", {json.dumps(v)});' for k, v in prefs.items()]
    (Path(profile) / "user.js").write_text("\n".join(lines) + "\n")


def run_tests(m, out, only):
    """Runs the test cases; returns the number of failed checks."""

    def shots_for(test_name):
        def shot(name, caption):
            try:
                el = m.send("WebDriver:FindElement", {"using": "css selector", "value": "#urlbar"})["value"]
                el_id = next(iter(el.values()))
                png = m.send("WebDriver:TakeScreenshot", {"id": el_id, "full": False, "hash": False, "scroll": False})
                (out / f"{name}.png").write_bytes(base64.b64decode(png["value"]))
            except Exception as e:  # screenshots are a bonus, not a test
                print(f"  (screenshot {name} failed: {str(e).splitlines()[0]})")

        return shot

    failures = 0
    for fn in TESTS:
        if only and only not in fn.__name__:
            continue
        print(f"\n{fn.__name__}")
        try:
            checks = fn(m, shots_for(fn.__name__))
        except Exception as e:
            failures += 1
            print(f"  ERROR {e}")
            continue
        for label, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'} {label}")
            if not ok:
                failures += 1
                print(f"       state: {json.dumps(detail)}")
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
        help="copy utils/ and JS/tab-autofill.uc.js from this profile chrome folder into the "
        "test profile and let fx-autoconfig (installed in Zen.app) load the script",
    )
    args = parser.parse_args()

    out = HERE / "output"
    out.mkdir(exist_ok=True)
    profile = tempfile.mkdtemp(prefix="arc-like-cmdT-test-")
    port = free_port()
    write_prefs(profile, port)
    if args.via_loader:
        source = Path(args.via_loader).expanduser()
        target = Path(profile) / "chrome"
        shutil.copytree(source / "utils", target / "utils")
        (target / "JS").mkdir(parents=True)
        shutil.copy2(source / "JS" / SCRIPT.name, target / "JS" / SCRIPT.name)

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
            info = m.run(
                "await window.delayedStartupPromise; return { version: Services.appinfo.version,"
                " app: Services.appinfo.name + ' ' + (globalThis.AppConstants?.MOZ_APP_VERSION_DISPLAY ?? '') };"
            )
            print(f"{info['app']} (Firefox {info['version']}), profile {profile}")

            icons = {"https://www.reddit.com/": solid_png((255, 69, 0)), "https://github.com/": solid_png((36, 41, 47))}
            seeded = m.run(SEED_JS, icons)
            if seeded["iconErrors"]:
                print("note: could not set test favicons:", seeded["iconErrors"])
            manifest = Path(profile) / "tabautofilltest.manifest"
            manifest.write_text(f"content tabautofilltest {SCRIPT.parent.as_uri()}/\n")
            m.run(REGISTER_JS, str(manifest))
            if args.via_loader:
                print("script started by the fx-autoconfig loader")
                m.run(LOADED_BY_LOADER_JS)
            elif not m.run(LOAD_JS, SCRIPT_URL):
                raise RuntimeError("script did not initialize")
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

    errors = [l for l in log_path.read_text(errors="replace").splitlines() if "[tab-autofill]" in l]
    if errors:
        print("\nscript messages in the browser log:")
        print("\n".join("  " + l for l in errors[:20]))
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'}; screenshots and log in {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
