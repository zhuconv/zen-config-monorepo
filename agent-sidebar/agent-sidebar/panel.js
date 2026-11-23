/* Agent Sidebar, the panel inside Zen's sidebar. It shows the conversation of
 * the selected tab, lets the user pick the agent, model and effort, takes
 * attached files, sends questions through agent-sidebar.uc.js, lets the user
 * edit a question and ask again from there, and copies or saves answers. */

import { marked } from "./vendor/marked.esm.js";
import { imageFromClipboard, kindOf, readAttachment, Refused } from "./attachments.mjs";
import { copyText, extensionFor, fileName, saveText } from "./saving.mjs";

const { AgentService, MODEL_OPTION, EFFORT_OPTIONS, optionValues } = ChromeUtils.importESModule(
  new URL("AgentService.sys.mjs", import.meta.url).href
);
const glue = window.browsingContext.topChromeWindow.ucAgentSidebar;
const parser = Cc["@mozilla.org/parserutils;1"].getService(Ci.nsIParserUtils);
const BLANK = Services.io.newURI("about:blank");
const PREF_MAX_CHARS = "uc.agent-sidebar.max-page-chars";
const PLACEHOLDER = "Ask about this page…";

const $ = id => document.getElementById(id);
const els = {
  messages: $("messages"),
  empty: $("empty"),
  harness: $("harness"),
  model: $("model"),
  effort: $("effort"),
  input: $("input"),
  send: $("send"),
  stop: $("stop"),
  chip: $("chip"),
  chipText: $("chip-text"),
  attachments: $("attachments"),
  attach: $("attach"),
  fileInput: $("file-input"),
};

let conv = null;
let unsubscribe = null;
let editing = null; // the question being edited: { message, el }
let waitingFor = null; // the conversation whose files are still being read for a send
let harnesses = AgentService.harnessList();
// Each conversation keeps its own list of message elements while the panel is
// open, so switching tabs swaps lists instead of rendering every answer again.
const lists = new WeakMap(); // conversation -> <div class="list">
const rendered = new WeakMap(); // message -> { el, v }
const messageOf = new WeakMap(); // message element -> message
// While an answer streams, render at most this often (the Markdown of the
// whole answer is parsed again each time).
const RENDER_INTERVAL = 50;
let renderTimer = null;
let lastRender = 0;

// --- Rendering --------------------------------------------------------------

/** Markdown from the model, through Firefox's HTML sanitizer: no scripts,
 * no event handlers, no forms or media. */
function renderMarkdown(el, markdown) {
  const html = marked.parse(markdown || "", { gfm: true, async: false });
  const fragment = parser.parseFragment(
    html,
    Ci.nsIParserUtils.SanitizerDropForms | Ci.nsIParserUtils.SanitizerDropMedia,
    false,
    BLANK,
    el
  );
  el.replaceChildren(fragment);
}

function div(className, text) {
  const el = document.createElement("div");
  el.className = className;
  if (text !== undefined) {
    el.textContent = text;
  }
  return el;
}

const TOOL_ICONS = { search: "🔍", fetch: "🌐", read: "📄", execute: "⌘", edit: "✎", think: "…" };
const FILE_ICONS = { image: "🖼", pdf: "📕", text: "📄" };

/** An attached file: its thumbnail, or an icon for its kind. */
function fileBadge(file, className) {
  const el = div(className);
  el.title = file.info ? `${file.name} · ${file.info}` : file.name;
  if (file.thumb) {
    const img = document.createElement("img");
    img.src = file.thumb;
    img.alt = "";
    el.append(img);
  } else {
    el.append(div("icon", FILE_ICONS[file.kind] ?? FILE_ICONS.text));
  }
  return el;
}

function listFor(c) {
  let list = lists.get(c);
  if (!list) {
    list = div("list");
    lists.set(c, list);
  }
  return list;
}

/** Icon buttons, [action, title] each: copy and save for a whole answer or one
 * code block, edit for a question. */
