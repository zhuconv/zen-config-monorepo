#!/usr/bin/env python3
"""End-to-end test for the agent sidebar against the Zen you have installed.

Starts Zen headless with a throwaway profile (your own profile is never
touched), a local web server for the test pages, and test/mock_agent.py in
place of Codex. Drives Zen over Marionette and checks the shortcut, the panel,
what the agent receives, streaming, stop, new chat, permissions, Markdown
sanitizing and crash recovery.

    python3 test/run_test.py                  # headless, mock agent
    python3 test/run_test.py --headful        # watch it in a visible window
    python3 test/run_test.py --via-loader PROFILE/chrome
                                              # let the installed fx-autoconfig
                                              # loader start the script
    python3 test/run_test.py --real CONFIG    # finish with one real question to
                                              # the agent in CONFIG (uses quota)

Standard library only.
"""

import argparse
import base64
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_ZEN = "/Applications/Zen.app/Contents/MacOS/zen"
PANEL = "viewAgentSidebar"


# --- Marionette -------------------------------------------------------------


class Marionette:
    """Minimal Marionette client: length-prefixed JSON over TCP."""

    def __init__(self, port, timeout=60):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=180)
                break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("Zen did not open the Marionette port")
                time.sleep(0.5)
        self.msg_id = 0
        self.context = None
        self._recv()

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
                    raise RuntimeError(f"{command}: {error.get('error')}: {error.get('message')}")
                return msg[3]

    def set_context(self, context):
        if self.context != context:
            self.send("Marionette:SetContext", {"value": context})
            self.context = context

    def chrome(self, body, *args):
        """Run an async script in the browser window; `return` gives the value."""
        self.set_context("chrome")
        script = (
            "const done = arguments[arguments.length - 1];\n"
            + CHROME_HELPERS
            + "\n(async () => {\n" + body + "\n})().then(done, e => done({__error: String(e) + '\\n' + (e && e.stack)}));"
        )
        result = self.send("WebDriver:ExecuteAsyncScript", {"script": script, "args": list(args)})["value"]
        if isinstance(result, dict) and "__error" in result:
            raise RuntimeError("chrome script failed: " + result["__error"])
        return result

    def content(self, script, *args):
        self.set_context("content")
        return self.send("WebDriver:ExecuteScript", {"script": script, "args": list(args)})["value"]


CHROME_HELPERS = r"""
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(fn, what, timeout = 10000) {
  const start = Date.now();
  for (;;) {
    let v;
    try { v = await fn(); } catch (e) {}
    if (v) return v;
    if (Date.now() - start > timeout) throw new Error("timed out waiting for " + what);
    await sleep(50);
  }
}
const glue = () => window.ucAgentSidebar;
const panelDoc = () => SidebarController.browser?.contentDocument;
const conv = () => glue().conversationFor(gBrowser.selectedTab);
function panelOpen() {
  return SidebarController.isOpen && SidebarController.currentID === "viewAgentSidebar" && !!glue()?.panel;
}
function panelState() {
  const doc = panelDoc();
  if (!doc || !glue()?.panel) return null;
  const q = s => doc.querySelector(s);
  return {
    chipHidden: q("#chip").hidden,
    chip: q("#chip-text").textContent,
    inputFocused: doc.activeElement === q("#input") && doc.hasFocus(),
    messages: [...doc.querySelectorAll("#messages .msg")].map(e => ({ role: e.className.replace("msg ", ""), text: e.textContent, state: e.dataset.state || e.dataset.status || "" })),
    status: q("#status").textContent,
    harness: q("#harness").value,
    harnesses: [...q("#harness").options].map(o => o.textContent),
    model: q("#model").hidden ? null : q("#model").value,
    effort: q("#effort").hidden ? null : q("#effort").value,
    emptyHidden: q("#empty").hidden,
  };
}
async function choose(id, value) {
  const doc = panelDoc();
  const select = doc.getElementById(id);
  select.value = value;
  select.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
  await sleep(300);
  return panelState();
}
async function ask(question) {
  const doc = panelDoc();
  const input = doc.getElementById("input");
  input.value = question;
  input.dispatchEvent(new doc.defaultView.Event("input"));
  input.dispatchEvent(new doc.defaultView.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await waitFor(() => !conv().busy, "the answer to " + JSON.stringify(question), 20000);
  await sleep(150);
  return panelState();
}
"""


# --- Test pages ---------------------------------------------------------------


