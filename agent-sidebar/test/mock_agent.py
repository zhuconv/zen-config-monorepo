#!/usr/bin/env python3
"""A tiny ACP agent for the tests.

    mock_agent.py LOG [--claude] [--slow-start SECONDS]

It writes every message it receives to LOG (one JSON object per line). By
default it looks like codex-acp: settings `model` and `reasoning_effort`, a
system prompt it appends, and sessions it can close, resume (any session id,
as if from a transcript on disk) and fork at a message. With --claude it looks
like claude-agent-acp, minus resuming and forking: settings `model` and
`effort`, and a system prompt it can only replace. --slow-start delays the
answer to `initialize`.

Each message of an answer has an id (`messageId`). Each session's questions
and message ids are kept in LOG.history.json, like transcripts on disk, so a
restarted agent can fork a session too. session/fork takes the fork point the
way both real adapters do, in `_meta.jetbrains.air.fork.messageId`: the new
session has the turns up to the one with that message.

Prompts are answered according to a keyword in the prompt text:

  CRASH       exit at once
  SLOW        stream 60 words, 0.1 s apart, until cancelled
  PERMISSION  ask the client for permission and report the answer
  MARKDOWN    answer with Markdown that also tries to inject a script
  DOCUMENT    answer with a Markdown file and a Python script in code blocks
  TOOL        say something, report a tool call, then answer
  HISTORY     list the questions the session had before this one, and the
              number of attached resources
  (anything else)  echo the question, the number of attached resources and
                   the session's model and effort, then the number of
                   images when there are any
"""

import json
import os
import sys
import threading
import time

LOG = sys.argv[1]
CLAUDE = "--claude" in sys.argv
SLOW_START = float(sys.argv[sys.argv.index("--slow-start") + 1]) if "--slow-start" in sys.argv else 0
EFFORT = "effort" if CLAUDE else "reasoning_effort"
MODELS = [("opus", "Opus"), ("sonnet", "Sonnet")] if CLAUDE else [("mock-model", "Mock Model"), ("mock-fast", "Mock Fast")]
EFFORTS = ["low", "medium", "high", "xhigh"]
DOCUMENT = (
    "Here is the file:\n\n"
    "```markdown\n# Reading notes: **Mamba**\n\n- selective state spaces\n- `linear` time\n```\n\n"
    "And a script:\n\n"
    "```python\nprint(\"hi\")\n```\n"
)

write_lock = threading.Lock()
sessions = {}  # session id -> {"model": ..., "effort": ...}
cancelled = set()
waiting = {}  # request id -> {"event": Event, "result": ...}
next_id = [1000]
HISTORY = LOG + ".history.json"
history_lock = threading.Lock()
histories = {}  # session id -> [{"question": ..., "messages": [message ids]}]
counters = {"message": 0, "fork": 0}


def load_histories():
    try:
        with open(HISTORY) as f:
            histories.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_histories():
    with open(HISTORY, "w") as f:
        json.dump(histories, f)


def new_message(session):
    """An id for a message of the answer, kept in the session's history."""
    with history_lock:
        counters["message"] += 1
        message = f"m{os.getpid()}-{counters['message']}"
        histories[session][-1]["messages"].append(message)
        save_histories()
    return message


def fork(params):
    """session/fork: the source's turns up to the one with the fork point's
    message. Returns (session, error)."""
    source = params["sessionId"]
    point = (((params.get("_meta") or {}).get("jetbrains") or {}).get("air") or {}).get("fork") or {}
    with history_lock:
        turns = histories.get(source)
        if turns is None:
            return None, {"code": -32002, "message": f"Session {source} not found"}
        if point.get("messageId"):
            end = next((i for i, t in enumerate(turns) if point["messageId"] in t["messages"]), None)
            if end is None:
                return None, {"code": -32602, "message": f"Fork point message {point['messageId']} was not found in session {source}"}
            turns = turns[:end + 1]
        counters["fork"] += 1
        session = f"s{os.getpid()}-f{counters['fork']}"
        histories[session] = json.loads(json.dumps(turns))
        save_histories()
    sessions[session] = dict(sessions.get(source) or {"model": MODELS[0][0], "effort": "low"})
    return session, None


def send(message):
    with write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def log(message):
    with open(LOG, "a") as f:
        f.write(json.dumps(message) + "\n")


def options(session):
    state = sessions[session]
    return [
        {"id": "mode", "name": "Mode", "type": "select", "currentValue": "default",
         "options": [{"value": "default", "name": "Default"}]},
        {"id": "model", "name": "Model", "type": "select", "currentValue": state["model"],
         "options": [{"value": v, "name": n} for v, n in MODELS]},
        {"id": EFFORT, "name": "Effort", "type": "select", "currentValue": state["effort"],
         "options": [{"value": v, "name": v} for v in EFFORTS]},
    ]


