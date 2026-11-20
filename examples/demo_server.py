#!/usr/bin/env python3
"""A tiny, dependency-free stdio MCP server used for demos and tests.

Tools:
  get_weather(city)          read-only, instant
  write_note(title, body)    writes into ./demo-notes/ next to this file
  delete_file(path)          destructive-looking but SIMULATED (never deletes)
  slow_task(seconds)         sleeps, to demonstrate concurrent requests

Add it to Claude Desktop with:
  "demo": {"command": "python3", "args": ["/abs/path/examples/demo_server.py"]}
"""

import json
import sys
import threading
import time
from pathlib import Path

NOTES = Path(__file__).resolve().parent / "demo-notes"
_out_lock = threading.Lock()

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current (fake) weather for a city.",
        "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "write_note",
        "description": "Save a text note to the demo notes folder.",
        "inputSchema": {"type": "object",
                        "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                        "required": ["title", "body"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "delete_file",
        "description": "Delete a file from disk (SIMULATED in this demo - nothing is deleted).",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "slow_task",
        "description": "Sleep for N seconds, then report. Useful to show parallel calls.",
        "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
    },
]


def send(msg):
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def text_result(req_id, text, is_error=False):
    send({"jsonrpc": "2.0", "id": req_id,
          "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def call_tool(req_id, name, args):
    if name == "get_weather":
        text_result(req_id, f"Weather in {args.get('city', '?')}: 24°C, clear skies.")
    elif name == "write_note":
        NOTES.mkdir(exist_ok=True)
        safe = "".join(c for c in str(args.get("title", "note")) if c.isalnum() or c in "-_ ")[:60] or "note"
        path = NOTES / f"{safe}.txt"
        path.write_text(str(args.get("body", "")), encoding="utf-8")
        text_result(req_id, f"Saved note to {path}")
    elif name == "delete_file":
        text_result(req_id, f"(simulated) Deleted {args.get('path')}")
    elif name == "slow_task":
        seconds = min(float(args.get("seconds", 2)), 30)
        time.sleep(seconds)
        text_result(req_id, f"slow_task finished after {seconds:g}s")
    else:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})


def handle(msg):
    method, req_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mcp-guard-demo", "version": "1.0.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        # Each call on its own thread so concurrent calls really overlap.
        threading.Thread(target=call_tool, args=(req_id, params.get("name"), params.get("arguments") or {}),
                         daemon=True).start()
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": req_id, "result": {}})
    elif req_id is not None:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            handle(m)


if __name__ == "__main__":
    main()