def make_pdf(path, pages):
    """A plain PDF whose pages hold the given lines of text."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        stream = "BT /F1 14 Tf 50 740 Td 18 TL " + " ".join(
            f"({line}) Tj T*" for line in lines
        ) + " ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory):
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# --- Helpers ------------------------------------------------------------------


class Agent:
    """Reads what the mock agent logged."""

    def __init__(self, log):
        self.log = log

    def messages(self, method=None):
        if not self.log.exists():
            return []
        out = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [m for m in out if method is None or m.get("method") == method]

    def last_prompt(self):
        prompts = self.messages("session/prompt")
        return prompts[-1]["params"] if prompts else None


def open_tab(m, url):
    m.set_context("content")
    handle = m.send("WebDriver:NewWindow", {"type": "tab", "focus": True})["handle"]
    m.send("WebDriver:SwitchToWindow", {"handle": handle, "focus": True})
    m.send("WebDriver:Navigate", {"url": url})
    return handle


def switch_tab(m, handle):
    m.set_context("content")
    m.send("WebDriver:SwitchToWindow", {"handle": handle, "focus": True})


def press_shortcut(m):
    """Cmd+Shift+A as real key events in the browser window."""
    m.set_context("chrome")
    keys = ["", "", "a"]
    actions = [{"type": "keyDown", "value": k} for k in keys] + [{"type": "keyUp", "value": k} for k in reversed(keys)]
    m.send("WebDriver:PerformActions", {"actions": [{"type": "key", "id": "keyboard", "actions": actions}]})
    m.send("WebDriver:ReleaseActions", {})


SELECT_ELEMENT = r"""
const [selector, text] = arguments;
const el = [...document.querySelectorAll(selector)].find(e => !text || e.textContent.includes(text));
if (!el) return null;
const range = document.createRange();
range.selectNodeContents(el);
const sel = window.getSelection();
sel.removeAllRanges();
sel.addRange(range);
return sel.toString().trim();
"""


def check(label, ok, detail=None):
    return (label, bool(ok), detail)


def agent_processes(root):
    """Agent processes (node, codex, claude) below the browser process."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
    children = {}
    for line in out.splitlines():
        pid, ppid, comm = line.split(None, 2)
        children.setdefault(int(ppid), []).append((int(pid), comm.strip()))
    found, todo = [], [root]
    while todo:
        for pid, comm in children.get(todo.pop(), []):
            todo.append(pid)
            if re.search(r"(^|/)(node|codex|claude)[^/]*$", comm):
                found.append(f"{pid} {comm}")
    return found


# --- Tests ----------------------------------------------------------------------