def update(session, data):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session, "update": data}})


def say(session, text, size=12, message=None):
    """One message of the answer, streamed in chunks."""
    message = message or new_message(session)
    for i in range(0, len(text), size):
        update(session, {"sessionUpdate": "agent_message_chunk", "messageId": message, "content": {"type": "text", "text": text[i:i + size]}})


def prompt(request):
    session = request["params"]["sessionId"]
    blocks = request["params"]["prompt"]
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    resources = [b for b in blocks if b.get("type") == "resource"]
    images = [b for b in blocks if b.get("type") == "image"]
    question = text.strip().splitlines()[-1] if text.strip() else ""
    with history_lock:
        turns = histories.setdefault(session, [])
        earlier = [t["question"] for t in turns]
        turns.append({"question": question, "messages": []})
        save_histories()
    cancelled.discard(session)
    stop = "end_turn"
    if "CRASH" in text:
        os._exit(3)
    elif "SLOW" in text:
        message = new_message(session)
        for i in range(60):
            if session in cancelled:
                stop = "cancelled"
                break
            say(session, f"word{i} ", message=message)
            time.sleep(0.1)
    elif "HISTORY" in text:
        say(session, f"History: {' / '.join(earlier) or '(none)'} | resources={len(resources)}")
    elif "PERMISSION" in text:
        next_id[0] += 1
        rid = next_id[0]
        waiting[rid] = {"event": threading.Event()}
        send({
            "jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
            "params": {
                "sessionId": session,
                "toolCall": {"toolCallId": "t1", "title": "Run `ls`", "kind": "execute"},
                "options": [
                    {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        })
        waiting[rid]["event"].wait(30)
        say(session, "permission outcome: " + json.dumps(waiting.pop(rid).get("result")))
    elif "MARKDOWN" in text:
        say(session, (
            "## Heading\n\n- item **bold**\n\n`inline`\n\n```py\nprint(1)\n```\n\n"
            "[a link](https://example.com/from-answer)\n\n"
            "<script>window.pwned = 1</script><img src=x onerror=\"window.pwned = 2\">"
        ), size=20)
    elif "DOCUMENT" in text:
        say(session, DOCUMENT, size=20)
    elif "TOOL" in text:
        say(session, "before the tool")
        update(session, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Searching the web", "kind": "search", "status": "in_progress"})
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"})
        say(session, "after the tool")
    else:
        update(session, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
        state = sessions[session]
        extra = f" | images={len(images)}" if images else ""
        say(session, f"Echo: {question} | resources={len(resources)} | model={state['model']} effort={state['effort']}{extra}")
    send({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": stop}})


load_histories()
for line in sys.stdin:
    message = json.loads(line)
    log(message)
    method = message.get("method")
    if method and "id" in message:
        if method == "initialize":
            time.sleep(SLOW_START)
            if CLAUDE:
                session_caps = {"systemPrompt": {"mode": {"override": {}}}, "close": {}}
            else:
                session_caps = {"systemPrompt": {}, "close": {}, "resume": {}, "fork": {}}
            send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": not CLAUDE,
                    "promptCapabilities": {"embeddedContext": True, "image": True},
                    "sessionCapabilities": session_caps,
                },
                "authMethods": [],
                "agentInfo": {"name": "mock-claude" if CLAUDE else "mock-codex", "version": "0"},
            }})
        elif method == "session/new":
            if CLAUDE and "systemPrompt" in message["params"]:
                send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32602, "message": "systemPrompt: only override is supported"}})
                continue
            # Unique across restarts, as real session ids are.
            session = f"{'c' if CLAUDE else 's'}{os.getpid()}-{len(sessions) + 1}"
            sessions[session] = {"model": MODELS[0][0], "effort": "xhigh" if CLAUDE else "low"}
            with history_lock:
                histories[session] = []
                save_histories()
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": session, "configOptions": options(session)}})
        elif method == "session/fork" and not CLAUDE:
            session, error = fork(message["params"])
            if error:
                send({"jsonrpc": "2.0", "id": message["id"], "error": error})
            else:
                send({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": session, "configOptions": options(session)}})
        elif method == "session/resume" and not CLAUDE:
            session = message["params"]["sessionId"]
            sessions.setdefault(session, {"model": MODELS[0][0], "effort": "low"})
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"configOptions": options(session)}})
        elif method == "session/set_config_option":
            params = message["params"]
            state = sessions[params["sessionId"]]
            state["model" if params["configId"] == "model" else "effort"] = params["value"]
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"configOptions": options(params["sessionId"])}})
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(message,), daemon=True).start()
        elif method == "session/close":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"{method} not supported"}})
    elif method == "session/cancel":
        cancelled.add(message["params"]["sessionId"])
    elif "id" in message and message["id"] in waiting:
        waiting[message["id"]]["result"] = message.get("result")
        waiting[message["id"]]["event"].set()