function actionRow(buttons) {
  const row = div("actions");
  for (const [action, title] of buttons) {
    const button = document.createElement("button");
    button.className = `icon ${action}`;
    button.dataset.action = action;
    button.title = title;
    button.setAttribute("aria-label", title);
    row.append(button);
  }
  return row;
}

function languageOf(code) {
  return code?.className.match(/(?:^|\s)language-(\S+)/)?.[1] ?? "";
}

/** Give each code block of a finished answer its own buttons. */
function addCodeActions(body) {
  for (const pre of body.querySelectorAll("pre")) {
    const block = div("code");
    pre.replaceWith(block);
    const extension = extensionFor(languageOf(pre.querySelector("code")));
    block.append(pre, actionRow([["copy", "Copy the code"], ["save", `Save as .${extension}`]]));
  }
}

/** A question: the passage it is about, its files, its text, and a button
 * to edit it. */
function fillQuestion(el, message) {
  const parts = [];
  if (message.selection) {
    parts.push(div("selection", message.selection));
  }
  if (message.files?.length) {
    const row = div("files");
    for (const file of message.files) {
      const badge = fileBadge(file, `file ${file.kind}`);
      badge.append(div("name", file.name));
      row.append(badge);
    }
    parts.push(row);
  }
  parts.push(div("text", message.text));
  if (message.attached) {
    const what = message.attached.kind === "pdf" ? "PDF" : "Page";
    const how = message.attached.update ? "updated" : "attached";
    const cut = message.attached.truncated ? " (truncated)" : "";
    parts.push(div("meta", `${what} ${how}${cut} · ${message.page?.title ?? ""}`));
  }
  parts.push(actionRow([["edit", "Edit"]]));
  el.replaceChildren(...parts);
}

function renderMessage(message) {
  let entry = rendered.get(message);
  if (!entry) {
    entry = { el: div(`msg ${message.role}`), v: 0 };
    rendered.set(message, entry);
    messageOf.set(entry.el, message);
    listFor(conv).append(entry.el);
  }
  if (entry.v === message.v) {
    return;
  }
  entry.v = message.v;
  const { el } = entry;
  switch (message.role) {
    case "user":
      if (editing?.message !== message) {
        fillQuestion(el, message); // an open editor shows it again when it closes
      }
      break;
    case "assistant": {
      // The buttons come once the answer is complete: while it streams, it
      // is parsed again every 50 ms.
      const body = el.firstChild ?? el.appendChild(div("body"));
      renderMarkdown(body, message.text);
      el.dataset.state = message.state;
      if (message.state !== "streaming") {
        addCodeActions(body);
        if (!body.nextSibling) {
          el.append(actionRow([["copy", "Copy the Markdown"], ["save", "Save as .md"]]));
        }
      }
      break;
    }
    case "tool":
      el.textContent = `${TOOL_ICONS[message.kind] ?? "•"} ${message.title}`;
      el.dataset.status = message.status;
      break;
    case "permission": {
      const row = div("options");
      if (message.answer === null) {
        for (const option of message.options) {
          const button = document.createElement("button");
          button.textContent = option.name;
          button.dataset.kind = option.kind;
          button.addEventListener("click", () =>
            AgentService.answerPermission(conv, message, option.id)
          );
          row.append(button);
        }
      } else {
        const chosen = message.options.find(o => o.id === message.answer);
        row.textContent = chosen ? `→ ${chosen.name}` : "Not allowed";
      }
      el.replaceChildren(div("title", `The agent wants to: ${message.title}`), row);
      break;
    }
    default:
      el.textContent = message.text;
  }
}

function nearBottom() {
  const m = els.messages;
  return m.scrollHeight - m.scrollTop - m.clientHeight < 40;
}

function renderMessages() {
  clearTimeout(renderTimer);
  renderTimer = null;
  lastRender = Date.now();
  if (!conv) {
    return;
  }
  const stick = nearBottom();
  for (const message of conv.messages) {
    renderMessage(message);
  }
  if (stick) {
    els.messages.scrollTop = els.messages.scrollHeight;
  }
  renderStatus();
}