def run_tests(m, ctx):
    agent, web = ctx["agent"], ctx["web"]
    results = []

    def record(name, checks):
        print(f"\n{name}")
        for label, ok, detail in checks:
            print(f"  {'PASS' if ok else 'FAIL'} {label}")
            if not ok and detail is not None:
                print(f"       {json.dumps(detail, ensure_ascii=False)[:1500]}")
            results.append(ok)

    def step(name, fn):
        try:
            record(name, fn())
        except Exception as e:
            print(f"\n{name}\n  ERROR {e}")
            results.append(False)

    tabs = {}

    def shortcut_opens_panel():
        tabs["article"] = open_tab(m, f"{web}/article.html")
        selected = m.content(SELECT_ELEMENT, "#method", "")
        zen_key = m.chrome("const k = document.getElementById('key_searchTabs'); return k ? k.getAttribute('disabled') : 'missing';")
        press_shortcut(m)
        opened = m.chrome("try { await waitFor(() => panelOpen() && !panelDoc().getElementById('chip').hidden, 'panel', 6000); return true; } catch (e) { return false; }")
        checks = [
            check("Cmd+Shift+A opens the panel", opened),
            check("Zen's own Cmd+Shift+A (search tabs) is switched off", zen_key == "true", zen_key),
        ]
        if not opened:
            # Keep testing the rest through the same command the key runs.
            m.chrome("document.getElementById('cmd_agentSidebarToggle').doCommand(); await waitFor(() => panelOpen() && !panelDoc().getElementById('chip').hidden, 'panel', 6000); return true;")
        state = m.chrome("return panelState();")
        layout = m.chrome(r"""
const box = document.getElementById('sidebar-box').getBoundingClientRect();
const content = gBrowser.selectedBrowser.getBoundingClientRect();
return { boxX: Math.round(box.x), boxW: Math.round(box.width), contentX: Math.round(content.x), contentW: Math.round(content.width),
         placed: Services.prefs.getBoolPref('uc.agent-sidebar.placed-right', false),
         sidebarHeader: getComputedStyle(document.getElementById('sidebar-header')).display };
""")
        read_ahead = m.chrome("try { await waitFor(() => conv().pageText, 'the read-ahead', 3000); return conv().pageText.url; } catch (e) { return null; }")
        checks += [
            check("the page is read while the user types", read_ahead == f"{web}/article.html", read_ahead),
            check("selection shows as a chip", "Offline, for a given model" in state["chip"] and not state["chipHidden"], state),
            check("chip holds the whole selection", selected and state["chip"].strip() == selected, {"selected": selected, "chip": state["chip"]}),
            check("the question box has focus", state["inputFocused"], state),
            check("panel sits right of the page", layout["boxX"] >= layout["contentX"] + layout["contentW"] - 2, layout),
            check("panel opens at least 400px wide", layout["boxW"] >= 400, layout),
            check("Zen's own sidebar header is hidden for the panel", layout["sidebarHeader"] == "none", layout),
        ]
        return checks

    def question_carries_page_and_selection():
        state = m.chrome("return await ask('这是什么意思');")
        prompt = agent.last_prompt() or {}
        blocks = prompt.get("prompt", [])
        resource = next((b["resource"] for b in blocks if b.get("type") == "resource"), {})
        text = next((b["text"] for b in blocks if b.get("type") == "text"), "")
        new_session = (agent.messages("session/new") or [{}])[-1].get("params", {})
        system = json.dumps(new_session.get("systemPrompt"), ensure_ascii=False)
        last = state["messages"][-1] if state["messages"] else {}
        return [
            check("page text is attached as a resource", "MARKER-ARTICLE-BODY" in resource.get("text", ""), resource.get("text", "")[:300]),
            check("resource URI is the page URL", resource.get("uri") == f"{web}/article.html", resource.get("uri")),
            check("Readability dropped the site menu", "Home · Papers" not in resource.get("text", ""), resource.get("text", "")[:200]),
            check("selection is quoted in the prompt", "<selection>" in text and "Offline, for a given model" in text, text[:300]),
            check("question follows the selection", text.rstrip().endswith("这是什么意思"), text[-80:]),
            check("session gets the reading-assistant system prompt", "reading assistant" in system, system[:200]),
            check("answer streams into the panel", last.get("role") == "assistant" and "Echo: 这是什么意思 | resources=1" in last.get("text", ""), last),
            check("user message notes the attached page", any(x["role"] == "user" and "Page attached" in x["text"] for x in state["messages"]), state["messages"]),
            check("chip clears after sending", state["chipHidden"], state),
            check("the read-ahead text is dropped once sent", m.chrome("return conv().pageText === null;")),
            check("header offers both agents", state["harnesses"] == ["Codex", "Claude Code"] and state["harness"] == "codex", state),
            check("header shows the session's model and effort", state["model"] == "mock-model" and state["effort"] == "low", state),
        ]

    def follow_up_skips_page_text():
        before = agent.last_prompt()
        state = m.chrome("return await ask('and the method?');")
        prompt = agent.last_prompt()
        blocks = prompt["prompt"]
        return [
            check("follow-up sends no page text again", not any(b.get("type") == "resource" for b in blocks), blocks),
            check("follow-up stays in the same session", prompt["sessionId"] == before["sessionId"], [before["sessionId"], prompt["sessionId"]]),
            check("follow-up has no selection", "<selection>" not in blocks[-1]["text"], blocks[-1]["text"]),
            check("conversation shows both turns", len(state["messages"]) == 4, state["messages"]),
        ]

    def pdf_selection_and_text():
        tabs["pdf"] = open_tab(m, f"{web}/paper.pdf")
        selected = None
        deadline = time.time() + 20
        while time.time() < deadline and not selected:
            selected = m.content(SELECT_ELEMENT, ".textLayer span", "MARKER-PDF-PAGE-ONE")
            if not selected:
                time.sleep(0.3)
        press_shortcut(m)
        m.chrome("await waitFor(() => panelOpen() && !panelDoc().getElementById('chip').hidden, 'chip', 6000); return true;")
        chip = m.chrome("return panelState().chip;")
        state = m.chrome("return await ask('what is this?');")
        prompt = agent.last_prompt()
        resource = next((b["resource"] for b in prompt["prompt"] if b.get("type") == "resource"), {})
        text = prompt["prompt"][-1]["text"]
        return [
            check("PDF text layer selection reaches the chip", selected and "MARKER-PDF-PAGE-ONE" in chip, {"selected": selected, "chip": chip}),
            check("PDF text is attached, page by page", "[page 1]" in resource.get("text", "") and "MARKER-PDF-PAGE-TWO" in resource.get("text", ""), resource.get("text", "")[:400]),
            check("PDF resource says how many pages", "pages 1-2 of 2" in resource.get("text", ""), resource.get("text", "")[:200]),
            check("PDF selection is quoted", "MARKER-PDF-PAGE-ONE" in text, text[:200]),
            check("PDF tab has its own session", prompt["sessionId"] != agent.messages("session/prompt")[0]["params"]["sessionId"], prompt["sessionId"]),
            check("PDF answer shows", "Echo: what is this?" in state["messages"][-1]["text"], state["messages"]),
            check("user message says PDF attached", any("PDF attached" in x["text"] for x in state["messages"]), state["messages"]),
        ]

    def tabs_keep_their_conversations():
        switch_tab(m, tabs["article"])
        article = m.chrome("await sleep(300); return panelState().messages;")
        switch_tab(m, tabs["pdf"])
        pdf = m.chrome("await sleep(300); return panelState().messages;")
        return [
            check("article tab shows its 4 messages", len(article) == 4 and "这是什么意思" in article[0]["text"], article),
            check("PDF tab shows its own 2 messages", len(pdf) == 2 and "what is this?" in pdf[0]["text"], pdf),
        ]

    def stop_cancels_the_turn():
        state = m.chrome(r"""
const doc = panelDoc();
const input = doc.getElementById('input');
input.value = 'SLOW please';
input.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await waitFor(() => (conv().current?.text || '').includes('word2'), 'streaming', 8000);
const stopVisible = !doc.getElementById('stop').hidden;
doc.getElementById('stop').click();
await waitFor(() => !conv().busy, 'stopped', 8000);
await sleep(150);
return { stopVisible, state: panelState() };
""")
        last = state["state"]["messages"][-1]
        return [
            check("Stop shows while answering", state["stopVisible"]),
            check("Stop sends session/cancel", len(agent.messages("session/cancel")) == 1, agent.messages("session/cancel")),
            check("answer is marked stopped", last["role"] == "assistant" and last["state"] == "stopped" and "word59" not in last["text"], last),
            check("Send comes back after stopping", m.chrome("return !panelDoc().getElementById('send').hidden;")),
        ]

    def new_chat_starts_over():
        sessions_before = len(agent.messages("session/new"))
        m.chrome("panelDoc().getElementById('new-chat').click(); await sleep(200); return true;")
        empty = m.chrome("return panelState();")
        state = m.chrome("return await ask('fresh start');")
        prompt = agent.last_prompt()
        return [
            check("New chat clears the panel", empty["messages"] == [] and not empty["emptyHidden"], empty),
            check("New chat opens a new session", len(agent.messages("session/new")) == sessions_before + 1),
            check("New chat sends the page again", any(b.get("type") == "resource" for b in prompt["prompt"]), prompt["prompt"][0].get("type")),
            check("old session is closed", len(agent.messages("session/close")) >= 1),
            check("answer shows", "Echo: fresh start | resources=1" in state["messages"][-1]["text"], state["messages"]),
        ]

    def permission_prompt():
        result = m.chrome(r"""
const doc = panelDoc();
const input = doc.getElementById('input');
input.value = 'PERMISSION please';
input.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
const card = await waitFor(() => doc.querySelector('.msg.permission'), 'permission card', 8000);
const buttons = [...card.querySelectorAll('button')].map(b => b.textContent);
const cardText = card.textContent;
[...card.querySelectorAll('button')].find(b => b.textContent === 'Allow once').click();
await waitFor(() => !conv().busy, 'answer', 8000);
await sleep(150);
return { buttons, cardText, after: card.textContent, state: panelState() };
""")
        answer = result["state"]["messages"][-1]["text"]
        return [
            check("permission card names the action", "Run `ls`" in result["cardText"] or "Run ls" in result["cardText"], result["cardText"]),
            check("permission card offers the agent's options", result["buttons"] == ["Allow once", "Deny"], result["buttons"]),
            check("chosen option reaches the agent", '"optionId": "allow"' in answer, answer),
            check("card shows the choice afterwards", "Allow once" in result["after"] and not result["after"].endswith("Deny"), result["after"]),
        ]

    def markdown_is_sanitized():
        r = m.chrome(r"""
await ask('MARKDOWN please');
const doc = panelDoc();
const answers = doc.querySelectorAll('.msg.assistant');
const el = answers[answers.length - 1];
const link = el.querySelector('a');
const before = new Set(gBrowser.tabs);
link?.click();
let newTab = null;
try { newTab = await waitFor(() => gBrowser.tabs.find(t => !before.has(t)), 'the link tab', 3000); } catch (e) {}
await sleep(300);
const opened = newTab ? (newTab.linkedBrowser.currentURI.spec + ' ' + (newTab.linkedBrowser.userTypedValue || '')) : '';
if (newTab) gBrowser.removeTab(newTab);
return {
  h2: !!el.querySelector('h2'), li: !!el.querySelector('li strong'), pre: !!el.querySelector('pre code'),
  scripts: el.querySelectorAll('script').length, imgs: el.querySelectorAll('img').length,
  handlers: [...el.querySelectorAll('*')].filter(n => [...n.attributes].some(a => a.name.startsWith('on'))).length,
  pwned: doc.defaultView.wrappedJSObject.pwned ?? null, linkHref: link?.href, opened,
  panelUrl: doc.location.href,
};
""")
        return [
            check("Markdown renders headings, lists and code", r["h2"] and r["li"] and r["pre"], r),
            check("scripts are stripped", r["scripts"] == 0 and r["pwned"] is None, r),
            check("images and event handlers are stripped", r["imgs"] == 0 and r["handlers"] == 0, r),
            check("links open in a new tab, not in the panel", "example.com/from-answer" in r["opened"] and r["panelUrl"].endswith("panel.html"), r),
        ]

    def tool_calls_show():
        state = m.chrome("return await ask('TOOL please');")
        tool = next((x for x in state["messages"] if x["role"] == "tool"), None)
        return [
            check("tool call shows with its title", tool and "Searching the web" in tool["text"], state["messages"][-3:]),
            check("tool call shows as completed", tool and tool["state"] == "completed", tool),
            check("text after the tool is a new bubble", state["messages"][-1]["role"] == "assistant" and state["messages"][-1]["text"].startswith("after the tool"), state["messages"][-2:]),
        ]

    def crash_recovers():
        inits = len(agent.messages("initialize"))
        crashed = m.chrome("return await ask('CRASH please');")
        session = agent.last_prompt()["sessionId"]
        error = crashed["messages"][-1]
        state = m.chrome("return await ask('hello again');")
        resumes = agent.messages("session/resume")
        prompt = agent.last_prompt()
        return [
            check("a crash shows an error in the panel", error["role"] == "error" and "agent stopped" in error["text"], error),
            check("the next question restarts the agent", len(agent.messages("initialize")) == inits + 1),
            check("and resumes the session it was in", resumes and resumes[-1]["params"]["sessionId"] == session and prompt["sessionId"] == session, resumes[-1:]),
            check("the resumed session gets the instructions again, not the page", "reading assistant" in prompt["prompt"][0].get("text", "") and not any(b.get("type") == "resource" for b in prompt["prompt"]), prompt["prompt"]),
            check("and gets an answer", "Echo: hello again | resources=0" in state["messages"][-1]["text"], state["messages"][-1]),
        ]

    def shortcut_toggles_panel():
        switch_tab(m, tabs["article"])
        toggle = "document.getElementById('cmd_agentSidebarToggle').doCommand(); await sleep(500); return { open: panelOpen(), state: panelOpen() ? panelState() : null };"
        # Panel open with the focus in it: the shortcut closes it.
        m.chrome("panelDoc().getElementById('input').focus(); return true;")
        closed = m.chrome(toggle)
        # Closed, nothing selected: the shortcut opens it, with no chip.
        m.content("window.getSelection().removeAllRanges(); document.activeElement?.blur(); return true;")
        reopened = m.chrome(toggle)
        # Open, focus back on the page, new text selected: it goes into the
        # composer and the panel stays open.
        m.chrome("gBrowser.selectedBrowser.focus(); return true;")
        m.content(SELECT_ELEMENT, "#abstract", "")
        added = m.chrome(toggle)
        # Open, the same selection still on the page: the shortcut closes it.
        m.chrome("gBrowser.selectedBrowser.focus(); return true;")
        closed_again = m.chrome(toggle)
        return [
            check("shortcut closes the open panel", not closed["open"], closed),
            check("shortcut opens it again, without a stale chip", reopened["open"] and reopened["state"]["chipHidden"] and reopened["state"]["inputFocused"], reopened),
            check("new page selection goes into the open panel", added["open"] and "Synthetic priors have so far" in added["state"]["chip"], added),
            check("same selection again closes the panel", not closed_again["open"], closed_again),
        ]

    def selection_in_text_field():
        m.content("const f = document.getElementById('notes'); f.focus(); f.setSelectionRange(0, 13); return true;")
        m.chrome("document.getElementById('cmd_agentSidebarToggle').doCommand(); await waitFor(() => panelOpen(), 'panel', 5000); await sleep(300); return true;")
        state = m.chrome("return panelState();")
        return [check("text selected in a field becomes the chip", state["chip"] == "selected text", state)]

    def model_and_effort_switch():
        switch_tab(m, tabs["article"])
        changed = m.chrome("return await choose('effort', 'high');")
        sets = agent.messages("session/set_config_option")
        pref = m.chrome("return Services.prefs.getStringPref('uc.agent-sidebar.codex.reasoning_effort', '');")
        state = m.chrome("return await ask('which effort?');")
        # A new conversation (a new tab) starts with the chosen effort.
        tabs["third"] = open_tab(m, f"{web}/article.html")
        # The panel stays open across tabs and shows the new tab's conversation.
        fresh = m.chrome("await waitFor(() => panelOpen() && glue().panel, 'panel', 5000); await sleep(200); return await ask('new tab effort?');")
        return [
            check("choosing an effort sets it on the session", sets and sets[-1]["params"] == {"sessionId": sets[-1]["params"]["sessionId"], "configId": "reasoning_effort", "value": "high"}, sets[-1:] if sets else None),
            check("the header shows the new effort", changed["effort"] == "high", changed),
            check("the choice is remembered per agent", pref == "high", pref),
            check("the agent answers with the new effort", "effort=high" in state["messages"][-1]["text"], state["messages"][-1]),
            check("a new conversation starts with the chosen effort", "effort=high" in fresh["messages"][-1]["text"] and fresh["effort"] == "high", fresh),
        ]

    def lean_codex_settings():
        r = m.chrome(r"""
const { leanCodexEnv } = ChromeUtils.importESModule(glue().AgentService.base + "AgentService.sys.mjs");
const home = PathUtils.join(PathUtils.tempDir, "agent-sidebar-codex-home-" + Date.now());
await IOUtils.makeDirectory(home);
await IOUtils.writeUTF8(PathUtils.join(home, "config.toml"), 'model = "x"\n[mcp_servers.github]\ncommand = "gh"\n[mcp_servers."my server"]\ncommand = "y"\n');
const env = await leanCodexEnv({ env: { CODEX_PATH: "/bin/codex", CODEX_HOME: home, CODEX_CONFIG: '{"model_reasoning_effort":"low","features":{"web_search":true}}' } });
await IOUtils.remove(home, { recursive: true });
return JSON.parse(env.CODEX_CONFIG);
""")
        features = r.get("features", {})
        return [
            check("Codex runs without the user's MCP servers", r.get("mcp_servers") == {"github": {"enabled": False}, "my server": {"enabled": False}}, r),
            check("nor their notifier, shell snapshot and other unused features",
                  r.get("notify") == [] and features.get("shell_snapshot") is False and features.get("computer_use") is False and features.get("apps") is False, r),
            check("the agent's own CODEX_CONFIG still wins", r.get("model_reasoning_effort") == "low" and features.get("web_search") is True, r),
        ]

    def header_is_ready_before_the_agent():
        # The options sessions reported are saved for the next start.
        cached = m.chrome("const S = glue().AgentService; S.saveOptions(); await sleep(300); return await IOUtils.readJSON(S.optionsPath());")
        # Next start: the cache is all there is, and the agent is slow to start.
        config = ctx["config"].read_text()
        slow = json.loads(config)
        slow["agents"]["codex"]["args"] += ["--slow-start", "3"]
        ctx["config"].write_text(json.dumps(slow))
        tabs["fourth"] = open_tab(m, f"{web}/article.html")
        r = m.chrome(r"""
const S = glue().AgentService;
S.shutdown();
S.options.clear();
await S.loadOptions();
SidebarController.hide();
await sleep(300);
const started = Date.now();
document.getElementById('cmd_agentSidebarToggle').doCommand();
await waitFor(() => panelOpen() && panelDoc().getElementById('harness').options.length && S.connecting.has('codex'), 'the panel', 5000);
const state = panelState();
const shown = Date.now() - started;
const early = { session: conv().sessionId, starting: S.connecting.has('codex') };
await waitFor(() => conv().sessionId, 'the slow agent', 10000);
return { shown, state, early };
""")
        ctx["config"].write_text(config)
        state = r["state"]
        return [
            check("the agents' options are saved to disk", [o["id"] for o in cached.get("codex", [])][:3] == ["mode", "model", "reasoning_effort"], cached),
            check("the header shows model and effort while the agent still starts",
                  state["model"] == "mock-model" and state["effort"] == "high" and r["early"]["session"] is None and r["early"]["starting"], r),
            check("the panel opens in under a second", r["shown"] < 1000, r["shown"]),
        ]

    def idle_agent_stops_and_resumes():
        switch_tab(m, tabs["fourth"])
        first = m.chrome("return await ask('before the break');")
        r = m.chrome(r"""
const S = glue().AgentService;
const conn = S.conns.get('codex');
const session = conv().sessionId;
S.sweep(Date.now() + 11 * 60000);
await waitFor(() => conn.exited, 'the agent to stop', 5000);
await sleep(200);
return { session, parked: conv().parkedId, live: conv().sessionId, running: S.conns.has('codex'), state: panelState() };
""")
        inits = len(agent.messages("initialize"))
        state = m.chrome("return await ask('after the break');")
        prompt = agent.last_prompt()
        resumes = agent.messages("session/resume")
        return [
            check("an agent unused for 10 minutes stops", not r["running"] and r["live"] is None and r["parked"] == r["session"], r),
            check("the header keeps its options while it is stopped", r["state"]["model"] == "mock-model" and r["state"]["effort"] == "high", r["state"]),
            check("the next question starts it again", len(agent.messages("initialize")) == inits + 1),
            check("and resumes the conversation's session", resumes and resumes[-1]["params"]["sessionId"] == r["session"] and prompt["sessionId"] == r["session"], resumes[-1:]),
            check("without sending the page again", "Echo: after the break | resources=0" in state["messages"][-1]["text"] and "resources=1" in first["messages"][-1]["text"], state["messages"][-1]),
            check("no note about starting over", not any(x["role"] == "note" for x in state["messages"]), state["messages"]),
        ]

    def open_sessions_are_limited():
        # "fourth" has an open session; "fifth" opens another; the PDF tab's
        # was closed when the agent stopped and opens a third.
        fourth = m.chrome("return conv().sessionId;")
        tabs["fifth"] = open_tab(m, f"{web}/article.html")
        m.chrome("await waitFor(() => panelOpen() && glue().panel, 'panel', 5000); await sleep(200); return await ask('fifth tab');")
        switch_tab(m, tabs["pdf"])
        pdf_session = m.chrome("return conv().parkedId;")
        state = m.chrome("await sleep(300); return await ask('back to the PDF');")
        r = m.chrome(r"""
const S = glue().AgentService;
const codex = [...S.conversations.values()].filter(c => c.harness === 'codex');
return { open: codex.filter(c => c.sessionId).map(c => c.sessionId), session: conv().sessionId };
""")
        closes = [x["params"]["sessionId"] for x in agent.messages("session/close")]
        return [
            check("at most 2 sessions stay open per agent", len(r["open"]) == 2 and fourth not in r["open"], r),
            check("the least recently used one is closed", fourth in closes, {"fourth": fourth, "closed": closes[-3:]}),
            check("a closed one resumes when its tab asks", r["session"] == pdf_session and "Echo: back to the PDF | resources=0" in state["messages"][-1]["text"], {"r": r, "pdf": pdf_session, "last": state["messages"][-1]}),
        ]

    def restore_fallback():
        # The mock Claude agent cannot resume sessions.
        r = m.chrome(r"""
const S = glue().AgentService;
const conn = S.conns.get('claude');
S.sweep(Date.now() + 11 * 60000);
await waitFor(() => conn.exited, 'the agent to stop', 5000);
return true;
""")
        state = m.chrome("return await ask('still there?');")
        prompt = ctx["claude"].last_prompt()
        note = next((x for x in state["messages"] if x["role"] == "note"), None)
        return [
            check("an agent that cannot resume says it starts over", note and "starts over" in note["text"], state["messages"]),
            check("and gets the instructions and the page again", "reading assistant" in prompt["prompt"][0].get("text", "") and any(b.get("type") == "resource" for b in prompt["prompt"]), prompt["prompt"]),
            check("and answers", "Echo: still there? | resources=1" in state["messages"][-1]["text"], state["messages"][-1]),
        ]

    def harness_switch():
        switch_tab(m, tabs["third"])
        before = m.chrome("await sleep(300); return panelState();")
        switched = m.chrome("return await choose('harness', 'claude');")
        state = m.chrome("return await ask('hello claude');")
        claude = ctx["claude"]
        new = (claude.messages("session/new") or [{}])[-1].get("params", {})
        prompt = claude.last_prompt() or {"prompt": []}
        first = prompt["prompt"][0] if prompt["prompt"] else {}
        sets = claude.messages("session/set_config_option")
        return [
            check("switching agent starts a new conversation", before["messages"] and switched["messages"] == [], {"before": len(before["messages"]), "after": switched["messages"]}),
            check("the question goes to the other agent", "Echo: hello claude" in state["messages"][-1]["text"] and claude.last_prompt() is not None, state["messages"][-1:]),
            check("its session gets the agent's _meta from config.json", new.get("_meta") == {"claudeCode": {"options": {"settingSources": []}}}, new),
            check("no appended system prompt where the agent cannot append", "systemPrompt" not in new, new),
            check("instructions go in the first message instead", first.get("type") == "text" and "reading assistant" in first.get("text", ""), first),
            check("config.json defaults set the effort", any(s["params"]["configId"] == "effort" and s["params"]["value"] == "low" for s in sets) and "effort=low" in state["messages"][-1]["text"], sets),
            check("the header shows this agent's options", state["harness"] == "claude" and state["model"] == "opus" and state["effort"] == "low", state),
            check("the agent choice is remembered", m.chrome("return Services.prefs.getStringPref('uc.agent-sidebar.harness', '');") == "claude"),
        ]

    def uninit_cleans_up():
        r = m.chrome(r"""
glue().uninit();
await sleep(200);
return {
  panelOpen: SidebarController.isOpen && SidebarController.currentID === 'viewAgentSidebar',
  keyset: !!document.getElementById('agent-sidebar-keys'),
  command: !!document.getElementById('cmd_agentSidebarAsk'),
  registered: SidebarController.sidebars.has('viewAgentSidebar'),
  glue: !!window.ucAgentSidebar,
};
""")
        return [check("uninit closes the panel and removes everything", not any(r.values()), r)]

    for name, fn in [
        ("shortcut_opens_panel", shortcut_opens_panel),
        ("question_carries_page_and_selection", question_carries_page_and_selection),
        ("follow_up_skips_page_text", follow_up_skips_page_text),
        ("pdf_selection_and_text", pdf_selection_and_text),
        ("tabs_keep_their_conversations", tabs_keep_their_conversations),
        ("stop_cancels_the_turn", stop_cancels_the_turn),
        ("new_chat_starts_over", new_chat_starts_over),
        ("permission_prompt", permission_prompt),
        ("markdown_is_sanitized", markdown_is_sanitized),
        ("tool_calls_show", tool_calls_show),
        ("crash_recovers", crash_recovers),
        ("shortcut_toggles_panel", shortcut_toggles_panel),
        ("selection_in_text_field", selection_in_text_field),
        ("model_and_effort_switch", model_and_effort_switch),
        ("lean_codex_settings", lean_codex_settings),
        ("header_is_ready_before_the_agent", header_is_ready_before_the_agent),
        ("idle_agent_stops_and_resumes", idle_agent_stops_and_resumes),
        ("open_sessions_are_limited", open_sessions_are_limited),
        ("harness_switch", harness_switch),
        ("restore_fallback", restore_fallback),
    ]:
        if ctx["only"] and ctx["only"] not in name:
            continue
        step(name, fn)

    if ctx["real"]:
        def real_agents():
            checks = []
            harnesses = m.chrome(r"""
const [config] = arguments;
const S = glue().AgentService;
// Stop the mock agents first: that saves their options next to the mock config.
S.shutdown();
Services.prefs.setStringPref('uc.agent-sidebar.config', config);
for (const name of Services.prefs.getChildList('uc.agent-sidebar.')) {
  if (/\.(model|effort|reasoning_effort)$/.test(name)) Services.prefs.clearUserPref(name);
}
S.config = null;
S.options.clear();
await S.loadOptions();
return (await S.harnesses()).map(h => h.id);
""", str(ctx["real"]))
            for harness in harnesses:
                r = m.chrome(r"""
const [harness] = arguments;
gBrowser.selectedTab = gBrowser.tabs.find(t => t.linkedBrowser.currentURI.spec.endsWith('/article.html'));
await sleep(300);
if (!panelOpen()) { document.getElementById('cmd_agentSidebarToggle').doCommand(); await waitFor(() => panelOpen(), 'panel', 5000); }
glue().AgentService.reset(conv());
glue().AgentService.setHarness(conv(), harness);
const started = Date.now();
const state = await (async () => {
  const doc = panelDoc();
  const input = doc.getElementById('input');
  input.value = 'In five words or fewer: what method does this page propose? Then add the token PONG.';
  input.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await waitFor(() => !conv().busy, 'real answer', 180000);
  await sleep(200);
  return panelState();
})();
return { seconds: (Date.now() - started) / 1000, last: state.messages[state.messages.length - 1], model: state.model, effort: state.effort };
""", harness)
                print(f"  {harness} ({r['model']} · {r['effort']}) answered in {r['seconds']:.1f}s: {r['last']['text'][:160]!r}")
                checks.append(check(f"real {harness} answers about the page", r["last"]["role"] == "assistant" and "PONG" in r["last"]["text"].upper(), r))
                # Stopped after sitting unused, then asked again: the agent
                # resumes the session from its own transcript.
                r2 = m.chrome(r"""
const S = glue().AgentService;
const conn = S.conns.get(conv().harness);
const before = conv().sessionId;
S.sweep(Date.now() + 11 * 60000);
await waitFor(() => conn.exited, 'the agent to stop', 10000);
const started = Date.now();
const doc = panelDoc();
const input = doc.getElementById('input');
input.value = 'What did my previous question ask about? Five words or fewer, then add the token PONG2.';
input.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await waitFor(() => !conv().busy, 'real answer', 180000);
await sleep(200);
const state = panelState();
return { seconds: (Date.now() - started) / 1000, before, after: conv().sessionId, last: state.messages[state.messages.length - 1],
         notes: state.messages.filter(x => x.role === 'note' || x.role === 'error').map(x => x.text) };
""")
                print(f"  {harness} after an idle stop answered in {r2['seconds']:.1f}s: {r2['last']['text'][:160]!r}")
                checks.append(check(f"real {harness} resumes its session after an idle stop",
                                    r2["before"] == r2["after"] and not r2["notes"] and "PONG2" in r2["last"]["text"].upper(), r2))
            # Stopping the agents leaves none of their processes behind.
            m.chrome("glue().AgentService.shutdown(); return true;")
            deadline = time.time() + 10
            while (left := agent_processes(ctx["zen_pid"])) and time.time() < deadline:
                time.sleep(0.5)
            checks.append(check("stopped agents leave no processes behind", not left, left))
            return checks
        step("real_agents", real_agents)

    try:
        m.set_context("chrome")
        shot = m.send("WebDriver:TakeScreenshot", {"full": False, "hash": False, "scroll": False})
        (HERE / "output" / "window.png").write_bytes(base64.b64decode(shot["value"]))
    except Exception as e:
        print(f"(screenshot failed: {e})")

    if not ctx["only"]:
        step("uninit_cleans_up", uninit_cleans_up)
    return results.count(False)


