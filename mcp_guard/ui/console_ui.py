"""Terminal prompt backend - for headless machines and development.

Requests are asked one at a time (a terminal has a single input line); later
requests wait in a queue. Withdrawn requests are skipped.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from typing import Set

from mcp_guard.models import ALLOW, BLOCK, ApprovalRequest, UserChoice
from mcp_guard.ui.base import UIBackend, format_arguments, origin_label, risk_badges


class ConsoleUI(UIBackend):
    name = "console"

    def __init__(self) -> None:
        super().__init__()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._cancelled: Set[str] = set()

    def ask(self, req: ApprovalRequest, timeout: float) -> "asyncio.Future[UserChoice]":
        assert self.loop is not None
        fut: "asyncio.Future[UserChoice]" = self.loop.create_future()
        self._queue.put((req, fut))
        return fut

    def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)

    def stop(self) -> None:
        self._queue.put((None, None))

    def run_main(self, stop_event: threading.Event) -> None:
        def worker() -> None:
            while not stop_event.is_set():
                req, fut = self._queue.get()
                if req is None:
                    stop_event.set()
                    return
                if req.request_id in self._cancelled or fut.done():
                    continue
                self._prompt(req, fut)

        threading.Thread(target=worker, daemon=True).start()
        while not stop_event.wait(0.2):
            pass

    def _prompt(self, req: ApprovalRequest, fut: "asyncio.Future[UserChoice]") -> None:
        out = sys.stderr
        badges = ", ".join(label for label, _ in risk_badges(req))
        out.write("\n" + "=" * 60 + "\n")
        out.write(f" MCP request from {origin_label(req)} ({req.client})\n")
        out.write(f" server: {req.server}   tool: {req.tool or req.method}\n")
        if badges:
            out.write(f" risk:   {badges}\n")
        out.write(" arguments:\n" + format_arguments(req.arguments) + "\n")
        out.write(" [a]llow / [b]lock / [A] allow+remember / [B] block+remember ? ")
        out.flush()
        answer = sys.stdin.readline().strip()
        if req.request_id in self._cancelled or fut.done():
            out.write(" (request was withdrawn or timed out - answer ignored)\n")
            return
        action = ALLOW if answer.lower().startswith("a") else BLOCK
        self._resolve(fut, UserChoice(action, remember=answer in ("A", "B")))