function scheduleRender() {
  if (!renderTimer) {
    const wait = Math.max(0, lastRender + RENDER_INTERVAL - Date.now());
    renderTimer = setTimeout(() => window.requestAnimationFrame(renderMessages), wait);
  }
}

/** Fill a <select> from an ACP config option (flat or grouped values). */
function fillSelect(select, option, value) {
  select.replaceChildren();
  const add = (parent, o) => {
    const el = document.createElement("option");
    el.value = o.value;
    el.textContent = o.name || o.value;
    parent.append(el);
  };
  for (const o of option.options ?? []) {
    if (Array.isArray(o.options)) {
      const group = document.createElement("optgroup");
      group.label = o.name;
      o.options.forEach(x => add(group, x));
      select.append(group);
    } else {
      add(select, o);
    }
  }
  select.value = value;
  select.dataset.optionId = option.id;
  select.title = `${option.name || option.id}: ${select.selectedOptions[0]?.textContent ?? value}`;
}

function renderHeader() {
  if (!conv) {
    return;
  }
  // Agents: from config.json.
  if (els.harness.options.length !== harnesses.length) {
    els.harness.replaceChildren(
      ...harnesses.map(h => {
        const el = document.createElement("option");
        el.value = h.id;
        el.textContent = h.name;
        return el;
      })
    );
  }
  els.harness.value = conv.harness;
  els.harness.hidden = harnesses.length === 0;

  // Model and effort: the session's own settings, or, before the session
  // exists, the last ones this agent reported, showing the user's choice.
  const options = conv.configOptions ?? AgentService.options.get(conv.harness) ?? [];
  const live = !!conv.configOptions;
  const show = (select, option) => {
    select.hidden = !option;
    if (option) {
      const wanted = live ? option.currentValue : AgentService.preferredValue(conv.harness, option.id);
      fillSelect(select, option, optionValues(option).includes(wanted) ? wanted : option.currentValue);
    }
  };
  show(els.model, options.find(o => o.id === MODEL_OPTION));
  show(els.effort, options.find(o => EFFORT_OPTIONS.includes(o.id)));
  for (const select of [els.harness, els.model, els.effort]) {
    select.disabled = !!conv.busy;
  }
}

/** While the agent works, the empty question box says what it is doing and
 * Send turns into Stop. */
function renderStatus() {
  const busy = !!conv?.busy;
  els.send.hidden = busy;
  els.stop.hidden = !busy;
  els.input.placeholder = busy
    ? conv.status || "Thinking…"
    : waitingFor && waitingFor === conv
      ? "Reading the files…"
      : PLACEHOLDER;
  document.body.classList.toggle("busy", busy);
  els.empty.hidden = !!conv?.messages.length;
  for (const select of [els.harness, els.model, els.effort]) {
    select.disabled = busy;
  }
  updateSend();
}

/** Send is dimmed while there is nothing to send. */
function updateSend() {
  const draft = conv?.draft;
  els.send.disabled = !els.input.value.trim() && !draft?.selection && !draft?.attachments?.length;
}

function renderChip() {
  const selection = conv?.draft?.selection ?? "";
  els.chip.hidden = !selection;
  els.chipText.textContent = selection;
  updateSend();
}

/** The files waiting in the composer, each with a ✕ to take it out. */
function renderAttachments() {
  const items = conv?.draft?.attachments ?? [];
  els.attachments.hidden = !items.length;
  updateSend();
  els.attachments.replaceChildren(
    ...items.map(item => {
      const el = fileBadge(item, `attachment ${item.state}`);
      if (item.error) {
        el.title = `${item.name}: ${item.error}`;
      }
      const label = div("label");
      label.append(div("name", item.name), div("info", item.state === "reading" ? "Reading…" : item.error || item.info));
      const remove = document.createElement("button");
      remove.className = "icon remove";
      remove.title = "Remove";
      remove.textContent = "✕";
      remove.addEventListener("click", () => {
        const list = conv?.draft?.attachments ?? [];
        if (list.includes(item)) {
          list.splice(list.indexOf(item), 1);
        }
        renderAttachments();
        els.input.focus();
      });
      el.append(label, remove);
      return el;
    })
  );
}

