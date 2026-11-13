/* Agent Sidebar, the panel inside Zen's sidebar. It shows the conversation of
 * the selected tab, lets the user pick the agent, model and effort, and sends
 * questions through agent-sidebar.uc.js. */

import { marked } from "./vendor/marked.esm.js";

const { AgentService, MODEL_OPTION, EFFORT_OPTIONS, optionValues } = ChromeUtils.importESModule(
  new URL("AgentService.sys.mjs", import.meta.url).href
);
const glue = window.browsingContext.topChromeWindow.ucAgentSidebar;
const parser = Cc["@mozilla.org/parserutils;1"].getService(Ci.nsIParserUtils);
const BLANK = Services.io.newURI("about:blank");

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
  status: $("status"),
  chip: $("chip"),
  chipText: $("chip-text"),
};

let conv = null;
let unsubscribe = null;
let harnesses = AgentService.harnessList();
// Each conversation keeps its own list of message elements while the panel is
// open, so switching tabs swaps lists instead of rendering every answer again.
const lists = new WeakMap(); // conversation -> <div class="list">
const rendered = new WeakMap(); // message -> { el, v }
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

function listFor(c) {
  let list = lists.get(c);
  if (!list) {
    list = div("list");
    lists.set(c, list);
  }
  return list;
}

function renderMessage(message) {
  let entry = rendered.get(message);
  if (!entry) {
    entry = { el: div(`msg ${message.role}`), v: 0 };
    rendered.set(message, entry);
    listFor(conv).append(entry.el);
  }
  if (entry.v === message.v) {
    return;
  }
  entry.v = message.v;
  const { el } = entry;
  switch (message.role) {
    case "user": {
      const parts = [];
      if (message.selection) {
        parts.push(div("selection", message.selection));
      }
      parts.push(div("text", message.text));
      if (message.attached) {
        const what = message.attached.kind === "pdf" ? "PDF" : "Page";
        const cut = message.attached.truncated ? " (truncated)" : "";
        parts.push(div("meta", `${what} attached${cut} · ${message.page?.title ?? ""}`));
      }
      el.replaceChildren(...parts);
      break;
    }
    case "assistant":
      renderMarkdown(el, message.text);
      el.dataset.state = message.state;
      break;
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

function renderStatus() {
  const busy = !!conv?.busy;
  els.send.hidden = busy;
  els.stop.hidden = !busy;
  els.status.textContent = conv?.status ?? "";
  els.empty.hidden = !!conv?.messages.length;
  for (const select of [els.harness, els.model, els.effort]) {
    select.disabled = busy;
  }
}

function renderChip() {
  const selection = conv?.draft?.selection ?? "";
  els.chip.hidden = !selection;
  els.chipText.textContent = selection;
}

function renderAll() {
  els.messages.replaceChildren(listFor(conv));
  renderHeader();
  renderChip();
  renderMessages();
  els.messages.scrollTop = els.messages.scrollHeight;
}

function onChange(change) {
  if (change.type === "reset") {
    listFor(conv).replaceChildren();
    renderAll();
  } else if (change.type === "session" || change.type === "options") {
    renderHeader();
  } else if (change.type === "status" || change.type === "busy") {
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
    conv.draft = text || selection ? { text, selection } : null;
  }
}

async function submit() {
  if (!conv || conv.busy) {
    return;
  }
  const selection = conv.draft?.selection ?? "";
  const question = els.input.value.trim() || (selection ? glue.defaultQuestion() : "");
  if (!question) {
    return;
  }
  els.input.value = "";
  autoGrow();
  conv.draft = null;
  renderChip();
  await glue.submit(conv, { question, selection });
}

els.input.addEventListener("input", autoGrow);
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

// Links in answers open in a new tab, never inside the panel.
els.messages.addEventListener("click", event => {
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
      conv.draft = { text: els.input.value, selection };
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
