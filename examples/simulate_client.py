#!/usr/bin/env python3
"""Drive a guarded MCP server exactly the way Claude Desktop does.

Spawns ``mcp-guard proxy`` around ``demo_server.py`` over stdio, performs the
MCP handshake, then fires several tool calls *at the same time* and prints
each response as it arrives. With the daemon running you get one popup per
call and can allow/block them in any order.

    python examples/simulate_client.py                 # 3 concurrent calls
    python examples/simulate_client.py --cancel        # also shows cancellation
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

CALLS = [
    ("get_weather", {"city": "Bengaluru"}),
    ("write_note", {"title": "groceries", "body": "milk, eggs, coffee"}),
    ("delete_file", {"path": "/Users/me/Documents/taxes-2025.pdf"}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cancel", action="store_true", help="cancel a 4th call after 5 seconds")
    args = ap.parse_args()

    cmd = [sys.executable, "-m", "mcp_guard", "proxy", "--name", "demo", "--",
           sys.executable, str(HERE / "demo_server.py")]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=os.environ)
    started = time.monotonic()

    def send(msg):
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        proc.stdin.flush()

    responses = {}
    done = threading.Event()

    def reader():
        for line in proc.stdout:
            msg = json.loads(line)
            responses[msg.get("id")] = msg
            if isinstance(msg.get("id"), int) and msg["id"] >= 10:
                result = msg.get("result") or {}
                text = (result.get("content") or [{}])[0].get("text") if result else msg.get("error", {}).get("message")
                status = "ERROR/BLOCKED" if result.get("isError") or "error" in msg else "OK"
                print(f"[{time.monotonic() - started:6.1f}s] id={msg['id']:<3} {status:<13} {text}")
        done.set()

    threading.Thread(target=reader, daemon=True).start()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "simulated-claude-desktop", "version": "1.0"}}})
    while 1 not in responses:
        time.sleep(0.05)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    while 2 not in responses:
        time.sleep(0.05)
    print("tools:", ", ".join(t["name"] for t in responses[2]["result"]["tools"]))

    print(f"Sending {len(CALLS)} concurrent tool calls - answer the popups in any order...")
    for i, (tool, arguments) in enumerate(CALLS):
        send({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}})
    expected = [10 + i for i in range(len(CALLS))]

    if args.cancel:
        send({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
              "params": {"name": "slow_task", "arguments": {"seconds": 1}}})
        time.sleep(5)
        print("Client cancels request id=99 -> its popup disappears")
        send({"jsonrpc": "2.0", "method": "notifications/cancelled",
              "params": {"requestId": 99, "reason": "user pressed stop"}})

    while not all(i in responses for i in expected):
        time.sleep(0.1)
    proc.stdin.close()
    proc.wait(timeout=10)
    print("done.")


if __name__ == "__main__":
    main()