function renderAll() {
  els.messages.replaceChildren(listFor(conv));
  renderHeader();
  renderChip();
  renderAttachments();
  renderMessages();
  els.messages.scrollTop = els.messages.scrollHeight;
}

function onChange(change) {
  if (change.type === "reset") {
    editing = null;
    listFor(conv).replaceChildren();
    renderAll();
  } else if (change.type === "truncate") {
    // An edited question: it and what came after it go.
    for (const message of change.dropped) {
      rendered.get(message)?.el.remove();
    }
  } else if (change.type === "session" || change.type === "options") {
    renderHeader();
  } else if (change.type === "status" || change.type === "busy") {
    if (conv?.busy) {
      stopEditing();
    }
    renderStatus();
  } else {
    scheduleRender();
  }
}

// --- Composer ---------------------------------------------------------------

function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
}

function saveDraft() {
  if (conv) {
    const text = els.input.value;
    const selection = conv.draft?.selection ?? "";
    const attachments = conv.draft?.attachments ?? [];
    conv.draft = text || selection || attachments.length ? { text, selection, attachments } : null;
  }
}

/** Attach files to the shown conversation's next question. They are read
 * right away, while the user types. */
function addFiles(files) {
  if (!conv || !files.length) {
    return;
  }
  const target = conv;
  target.draft = { text: els.input.value, selection: "", ...target.draft };
  target.draft.attachments ??= [];
  const maxChars = Services.prefs.getIntPref(PREF_MAX_CHARS, 120000);
  for (const file of files) {
    const item = { name: file.name || "file", kind: kindOf(file), state: "reading", info: "", error: "" };
    item.ready = readAttachment(file, { maxChars })
      .then(
        read => Object.assign(item, read, { state: "ready" }),
        e => {
          if (!(e instanceof Refused)) {
            console.error("[agent-sidebar] could not read", item.name, e);
          }
          Object.assign(item, { state: "error", error: e instanceof Refused ? e.message : "could not read it" });
        }
      )
      .then(() => conv === target && renderAttachments());
    target.draft.attachments.push(item);
  }
  renderAttachments();
}

async function submit() {
  if (!conv || conv.busy || waitingFor === conv) {
    return;
  }
  const target = conv;
  const items = target.draft?.attachments ?? [];
  if (items.some(item => item.state === "reading")) {
    waitingFor = target;
    renderStatus();
    try {
      await Promise.all(items.map(item => item.ready));
    } finally {
      waitingFor = null;
    }
    if (conv !== target) {
      return; // another tab is shown now; the files stay with this one
    }
    renderStatus();
    if (target.busy) {
      return;
    }
  }
  const selection = target.draft?.selection ?? "";
  const attachments = items
    .filter(item => item.state === "ready")
    .map(({ state, error, ready, ...attachment }) => attachment);
  const question = els.input.value.trim() || (selection || attachments.length ? glue.defaultQuestion() : "");
  if (!question) {
    return;
  }
  els.input.value = "";
  autoGrow();
  target.draft = null;
  renderChip();
  renderAttachments();
  await glue.submit(target, { question, selection, attachments });
}

/** Typing a question starts the agent and the session if they were stopped
 * (after sitting unused) or closed, so they are ready by the time it is sent. */
function warmUp() {
  if (conv && !conv.busy && (!conv.sessionId || !conv.conn || conv.conn.exited)) {
    AgentService.prewarm(conv);
  }
}

els.input.addEventListener("input", () => {
  autoGrow();
  updateSend();
  warmUp();
});
els.input.addEventListener("focus", warmUp);
els.input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submit();
  } else if (event.key === "Escape") {
    event.preventDefault();
    glue.focusPage();
  }
});
els.send.addEventListener("click", submit);
els.stop.addEventListener("click", () => conv && AgentService.cancel(conv));
$("chip-remove").addEventListener("click", () => {
  if (conv?.draft) {
    conv.draft.selection = "";
  }
  renderChip();
  els.input.focus();
});

