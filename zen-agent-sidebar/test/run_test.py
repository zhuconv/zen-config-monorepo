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
import struct
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
    status: q("#input").placeholder,
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
async function ask(question, timeout = 20000) {
  const doc = panelDoc();
  const input = doc.getElementById("input");
  input.value = question;
  input.dispatchEvent(new doc.defaultView.Event("input"));
  input.dispatchEvent(new doc.defaultView.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await waitFor(() => !conv().busy, "the answer to " + JSON.stringify(question), timeout);
  await sleep(150);
  return panelState();
}
/** Edit the question whose text is `text` the way the user would: its edit
 * button, the new text, then Enter (sent) or Escape (cancelled). */
async function editQuestion(text, edited, key = "Enter", timeout = 20000) {
  const doc = panelDoc();
  const win = doc.defaultView;
  const el = [...doc.querySelectorAll("#messages .msg.user")].find(e => e.querySelector(":scope > .text")?.textContent === text);
  if (!el) throw new Error("no question " + JSON.stringify(text));
  el.querySelector(".actions .edit").click();
  const box = await waitFor(() => el.querySelector(".editor textarea"), "the editor", 3000);
  const shown = { value: box.value, focused: doc.activeElement === box, editing: el.classList.contains("editing") };
  box.value = edited;
  box.dispatchEvent(new win.Event("input"));
  box.dispatchEvent(new win.KeyboardEvent("keydown", { key, bubbles: true }));
  if (key === "Enter") {
    await waitFor(() => conv().messages.some(m => m.role === "user" && m.text === edited), "the edited question", 5000);
    await waitFor(() => !conv().busy, "the answer to the edited question", timeout);
  }
  await sleep(150);
  return { shown, state: panelState() };
}
// Files, made in the panel's own window.
function panelFile(parts, name, type) {
  return new (panelDoc().defaultView.File)(parts, name, { type });
}
async function wordImage(word, width, height) {
  const canvas = new OffscreenCanvas(width, height);
  const c = canvas.getContext("2d");
  c.fillStyle = "#fff";
  c.fillRect(0, 0, width, height);
  c.fillStyle = "#000";
  c.font = `bold ${Math.round(height / 4)}px sans-serif`;
  c.fillText(word, width / 10, height / 2);
  return panelFile([await canvas.convertToBlob({ type: "image/png" })], word.toLowerCase() + ".png", "image/png");
}
/** Attach files the way the user would: the 📎 file picker, a real paste (an
 * image as image data, like a screenshot; any other file as a file copied in
 * Finder), or a drop. Headless Zen has a clipboard of its own. */
async function attachVia(how, files) {
  const doc = panelDoc();
  const win = doc.defaultView;
  if (how === "picker") {
    const input = doc.getElementById("file-input");
    input.mozSetFileArray(files);
    input.dispatchEvent(new win.Event("change", { bubbles: true }));
  } else if (how === "paste") {
    for (const file of files) {
      const trans = Cc["@mozilla.org/widget/transferable;1"].createInstance(Ci.nsITransferable);
      trans.init(null);
      if (file.type.startsWith("image/")) {
        const tools = Cc["@mozilla.org/image/tools;1"].getService(Ci.imgITools);
        trans.addDataFlavor(file.type);
        trans.setTransferData(file.type, tools.decodeImageFromArrayBuffer(await file.arrayBuffer(), file.type));
      } else {
        const path = PathUtils.join(PathUtils.profileDir, "pasted-files", file.name);
        await IOUtils.write(path, new Uint8Array(await file.arrayBuffer()));
        const copied = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
        copied.initWithPath(path);
        trans.addDataFlavor("application/x-moz-file");
        trans.setTransferData("application/x-moz-file", copied);
      }
      Services.clipboard.setData(trans, null, Ci.nsIClipboard.kGlobalClipboard);
      doc.getElementById("input").focus();
      SidebarController.browser.docShell.doCommand("cmd_paste");
    }
  } else {
    const data = new win.DataTransfer();
    for (const file of files) data.items.add(file);
    const target = doc.getElementById("messages");
    for (const type of ["dragenter", "dragover", "drop"]) {
      target.dispatchEvent(new win.DragEvent(type, { dataTransfer: data, bubbles: true, cancelable: true }));
    }
  }
}
function attachmentChips() {
  return [...panelDoc().querySelectorAll("#attachments .attachment")].map(e => ({
    name: e.querySelector(".name").textContent,
    info: e.querySelector(".info").textContent,
    state: e.className.replace("attachment ", ""),
    thumb: !!e.querySelector("img"),
  }));
}
async function filesRead(count) {
  await waitFor(() => attachmentChips().length === count && attachmentChips().every(c => c.state !== "reading"), count + " files to be read", 15000);
  return attachmentChips();
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
        read_ahead = m.chrome("await sleep(300); return conv().pageText?.url ?? null;")
        checks += [
            check("a web page is not read ahead, but when the question is sent", read_ahead is None, read_ahead),
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
            check("the site menu is left out", "Home · Papers" not in resource.get("text", ""), resource.get("text", "")[:200]),
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
        read_ahead = m.chrome("try { await waitFor(() => conv().pageText, 'the read-ahead', 3000); return conv().pageText.url; } catch (e) { return null; }")
        state = m.chrome("return await ask('what is this?');")
        prompt = agent.last_prompt()
        resource = next((b["resource"] for b in prompt["prompt"] if b.get("type") == "resource"), {})
        text = prompt["prompt"][-1]["text"]
        return [
            check("PDF text layer selection reaches the chip", selected and "MARKER-PDF-PAGE-ONE" in chip, {"selected": selected, "chip": chip}),
            check("a PDF is read while the user types", read_ahead == f"{web}/paper.pdf", read_ahead),
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
const win = doc.defaultView;
const input = doc.getElementById('input');
const rect = id => { const b = doc.getElementById(id).getBoundingClientRect(); return { x: Math.round(b.x), mid: Math.round(b.y + b.height / 2), w: Math.round(b.width) }; };
const idle = { placeholder: input.placeholder, sendDisabled: doc.getElementById('send').disabled };
input.value = 'SLOW please';
input.dispatchEvent(new win.Event('input'));
const layout = {
  attach: rect('attach'), input: rect('input'), send: rect('send'),
  attachText: doc.getElementById('attach').textContent,
  plus: win.getComputedStyle(doc.getElementById('attach'), '::before').backgroundImage,
  sendDisabled: doc.getElementById('send').disabled,
};
input.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await waitFor(() => (conv().current?.text || '').includes('word2'), 'streaming', 8000);
const busy = {
  placeholder: input.placeholder, stop: rect('stop'),
  stopVisible: !doc.getElementById('stop').hidden, sendHidden: doc.getElementById('send').hidden,
  editShown: [...doc.querySelectorAll('.msg.user > .actions')].filter(e => win.getComputedStyle(e).display !== 'none').length,
};
doc.getElementById('stop').click();
await waitFor(() => !conv().busy, 'stopped', 8000);
await sleep(150);
const after = { placeholder: input.placeholder, edits: doc.querySelectorAll('.msg.user > .actions .edit').length };
return { idle, layout, busy, after, state: panelState() };
""")
        last = state["state"]["messages"][-1]
        layout, busy = state["layout"], state["busy"]
        one_row = (layout["attach"]["x"] < layout["input"]["x"] < layout["send"]["x"]
                   and max(abs(layout[k]["mid"] - layout["input"]["mid"]) for k in ("attach", "send")) <= 2)
        return [
            check("the question box is one row: + first, then the text, then Send", one_row, layout),
            check("+ is an icon, not a paperclip", layout["attachText"] == "" and "plus.svg" in layout["plus"], layout),
            check("Send is dimmed with nothing to send, and not once there is text", state["idle"]["sendDisabled"] and not layout["sendDisabled"], {"idle": state["idle"], "layout": layout}),
            check("while it answers, the box says Thinking… and Stop takes Send's place",
                  busy["placeholder"] == "Thinking…" and busy["stopVisible"] and busy["sendHidden"] and busy["stop"]["x"] == layout["send"]["x"], busy),
            check("questions can't be edited while it answers", busy["editShown"] == 0 and state["after"]["edits"] >= 1, {"busy": busy["editShown"], "after": state["after"]}),
            check("Stop sends session/cancel", len(agent.messages("session/cancel")) == 1, agent.messages("session/cancel")),
            check("answer is marked stopped", last["role"] == "assistant" and last["state"] == "stopped" and "word59" not in last["text"], last),
            check("Send and the usual prompt come back after stopping",
                  m.chrome("return !panelDoc().getElementById('send').hidden;") and state["after"]["placeholder"] == "Ask about this page…", state["after"]),
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
        before = next((x for x in state["messages"] if x["text"].startswith("before the tool")), None)
        return [
            check("tool call shows with its title", tool and "Searching the web" in tool["text"], state["messages"][-3:]),
            check("tool call shows as completed", tool and tool["state"] == "completed", tool),
            check("text after the tool is a new bubble", state["messages"][-1]["role"] == "assistant" and state["messages"][-1]["text"].startswith("after the tool"), state["messages"][-2:]),
            check("text before the tool is finished, not left streaming", before and before["state"] == "done", state["messages"][-4:]),
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

    def attachments_reach_the_agent():
        tabs["files"] = open_tab(m, f"{web}/article.html")
        r = m.chrome(r"""
const [web] = arguments;
if (!panelOpen()) document.getElementById('cmd_agentSidebarToggle').doCommand();
await waitFor(() => panelOpen() && glue().panel, 'panel', 5000);
glue().AgentService.setHarness(conv(), 'codex');
await sleep(200);
const pdf = await (await fetch(web + '/paper.pdf')).arrayBuffer();
const blank = await (await fetch(web + '/blank.pdf')).arrayBuffer();
await attachVia('picker', [panelFile(['# Notes\nMARKER-TEXT-FILE one\nline two\n'], 'notes.md', 'text/markdown'),
                     panelFile([pdf], 'paper.pdf', 'application/pdf')]);
await attachVia('paste', [await wordImage('PELICAN', 3200, 2000)]);
await attachVia('drop', [panelFile([new Uint8Array([0x50, 0x4b, 3, 4, 0xff, 0xfe, 0, 0x80])], 'report.docx', ''),
                   panelFile([blank], 'scan.pdf', 'application/pdf')]);
const chips = await filesRead(5);
for (const button of panelDoc().querySelectorAll('#attachments .attachment.error .remove')) button.click();
await sleep(100);
return { chips, kept: attachmentChips() };
""", web)
        chips = {c["name"]: c for c in r["chips"]}
        notes, paper, image = chips.get("notes.md", {}), chips.get("paper.pdf", {}), chips.get("pasted.png", {})
        docx, scan = chips.get("report.docx", {}), chips.get("scan.pdf", {})
        checks = [
            check("the + button's picker, a paste and a drop all attach", len(r["chips"]) == 5, r["chips"]),
            check("a text file shows its length", notes.get("state") == "ready" and notes.get("info", "").endswith("chars"), notes),
            check("a PDF shows its pages", paper.get("state") == "ready" and paper.get("info") == "2 pages", paper),
            check("a big image is scaled to 1568 px, with a thumbnail", image.get("info") == "1568×980" and image.get("thumb"), image),
            check("an Office file is refused with a reason", docx.get("state") == "error" and "can't be read" in docx.get("info", ""), docx),
            check("a PDF without text is refused with a reason", scan.get("state") == "error" and "no text" in scan.get("info", ""), scan),
            check("✕ removes a file", [c["name"] for c in r["kept"]] == ["notes.md", "paper.pdf", "pasted.png"], r["kept"]),
        ]
        state = m.chrome("return await ask('what are these files?');")
        after = m.chrome(r"""
const sent = conv().messages.find(x => x.files?.length);
return { chips: attachmentChips(), hidden: panelDoc().getElementById('attachments').hidden, kept: sent.files,
         held: sent.attachments.map(a => [a.name, !!(a.data || a.text)]) };
""")
        blocks = (agent.last_prompt() or {"prompt": []})["prompt"]
        types = [b.get("type") for b in blocks]
        resources = [b["resource"] for b in blocks if b.get("type") == "resource"]
        notes_block = next((x for x in resources if x["text"].startswith("File: notes.md")), {})
        pdf_block = next((x for x in resources if x["text"].startswith("File: paper.pdf")), {})
        image_block = next((b for b in blocks if b.get("type") == "image"), {})
        png = base64.b64decode(image_block.get("data", ""))
        size = list(struct.unpack(">II", png[16:24])) if png[:8] == b"\x89PNG\r\n\x1a\n" else None
        user = next((x for x in reversed(state["messages"]) if x["role"] == "user"), {})
        last = state["messages"][-1]
        return checks + [
            check("the files go between the page and the question", types == ["resource", "resource", "resource", "image", "text"], types),
            check("a text file arrives as its text, named", "MARKER-TEXT-FILE" in notes_block.get("text", "") and notes_block.get("uri") == "attachment:notes.md", notes_block),
            check("a PDF arrives as its text, page by page",
                  "PDF text, pages 1-2 of 2" in pdf_block.get("text", "") and "[page 2]" in pdf_block.get("text", "") and "MARKER-PDF-PAGE-TWO" in pdf_block.get("text", ""),
                  pdf_block.get("text", "")[:300]),
            check("an image arrives as a PNG of at most 1568 px", image_block.get("mimeType") == "image/png" and size == [1568, 980], {"mime": image_block.get("mimeType"), "size": size}),
            check("the agent answers", "Echo: what are these files? | resources=3" in last["text"] and "images=1" in last["text"], last),
            check("the message lists the files", all(n in user.get("text", "") for n in ["notes.md", "paper.pdf", "pasted.png"]), user),
            check("the composer is empty after sending", after["chips"] == [] and after["hidden"], after["chips"]),
            check("the message shows names and thumbnails, and keeps the files for an edit",
                  all(set(f) <= {"name", "kind", "info", "thumb"} for f in after["kept"])
                  and after["held"] == [["notes.md", True], ["paper.pdf", True], ["pasted.png", True]],
                  {"shown": [list(f) for f in after["kept"]], "held": after["held"]}),
        ]

    def attachments_wait_switch_and_trim():
        # Sent while a PDF is still being read: the question waits for it.
        waited = m.chrome(r"""
const [web] = arguments;
const doc = panelDoc();
const pdf = await (await fetch(web + '/paper.pdf')).arrayBuffer();
await attachVia('picker', [panelFile([pdf], 'again.pdf', 'application/pdf')]);
const atSend = attachmentChips().map(c => c.state);
const input = doc.getElementById('input');
input.value = 'quick one';
input.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await waitFor(() => conv().messages.some(x => x.role === 'user' && x.text === 'quick one'), 'the question', 10000);
await waitFor(() => !conv().busy, 'the answer', 10000);
return { atSend, files: conv().messages.find(x => x.text === 'quick one').files.map(f => f.name) };
""", web)
        sent = (agent.last_prompt() or {"prompt": []})["prompt"]
        # Files wait in the composer of their own tab.
        m.chrome("await attachVia('picker', [panelFile(['MARKER-STAYS'], 'stays.txt', 'text/plain')]); await filesRead(1); return true;")
        switch_tab(m, tabs.get("article") or open_tab(m, f"{web}/article.html"))
        elsewhere = m.chrome("await sleep(300); return attachmentChips();")
        switch_tab(m, tabs["files"])
        back = m.chrome("await sleep(300); return attachmentChips();")
        # Switching agents keeps them for the new agent.
        switched = m.chrome("await choose('harness', 'claude'); return attachmentChips();")
        to_claude = m.chrome("return await ask('to claude');")
        claude_blocks = (ctx["claude"].last_prompt() or {"prompt": []})["prompt"]
        # Files alone: the default question goes with them.
        alone = m.chrome(r"""
await attachVia('paste', [panelFile(['MARKER-ALONE'], 'alone.txt', 'text/plain')]);
await filesRead(1);
return await ask('');
""")
        alone_text = (ctx["claude"].last_prompt() or {"prompt": [{}]})["prompt"][-1].get("text", "")
        # Long text is cut at the page-text limit.
        trimmed = m.chrome(r"""
Services.prefs.setIntPref('uc.agent-sidebar.max-page-chars', 300);
await attachVia('picker', [panelFile(['MARKER-LONG ' + 'x'.repeat(5000)], 'long.txt', 'text/plain')]);
const chips = await filesRead(1);
await ask('trimmed?');
Services.prefs.clearUserPref('uc.agent-sidebar.max-page-chars');
return chips;
""")
        long_block = next((b["resource"] for b in (ctx["claude"].last_prompt() or {"prompt": []})["prompt"] if b.get("type") == "resource" and "long.txt" in b["resource"]["text"][:40]), {})
        body = long_block.get("text", "").split("\n\n", 1)[-1]
        return [
            check("a question sent while a PDF is read waits for it", waited["atSend"] == ["reading"] and waited["files"] == ["again.pdf"]
                  and any(b.get("type") == "resource" and "again.pdf" in b["resource"]["text"][:40] for b in sent), waited),
            check("files stay with their tab", elsewhere == [] and [c["name"] for c in back] == ["stays.txt"], {"elsewhere": elsewhere, "back": back}),
            check("switching agents keeps the files", [c["name"] for c in switched] == ["stays.txt"], switched),
            check("and the other agent gets them",
                  any(b.get("type") == "resource" and "MARKER-STAYS" in b["resource"]["text"] for b in claude_blocks) and "Echo: to claude" in to_claude["messages"][-1]["text"],
                  claude_blocks),
            check("files alone go with the default question", alone_text.endswith("解释一下这段内容。") and "Echo: 解释一下这段内容。" in alone["messages"][-1]["text"], alone_text[-80:]),
            check("long text is cut at the page-text limit", trimmed[0]["info"].endswith("truncated") and "Text (truncated)" in long_block.get("text", "") and len(body) == 300,
                  {"chip": trimmed, "header": long_block.get("text", "")[:60], "body": len(body)}),
        ]

    def page_as_seen_and_its_changes():
        tabs["forum"] = open_tab(m, f"{web}/forum.html")
        deadline = time.time() + 10  # the reviews load after the page
        while time.time() < deadline and m.content("return document.querySelectorAll('.reply').length;") < 2:
            time.sleep(0.2)
        m.chrome(r"""
if (!panelOpen()) document.getElementById('cmd_agentSidebarToggle').doCommand();
await waitFor(() => panelOpen() && glue().panel, 'panel', 5000);
glue().AgentService.setHarness(conv(), 'codex');
await sleep(200);
return true;
""")

        def page_text_sent():
            blocks = (agent.last_prompt() or {"prompt": []})["prompt"]
            return next((b["resource"]["text"] for b in blocks if b.get("type") == "resource"), None)

        m.chrome("return await ask('what do the reviewers say?');")
        full = page_text_sent() or ""
        # Another review appears, as when more replies load or a thread is opened.
        m.content(r"""
const reply = document.createElement('div');
reply.className = 'reply';
reply.innerHTML = '<h3>Official Review by Reviewer Zz99</h3><p>MARKER-REVIEW-THREE The writing is clear.</p>';
document.getElementById('replies').append(reply);
return true;
""")
        second = m.chrome("return await ask('and the new one?');")
        update = page_text_sent() or ""
        m.chrome("return await ask('anything else?');")
        unchanged = page_text_sent()
        # Something only goes away: nothing new to send.
        m.content("document.querySelector('.reply').remove(); return true;")
        m.chrome("return await ask('after a removal?');")
        removed = page_text_sent()
        # Most of the page is new: it goes again in full.
        m.content(r"""
document.getElementById('content').innerHTML = '<p>MARKER-NEW-PAGE</p>' +
  Array.from({ length: 30 }, (_, i) => `<p>new paragraph ${i} about something else entirely</p>`).join('');
return true;
""")
        m.chrome("return await ask('and now?');")
        whole = page_text_sent() or ""
        meta = [x["text"] for x in second["messages"] if x["role"] == "user"][-1]
        return [
            check("the page goes as rendered, with the reviews that loaded later",
                  all(k in full for k in ["MARKER-NOTE-TITLE", "MARKER-NOTE-ABSTRACT", "MARKER-REVIEW-ONE", "MARKER-REVIEW-TWO"]), full[:600]),
            check("the site's menu, header and footer are left out", not any(k in full for k in ["Home · Venues", "SITE-HEADER-BRAND", "SITE-FOOTER"]), full[:600]),
            check("hidden text is left out", "MARKER-HIDDEN-REPLY" not in full, full[-300:]),
            check("a change on the page reaches the agent as an update", "Page update" in update and "MARKER-REVIEW-THREE" in update, update),
            check("an update carries only what is new", "MARKER-REVIEW-ONE" not in update and "MARKER-NOTE-ABSTRACT" not in update, update),
            check("the message says the page was updated", "Page updated" in meta, meta),
            check("an unchanged page is not sent again", unchanged is None, unchanged),
            check("a removal alone sends nothing", removed is None, removed),
            check("a page that is mostly new goes again in full", "full text again" in whole and "MARKER-NEW-PAGE" in whole, whole[:300]),
        ]

    def shortcut_does_not_wait_for_busy_frames():
        tabs["framed"] = open_tab(m, f"{web}/framed.html")
        time.sleep(2)  # the frame loads and gets busy
        timing = r"""
gBrowser.selectedBrowser.focus();
await sleep(100);
const started = performance.now();
await glue().toggle();
return { ms: Math.round(performance.now() - started), open: panelOpen(), state: panelOpen() ? panelState() : null };
"""
        m.chrome("if (panelOpen()) SidebarController.hide(); await sleep(300); return true;")
        m.content("window.getSelection().removeAllRanges(); return true;")
        opened = m.chrome(timing)
        m.content(SELECT_ELEMENT, "#text", "")
        composed = m.chrome(timing)
        print(f"  (shortcut with a busy frame: {opened['ms']} ms to open, {composed['ms']} ms to take a selection)")
        return [
            check("with nothing selected, the shortcut opens the panel without waiting for a busy frame", opened["open"] and opened["ms"] < 250, opened["ms"]),
            check("with text selected, it goes into the composer without waiting either",
                  composed["open"] and "MARKER-FRAMED-PAGE" in composed["state"]["chip"] and composed["ms"] < 250, {"ms": composed["ms"], "chip": composed["state"]["chip"][:60]}),
        ]

    def typing_starts_a_stopped_agent():
        r = m.chrome(r"""
const S = glue().AgentService;
await waitFor(() => conv().sessionId, 'the session', 10000);
const conn = S.conns.get(conv().harness);
const session = conv().sessionId;
S.sweep(Date.now() + 11 * 60000);
await waitFor(() => conn.exited, 'the agent to stop', 5000);
const stopped = !conv().sessionId;
const doc = panelDoc();
const input = doc.getElementById('input');
input.focus();
input.value = 'w';
input.dispatchEvent(new doc.defaultView.Event('input'));
const started = performance.now();
await waitFor(() => conv().sessionId, 'the session to be ready again', 10000);
return { stopped, readyMs: Math.round(performance.now() - started), sameSession: conv().sessionId === session, busy: conv().busy };
""")
        state = m.chrome("return await ask('typed while it started');")
        return [
            check("typing in the composer starts a stopped agent and reopens the session before sending",
                  r["stopped"] and r["sameSession"] and not r["busy"], r),
            check("and the question then goes straight to it", "Echo: typed while it started" in state["messages"][-1]["text"], state["messages"][-1]),
        ]

    def answers_can_be_copied_and_saved():
        open_tab(m, f"{web}/article.html")
        r = m.chrome(r"""
const { Downloads } = ChromeUtils.importESModule('resource://gre/modules/Downloads.sys.mjs');
const { FileUtils } = ChromeUtils.importESModule('resource://gre/modules/FileUtils.sys.mjs');
const dir = PathUtils.join(PathUtils.profileDir, 'saved');
await IOUtils.makeDirectory(dir);
Services.prefs.setIntPref('browser.download.folderList', 2);
Services.prefs.setStringPref('browser.download.dir', dir);
Services.prefs.setBoolPref('browser.download.useDownloadDir', true);
if (!panelOpen()) { document.getElementById('cmd_agentSidebarToggle').doCommand(); await waitFor(() => panelOpen(), 'panel', 5000); }
const doc = panelDoc();
const win = doc.defaultView;
const out = { dir };
const after = el => win.getComputedStyle(el, '::after').content;

// The buttons come once an answer is complete, or stopped.
const input = doc.getElementById('input');
input.value = 'SLOW please';
input.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
const slow = await waitFor(() => doc.querySelector('.msg.assistant[data-state="streaming"]'), 'a streaming answer', 8000);
await sleep(300);
out.whileStreaming = slow.querySelectorAll('.actions').length;
out.cursor = after(slow.querySelector('.body'));
doc.getElementById('stop').click();
await waitFor(() => !conv().busy, 'the stop', 8000);
await sleep(150);
out.whenStopped = slow.querySelectorAll(':scope > .actions button').length;
out.stoppedMark = after(slow.querySelector('.body'));

await ask('DOCUMENT please');
const answers = doc.querySelectorAll('.msg.assistant');
const el = answers[answers.length - 1];
const answerRow = el.querySelector(':scope > .actions');
const blockRows = [...el.querySelectorAll('.code > .actions')];
out.text = el.textContent;
out.buttons = [answerRow, ...blockRows].map(row => [...row.querySelectorAll('button')].map(b => b.title));
function clipboard() {
  const trans = Cc['@mozilla.org/widget/transferable;1'].createInstance(Ci.nsITransferable);
  trans.init(null);
  trans.addDataFlavor('text/plain');
  try {
    Services.clipboard.getData(trans, Ci.nsIClipboard.kGlobalClipboard);
  } catch (e) {
    Services.clipboard.getData(trans, Ci.nsIClipboard.kGlobalClipboard, win.browsingContext.currentWindowContext);
  }
  const data = {};
  trans.getTransferData('text/plain', data);
  return data.value.QueryInterface(Ci.nsISupportsString).data;
}
async function press(row, action) {
  delete row.dataset.feedback;
  row.querySelector(`button[data-action="${action}"]`).click();
  return waitFor(() => row.dataset.feedback, action + ' feedback', 5000);
}
out.copied = [];
for (const row of [answerRow, ...blockRows]) {
  const feedback = await press(row, 'copy');
  out.copied.push({ feedback, text: clipboard() });
}
out.saved = [];
for (const row of [answerRow, ...blockRows, blockRows[0]]) {
  out.saved.push(await press(row, 'save'));
}
out.icon = win.getComputedStyle(answerRow.querySelector('.save'), '::before').backgroundImage;
out.files = {};
for (const path of await IOUtils.getChildren(dir)) out.files[PathUtils.filename(path)] = await IOUtils.readUTF8(path);
out.listed = (await (await Downloads.getList(Downloads.PUBLIC)).getAll())
  .filter(d => d.target.path.startsWith(dir))
  .map(d => ({ name: PathUtils.filename(d.target.path), succeeded: d.succeeded, from: d.source.url }));
out.title = gBrowser.selectedTab.label;
out.url = gBrowser.selectedBrowser.currentURI.spec;

// Firefox set to ask where to save: the Save As dialog (a stand-in here)
// offers the name, and the file goes where the user picks. Cancel saves
// nothing.
const registrar = Components.manager.QueryInterface(Ci.nsIComponentRegistrar);
const contract = '@mozilla.org/filepicker;1';
const realCID = registrar.contractIDToCID(contract);
const standInCID = Services.uuid.generateUUID();
const picked = PathUtils.join(PathUtils.profileDir, 'picked', 'chosen.md');
await IOUtils.makeDirectory(PathUtils.parent(picked));
const shown = [];
let choice = Ci.nsIFilePicker.returnOK;
const factory = {
  QueryInterface: ChromeUtils.generateQI(['nsIFactory']),
  createInstance(iid) {
    const picker = {
      QueryInterface: ChromeUtils.generateQI(['nsIFilePicker']),
      init(context, title, mode) { this.mode = mode; },
      appendFilter() {},
      appendFilters() {},
      defaultString: '', defaultExtension: '', displayDirectory: null, filterIndex: 0, file: null,
      open(callback) {
        shown.push({ name: this.defaultString, mode: this.mode, dir: this.displayDirectory?.path ?? null });
        this.file = choice === Ci.nsIFilePicker.returnOK ? new FileUtils.File(picked) : null;
        typeof callback === 'function' ? callback(choice) : callback.done(choice);
      },
    };
    return picker.QueryInterface(iid);
  },
};
registrar.registerFactory(standInCID, 'stand-in file picker', contract, factory);
Services.prefs.setBoolPref('browser.download.useDownloadDir', false);
try {
  out.askSaved = await press(answerRow, 'save');
  out.picked = (await IOUtils.exists(picked)) ? await IOUtils.readUTF8(picked) : null;
  choice = Ci.nsIFilePicker.returnCancel;
  delete answerRow.dataset.feedback;
  answerRow.querySelector('.save').click();
  await sleep(500);
  out.cancelFeedback = answerRow.dataset.feedback ?? null;
} finally {
  Services.prefs.setBoolPref('browser.download.useDownloadDir', true);
  registrar.unregisterFactory(standInCID, factory);
  registrar.registerFactory(realCID, '', contract, null);
}
out.shown = shown;
out.filesAfterCancel = (await IOUtils.getChildren(dir)).length;
await sleep(2700);
out.feedbackLater = answerRow.dataset.feedback ?? null;
return out;
""")
        answer = ("Here is the file:\n\n```markdown\n# Reading notes: **Mamba**\n\n- selective state spaces\n- `linear` time\n```\n\n"
                  "And a script:\n\n```python\nprint(\"hi\")\n```")
        block = "# Reading notes: **Mamba**\n\n- selective state spaces\n- `linear` time"
        title, files = r["title"], r["files"]
        first_shown = r["shown"][0] if r["shown"] else {}
        return [
            check("an answer gets its buttons once complete, or stopped",
                  r["whileStreaming"] == 0 and r["whenStopped"] == 2 and r["cursor"] == '"▍"' and r["stoppedMark"] == '"(stopped)"',
                  {k: r[k] for k in ("whileStreaming", "whenStopped", "cursor", "stoppedMark")}),
            check("so does each code block, saved with its language's extension",
                  r["buttons"] == [["Copy the Markdown", "Save as .md"], ["Copy the code", "Save as .md"], ["Copy the code", "Save as .py"]], r["buttons"]),
            check("the buttons stay out of the answer's text", not any(w in r["text"] for w in ("Copy", "Save", "Copied")), r["text"]),
            check("copy puts the answer's Markdown on the clipboard", r["copied"][0] == {"feedback": "Copied", "text": answer}, r["copied"][0]),
            check("copy on a code block takes just that block",
                  [c["text"] for c in r["copied"][1:]] == [block, 'print("hi")'], r["copied"][1:]),
            check("save puts the answer's Markdown in the downloads folder, named after the page",
                  files.get(f"{title}.md") == answer + "\n" and "check.svg" in r["icon"], {"files": files, "icon": r["icon"]}),
            check("a Markdown block is named after its first heading", files.get("Reading notes - Mamba.md") == block + "\n", files),
            check("other code is named after the page", files.get(f"{title}.py") == 'print("hi")\n', files),
            check("saving again keeps the first file", files.get("Reading notes - Mamba(1).md") == block + "\n" and len(files) == 4, sorted(files)),
            check("the panel says what it saved",
                  r["saved"] == [f"Saved “{title}.md”", "Saved “Reading notes - Mamba.md”", f"Saved “{title}.py”", "Saved “Reading notes - Mamba(1).md”"], r["saved"]),
            check("saved files are listed in the downloads panel, as from the page",
                  len(r["listed"]) == 4 and all(d["succeeded"] and d["from"] == r["url"] for d in r["listed"]), r["listed"]),
            check("with Firefox set to ask, Save As offers the name and saves where the user picks",
                  first_shown == {"name": f"{title}.md", "mode": 1, "dir": r["dir"]} and r["picked"] == answer + "\n" and r["askSaved"] == "Saved “chosen.md”",
                  {k: r.get(k) for k in ("shown", "picked", "askSaved")}),
            check("cancelling Save As saves nothing", len(r["shown"]) == 2 and r["cancelFeedback"] is None and r["filesAfterCancel"] == 4,
                  {k: r.get(k) for k in ("shown", "cancelFeedback", "filesAfterCancel")}),
            check("the feedback goes away", r["feedbackLater"] is None, r["feedbackLater"]),
        ]

    def editing_a_question():
        tabs["edit"] = open_tab(m, f"{web}/article.html")
        start = m.chrome(r"""
if (!panelOpen()) document.getElementById('cmd_agentSidebarToggle').doCommand();
await waitFor(() => panelOpen() && glue().panel, 'panel', 5000);
glue().AgentService.setHarness(conv(), 'codex');
await sleep(200);
await ask('first question');
await ask('second question');
await ask('third question');
return { session: conv().sessionId, answers: conv().messages.filter(x => x.role === 'assistant').map(x => x.messageId) };
""")
        # An answer in the middle: the agent goes back to the answer before it.
        r = m.chrome("return await editQuestion('second question', 'HISTORY edited second');")
        forks = agent.messages("session/fork")
        fork = forks[-1]["params"] if forks else {}
        prompt = agent.last_prompt() or {}
        forked = m.chrome("return conv().sessionId;")
        closed = [x["params"]["sessionId"] for x in agent.messages("session/close")]
        msgs = r["state"]["messages"]
        checks = [
            check("Edit opens the question in place, ready to type", r["shown"] == {"value": "second question", "focused": True, "editing": True}, r["shown"]),
            check("sending it replaces that question and drops what came after",
                  [x["role"] for x in msgs] == ["user", "assistant", "user", "assistant"]
                  and msgs[0]["text"].startswith("first question") and msgs[2]["text"].startswith("HISTORY edited second")
                  and not any("third question" in x["text"] for x in msgs), msgs),
            check("the agent forks its session at the answer before the edited question",
                  fork.get("sessionId") == start["session"] and fork.get("_meta") == {"jetbrains": {"air": {"fork": {"version": 1, "messageId": start["answers"][0]}}}},
                  {"fork": fork, "answers": start["answers"]}),
            check("the fork keeps the reading instructions", "reading assistant" in json.dumps(fork.get("systemPrompt")), fork.get("systemPrompt")),
            check("the edited question goes to the fork, which holds only what came before it, page included",
                  prompt.get("sessionId") == forked and forked != start["session"] and "History: first question | resources=0" in msgs[-1]["text"],
                  {"prompt": prompt.get("sessionId"), "forked": forked, "answer": msgs[-1]}),
            check("the session the question was in is closed", start["session"] in closed, closed[-3:]),
        ]
        # The first question: nothing before it to keep.
        news, fork_count = len(agent.messages("session/new")), len(agent.messages("session/fork"))
        r = m.chrome("return await editQuestion('first question', 'HISTORY edited first');")
        msgs = r["state"]["messages"]
        checks.append(check("editing the first question starts a new session, which gets the page again",
                            len(agent.messages("session/new")) == news + 1 and len(agent.messages("session/fork")) == fork_count
                            and len(msgs) == 2 and "History: (none) | resources=1" in msgs[-1]["text"], msgs))
        # Escape: nothing changes.
        prompts = len(agent.messages("session/prompt"))
        r = m.chrome(r"""
const before = panelState().messages;
const r = await editQuestion('HISTORY edited first', 'never sent', 'Escape');
const doc = panelDoc();
return { before, after: r.state.messages, editor: !!doc.querySelector('.editor'), focus: doc.activeElement?.id ?? null };
""")
        checks.append(check("Escape closes the editor and changes nothing",
                            r["before"] == r["after"] and not r["editor"] and r["focus"] == "input" and len(agent.messages("session/prompt")) == prompts, r))
        # Files go with the edited question as they were.
        r = m.chrome(r"""
await attachVia('picker', [panelFile(['MARKER-EDIT-FILE'], 'kept.txt', 'text/plain')]);
await filesRead(1);
await ask('about the file');
return (await editQuestion('about the file', 'HISTORY about the file, edited')).state.messages;
""")
        blocks = (agent.last_prompt() or {"prompt": []})["prompt"]
        user = next((x for x in reversed(r) if x["role"] == "user"), {})
        checks.append(check("the files go with the edited question",
                            any(b.get("type") == "resource" and "MARKER-EDIT-FILE" in b["resource"]["text"] for b in blocks)
                            and "kept.txt" in user.get("text", "") and "History: HISTORY edited first | resources=1" in r[-1]["text"],
                            {"blocks": [b.get("type") for b in blocks], "messages": r[-2:]}))
        # The agent stopped after sitting unused: it starts again and forks
        # the closed session from its transcript.
        r = m.chrome(r"""
const S = glue().AgentService;
const conn = S.conns.get('codex');
const session = conv().sessionId;
S.sweep(Date.now() + 11 * 60000);
await waitFor(() => conn.exited, 'the agent to stop', 5000);
const r = await editQuestion('HISTORY about the file, edited', 'HISTORY after the break');
return { session, state: r.state };
""")
        fork = agent.messages("session/fork")[-1]["params"]
        msgs = r["state"]["messages"]
        checks.append(check("after the agent stopped, an edit still goes back, in the restarted agent",
                            fork["sessionId"] == r["session"] and "History: HISTORY edited first | resources=1" in msgs[-1]["text"]
                            and not any(x["role"] in ("note", "error") for x in msgs), {"fork": fork["sessionId"], "session": r["session"], "messages": msgs}))
        # An agent that cannot fork: the edited question starts over, and says so.
        r = m.chrome(r"""
await choose('harness', 'claude');
await ask('claude one');
await ask('claude two');
return (await editQuestion('claude two', 'HISTORY claude edited')).state.messages;
""")
        note = next((x for x in r if x["role"] == "note"), None)
        checks.append(check("an agent that cannot fork starts over without the earlier messages, and says so",
                            note and "could not go back" in note["text"] and "History: (none) | resources=1" in r[-1]["text"]
                            and not ctx["claude"].messages("session/fork"), r))
        return checks

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
        ("attachments_reach_the_agent", attachments_reach_the_agent),
        ("attachments_wait_switch_and_trim", attachments_wait_switch_and_trim),
        ("page_as_seen_and_its_changes", page_as_seen_and_its_changes),
        ("shortcut_does_not_wait_for_busy_frames", shortcut_does_not_wait_for_busy_frames),
        ("typing_starts_a_stopped_agent", typing_starts_a_stopped_agent),
        ("answers_can_be_copied_and_saved", answers_can_be_copied_and_saved),
        ("editing_a_question", editing_a_question),
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
                # An attached image: the agent reads the word drawn in it.
                r3 = m.chrome(r"""
const doc = panelDoc();
await attachVia('paste', [await wordImage('PELICAN', 1200, 400)]);
await filesRead(1);
const started = Date.now();
const input = doc.getElementById('input');
input.value = 'What single word is written in the attached image? Reply with only that word.';
input.dispatchEvent(new doc.defaultView.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await waitFor(() => !conv().busy, 'real answer', 180000);
await sleep(200);
const state = panelState();
return { seconds: (Date.now() - started) / 1000, last: state.messages[state.messages.length - 1] };
""")
                print(f"  {harness} read an attached image in {r3['seconds']:.1f}s: {r3['last']['text'][:120]!r}")
                checks.append(check(f"real {harness} reads an attached image", r3["last"]["role"] == "assistant" and "PELICAN" in r3["last"]["text"].upper(), r3))
                # An edited question: the agent forgets it and what came after.
                r4 = m.chrome(r"""
await ask('Reply with exactly the word ALPHA and nothing else.', 180000);
await ask('Reply with exactly the word BETA and nothing else.', 180000);
const started = Date.now();
const r = await editQuestion('Reply with exactly the word BETA and nothing else.',
  'List every reply you have given so far in this conversation, one per line, nothing else.', 'Enter', 180000);
const messages = r.state.messages;
return { seconds: (Date.now() - started) / 1000, last: messages[messages.length - 1],
         notes: messages.filter(x => x.role === 'note' || x.role === 'error').map(x => x.text) };
""")
                print(f"  {harness} answered an edited question in {r4['seconds']:.1f}s: {r4['last']['text'][:200]!r}")
                text = r4["last"]["text"].upper()
                checks.append(check(f"real {harness} forgets an edited question and what came after it",
                                    r4["last"]["role"] == "assistant" and "ALPHA" in text and "BETA" not in text and not r4["notes"], r4))
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
    make_pdf(www / "blank.pdf", [[], []])  # like a scan: pages without text
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
