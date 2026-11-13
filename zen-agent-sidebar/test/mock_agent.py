#!/usr/bin/env python3
"""A tiny ACP agent for the tests.

    mock_agent.py LOG [--claude] [--slow-start SECONDS]

It writes every message it receives to LOG (one JSON object per line). By
default it looks like codex-acp: settings `model` and `reasoning_effort`, a
system prompt it appends, and sessions it can close and resume (any session
id, as if from a transcript on disk). With --claude it looks like
claude-agent-acp, minus resuming: settings `model` and `effort`, and a system
prompt it can only replace. --slow-start delays the answer to `initialize`.

Prompts are answered according to a keyword in the prompt text:

  CRASH       exit at once
  SLOW        stream 60 words, 0.1 s apart, until cancelled
  PERMISSION  ask the client for permission and report the answer
  MARKDOWN    answer with Markdown that also tries to inject a script
  TOOL        report a tool call, then answer
  (anything else)  echo the question, the number of attached resources and
                   the session's model and effort
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

write_lock = threading.Lock()
sessions = {}  # session id -> {"model": ..., "effort": ...}
cancelled = set()
waiting = {}  # request id -> {"event": Event, "result": ...}
next_id = [1000]


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


def say(session, text, size=12):
    for i in range(0, len(text), size):
        update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text[i:i + size]}})


def prompt(request):
    session = request["params"]["sessionId"]
    blocks = request["params"]["prompt"]
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    resources = [b for b in blocks if b.get("type") == "resource"]
    cancelled.discard(session)
    stop = "end_turn"
    if "CRASH" in text:
        os._exit(3)
    elif "SLOW" in text:
        for i in range(60):
            if session in cancelled:
                stop = "cancelled"
                break
            say(session, f"word{i} ")
            time.sleep(0.1)
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
    elif "TOOL" in text:
        update(session, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Searching the web", "kind": "search", "status": "in_progress"})
        update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"})
        say(session, "after the tool")
    else:
        update(session, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
        question = text.strip().splitlines()[-1] if text.strip() else ""
        state = sessions[session]
        say(session, f"Echo: {question} | resources={len(resources)} | model={state['model']} effort={state['effort']}")
    send({"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": stop}})


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
                session_caps = {"systemPrompt": {}, "close": {}, "resume": {}}
            send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": not CLAUDE,
                    "promptCapabilities": {"embeddedContext": True},
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