// Files: the + button, a paste (a screenshot, or files copied in Finder), or
// a drop anywhere on the panel.
els.attach.addEventListener("click", () => els.fileInput.click());
els.fileInput.addEventListener("change", () => {
  addFiles([...els.fileInput.files]);
  els.fileInput.value = "";
  els.input.focus();
});
els.input.addEventListener("paste", event => {
  const data = event.clipboardData;
  if (!data) {
    return;
  }
  let files = [...data.files]; // files copied in Finder
  // An image, unless the paste is really text that also has a picture of
  // itself (some apps add one). "Copy Image" adds the image's URL as text.
  const text = data.getData("text/plain");
  if (!files.length && (!text.trim() || /^\s*(https?|data|blob|file):\S*\s*$/.test(text))) {
    try {
      const image = imageFromClipboard(data);
      files = image ? [image] : [];
    } catch (e) {
      console.error("[agent-sidebar] could not read the pasted image", e);
    }
  }
  if (files.length) {
    event.preventDefault();
    addFiles(files);
  }
});
let dragDepth = 0;
const carriesFiles = event => [...(event.dataTransfer?.types ?? [])].includes("Files");
function endDrag() {
  dragDepth = 0;
  document.body.classList.remove("dragging");
}
document.addEventListener("dragenter", event => {
  if (carriesFiles(event)) {
    event.preventDefault();
    dragDepth++;
    document.body.classList.add("dragging");
  }
});
document.addEventListener("dragover", event => {
  if (carriesFiles(event)) {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }
});
document.addEventListener("dragleave", event => {
  if (carriesFiles(event) && --dragDepth <= 0) {
    endDrag();
  }
});
document.addEventListener("drop", event => {
  if (carriesFiles(event)) {
    event.preventDefault();
    endDrag();
    addFiles([...event.dataTransfer.files]);
    els.input.focus();
  }
});
$("new-chat").addEventListener("click", () => {
  if (conv) {
    AgentService.reset(conv);
    els.input.value = "";
    autoGrow();
    els.input.focus();
  }
});
$("close").addEventListener("click", () => glue.closePanel());

els.harness.addEventListener("change", () => {
  if (conv) {
    AgentService.setHarness(conv, els.harness.value);
    els.input.focus();
  }
});
for (const select of [els.model, els.effort]) {
  select.addEventListener("change", () => {
    if (conv && select.dataset.optionId) {
      AgentService.setOption(conv, select.dataset.optionId, select.value);
    }
  });
}

// --- Editing a question -------------------------------------------------------

/** Edit a question in place. Sending it asks again from there: the question
 * and everything after it go, and the agent forgets them (AgentService.send).
 * The passage and the files go with it as they were. */