# --- Runner -------------------------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_prefs(profile, port, config):
    prefs = {
        "marionette.port": port,
        "zen.welcome-screen.seen": True,
        "zen.urlbar.open-on-startup": False,
        "zen.updates.show-update-notification": False,
        "browser.shell.checkDefaultBrowser": False,
        "app.update.disabledForTesting": True,
        "devtools.console.stdout.chrome": True,
        "uc.agent-sidebar.config": str(config),
        "uc.agent-sidebar.debug": True,
        "pdfjs.firstRun": False,
    }
    (Path(profile) / "user.js").write_text("".join(f'user_pref("{k}", {json.dumps(v)});\n' for k, v in prefs.items()))


def install_mod(profile):
    js = Path(profile) / "chrome" / "JS"
    js.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "agent-sidebar.uc.js", js / "agent-sidebar.uc.js")
    shutil.copytree(ROOT / "agent-sidebar", js / "agent-sidebar", ignore=shutil.ignore_patterns("config.json"))
    return js


LOAD_DIRECT = r"""
const [jsDir] = arguments;
const manifest = Services.dirsvc.get("ProfD", Ci.nsIFile);
manifest.append("agentsidebartest.manifest");
const dir = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
dir.initWithPath(jsDir);
await IOUtils.writeUTF8(manifest.path, `content agentsidebartest ${Services.io.newFileURI(dir).spec}\n`);
Components.manager.QueryInterface(Ci.nsIComponentRegistrar).autoRegister(manifest);
Services.scriptloader.loadSubScript("chrome://agentsidebartest/content/agent-sidebar.uc.js", window);
return !!window.ucAgentSidebar;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zen", default=os.environ.get("ZEN_BINARY", DEFAULT_ZEN))
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--keep-profile", action="store_true")
    parser.add_argument("-k", dest="only", help="run tests whose name contains this")
    parser.add_argument("--via-loader", metavar="CHROME_DIR", help="copy fx-autoconfig's utils/ from this profile chrome folder and let it load the script")
    parser.add_argument("--real", metavar="CONFIG", help="also ask the real agent described by this config.json")
    args = parser.parse_args()

    out = HERE / "output"
    out.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="zen-agent-sidebar-test-"))
    profile = work / "profile"
    profile.mkdir()
    www = work / "www"
    shutil.copytree(HERE / "www", www)
    make_pdf(www / "paper.pdf", [
        ["MARKER-PDF-PAGE-ONE synthetic priors explained.", "A second line on page one."],
        ["MARKER-PDF-PAGE-TWO the method section.", "More words on page two."],
    ])
    server, web = serve(www)

    agent_log = work / "codex.log"
    claude_log = work / "claude.log"
    python = sys.executable if os.path.isabs(sys.executable) else "/usr/bin/python3"
    mock = str(HERE / "mock_agent.py")
    config = work / "agent.json"
    config.write_text(json.dumps({
        "default": "codex",
        "agents": {
            "codex": {"name": "Codex", "command": python, "args": ["-I", mock, str(agent_log)], "cwd": str(work / "agent-cwd"), "env": {}},
            "claude": {
                "name": "Claude Code", "command": python, "args": ["-I", mock, str(claude_log), "--claude"],
                "cwd": str(work / "agent-cwd"), "env": {},
                "sessionMeta": {"claudeCode": {"options": {"settingSources": []}}},
                "defaults": {"effort": "low"},
            },
        },
    }))

    port = free_port()
    write_prefs(profile, port, config)
    js_dir = install_mod(profile)
    if args.via_loader:
        shutil.copytree(Path(args.via_loader).expanduser() / "utils", profile / "chrome" / "utils")

    env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1")
    if not args.headful:
        env.update(MOZ_HEADLESS="1", MOZ_HEADLESS_WIDTH="1400", MOZ_HEADLESS_HEIGHT="900")
    cmd = [args.zen, "--marionette", "--remote-allow-system-access", "--no-remote", "--profile", str(profile)]
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
            m.send("WebDriver:SetTimeouts", {"script": 180000, "pageLoad": 60000})
            info = m.chrome("await window.delayedStartupPromise; return Services.appinfo.name + ' ' + AppConstants.MOZ_APP_VERSION_DISPLAY + ' (Firefox ' + Services.appinfo.version + ')';")
            print(f"{info}, profile {profile}")
            if args.via_loader:
                loaded = m.chrome("await waitFor(() => window.ucAgentSidebar, 'the loader to start the script', 15000); return true;")
                print("script started by the fx-autoconfig loader")
            elif not m.chrome(LOAD_DIRECT, str(js_dir)):
                raise RuntimeError("script did not initialize")
        except Exception as e:
            failures += 1
            print(f"FAIL setup: {e}")
        else:
            failures += run_tests(m, {"agent": Agent(agent_log), "claude": Agent(claude_log), "web": web, "only": args.only,
                                      "real": args.real, "config": config, "zen_pid": proc.pid})
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
        server.shutdown()
        if not args.keep_profile:
            shutil.rmtree(work, ignore_errors=True)

    errors = [l for l in log_path.read_text(errors="replace").splitlines() if "[agent-sidebar]" in l and ("rror" in l or "fail" in l)]
    if errors:
        print("\nagent-sidebar errors in the browser log:")
        for line in errors[:20]:
            print("  " + line[:300])
    print(f"\n{'OK' if failures == 0 else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