function startEditing(el) {
  const message = messageOf.get(el);
  if (!conv || conv.busy || !message) {
    return;
  }
  stopEditing();
  editing = { message, el };
  const box = document.createElement("textarea");
  box.rows = 1;
  box.value = message.text;
  box.setAttribute("aria-label", "Edit the question");
  const cancel = document.createElement("button");
  cancel.className = "cancel";
  cancel.textContent = "Cancel";
  const send = document.createElement("button");
  send.className = "send";
  send.textContent = "Send";
  const buttons = div("buttons");
  buttons.append(cancel, send);
  const editor = div("editor");
  editor.append(box, buttons);
  el.querySelector(":scope > .text").replaceWith(editor);
  el.querySelector(":scope > .meta")?.remove();
  el.querySelector(":scope > .actions")?.remove();
  el.classList.add("editing");
  const grow = () => {
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, 300)}px`;
  };
  box.addEventListener("input", grow);
  box.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendEdit(box.value);
    } else if (event.key === "Escape") {
      event.preventDefault();
      stopEditing();
      els.input.focus();
    }
  });
  cancel.addEventListener("click", () => {
    stopEditing();
    els.input.focus();
  });
  send.addEventListener("click", () => sendEdit(box.value));
  grow();
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
}

/** Close the editor; the question shows as it was. */
function stopEditing() {
  if (!editing) {
    return;
  }
  const { message, el } = editing;
  editing = null;
  el.classList.remove("editing");
  fillQuestion(el, message);
}

function sendEdit(text) {
  const message = editing?.message;
  if (!message || !conv || conv.busy) {
    return;
  }
  const attachments = message.attachments ?? [];
  const question = text.trim() || (message.selection || attachments.length ? glue.defaultQuestion() : "");
  if (!question) {
    return;
  }
  editing = null; // the question's element goes with it
  els.input.focus();
  glue.submit(conv, { question, selection: message.selection, attachments, replace: message });
}

// --- Copying and saving answers ---------------------------------------------

/** The page of the question an answer replies to. */
function pageBefore(message) {
  const i = conv?.messages.indexOf(message) ?? -1;
  return i < 0 ? null : (conv.messages.slice(0, i).findLast(m => m.role === "user")?.page ?? null);
}

const feedbackTimers = new WeakMap();

/** Say what a button did, next to it, for a moment. A check mark replaces
 * its icon unless it failed. */
function flash(row, action, text, failed = false) {
  clearTimeout(feedbackTimers.get(row));
  if (failed) {
    delete row.dataset.done;
  } else {
    row.dataset.done = action;
  }
  row.dataset.feedback = text;
  row.classList.toggle("failed", failed);
  feedbackTimers.set(
    row,
    setTimeout(() => {
      delete row.dataset.done;
      delete row.dataset.feedback;
      row.classList.remove("failed");
    }, 2500)
  );
}

/** Copy or save an answer's Markdown, or one of its code blocks. */
async function act(button) {
  const row = button.closest(".actions");
  const message = messageOf.get(row.closest(".msg"));
  const block = row.closest(".code");
  const action = button.dataset.action;
  let text, extension;
  if (block) {
    const code = block.querySelector("pre code") ?? block.querySelector("pre");
    text = code.textContent.replace(/\n$/, "");
    extension = extensionFor(languageOf(code));
  } else {
    text = message.text.trim();
    extension = "md";
  }
  try {
    if (action === "copy") {
      copyText(window, text);
      flash(row, action, "Copied");
    } else {
      const page = pageBefore(message);
      const path = await saveText(window, {
        name: fileName(text, extension, page?.title),
        text: `${text}\n`,
        source: page?.url,
      });
      if (path) {
        flash(row, action, `Saved “${PathUtils.filename(path)}”`);
      }
    }
  } catch (e) {
    console.error(`[agent-sidebar] could not ${action} it`, e);
    flash(row, action, action === "copy" ? "Could not copy it" : "Could not save it", true);
  }
}

els.messages.addEventListener("click", event => {
  const button = event.target.closest(".actions button");
  if (button) {
    if (button.dataset.action === "edit") {
      startEditing(button.closest(".msg"));
    } else {
      act(button);
    }
    return;
  }
  // Links in answers open in a new tab, never inside the panel.
  const link = event.target.closest("a[href]");
  if (link) {
    event.preventDefault();
    glue.openLink(link.href);
  }
});

// --- Wiring to the window -----------------------------------------------------

const api = {
  /** Show a conversation (the selected tab's). */
  show(next) {
    if (conv === next) {
      return;
    }
    stopEditing();
    saveDraft();
    unsubscribe?.();
    conv = next;
    unsubscribe = conv.subscribe(onChange);
    els.input.value = conv.draft?.text ?? "";
    autoGrow();
    renderAll();
  },
  /** The shortcut: show the conversation with the selection ready. */
  compose(next, { selection }) {
    this.show(next);
    if (selection) {
      conv.draft = { ...conv.draft, text: els.input.value, selection };
      renderChip();
    }
    els.input.focus();
  },
};

window.addEventListener("unload", () => {
  saveDraft();
  unsubscribe?.();
  glue?.detachPanel(api);
});

AgentService.harnesses()
  .then(list => {
    harnesses = list;
    renderHeader();
  })
  .catch(e => console.error("[agent-sidebar]", e));

glue?.attachPanel(api);
