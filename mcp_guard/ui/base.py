"""UI backend interface.

Threading model: the asyncio broker runs on a background thread; GUI
toolkits (Tk, AppKit) must own the *main* thread. A backend therefore

* receives ``ask``/``cancel`` calls on the asyncio thread and hands them to
  its UI thread through a thread-safe queue, and
* resolves the returned future with :meth:`_resolve`, which hops back onto the
  asyncio loop via ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any, List, Optional, Tuple

from mcp_guard.models import ApprovalRequest, UserChoice

_RISKY_WORDS = re.compile(
    r"(delete|remove|rm|drop|truncate|write|edit|update|create|move|rename|exec|execute|run|"
    r"shell|command|kill|send|post|push|deploy|transfer|pay|install)", re.I)
_MAX_ARGS_CHARS = 20000


class UIBackend:
    name = "base"
    supports_remember = True

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    # Called on the asyncio thread -------------------------------------
    def ask(self, req: ApprovalRequest, timeout: float) -> "asyncio.Future[UserChoice]":
        raise NotImplementedError

    def cancel(self, request_id: str) -> None:
        """Dismiss the popup for a request that no longer needs a decision."""

    # Called on the main thread ----------------------------------------
    def run_main(self, stop_event: threading.Event) -> None:
        """Block the main thread until ``stop_event`` is set."""
        while not stop_event.wait(0.2):
            pass

    def stop(self) -> None:
        """Request ``run_main`` to return (thread-safe)."""

    def dispose(self) -> None:
        """Release UI resources when ``run_main`` will never be called."""

    # Helpers -------------------------------------------------------------
    def _resolve(self, fut: "asyncio.Future[UserChoice]", choice: UserChoice) -> None:
        assert self.loop is not None

        def _set() -> None:
            if not fut.done():
                fut.set_result(choice)

        self.loop.call_soon_threadsafe(_set)


def bmp_safe(text: str) -> str:
    """Tk 8.5/8.6 cannot render characters outside the BMP (emoji, ...)."""
    return "".join(c if ord(c) <= 0xFFFF else "�" for c in text)


def format_arguments(args: Any) -> str:
    try:
        text = json.dumps(args, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(args)
    if len(text) > _MAX_ARGS_CHARS:
        text = text[:_MAX_ARGS_CHARS] + f"\n... ({len(text) - _MAX_ARGS_CHARS} more characters truncated)"
    return bmp_safe(text)


def risk_badges(req: ApprovalRequest) -> List[Tuple[str, str]]:
    """Return ``(label, level)`` badges; level is ``danger|warn|ok``.

    Annotations are *hints declared by the server* and therefore untrusted;
    the UI labels them as such. When a tool has no annotations we fall back to
    a name-based heuristic.
    """
    ann = req.annotations or {}
    badges: List[Tuple[str, str]] = []
    if ann.get("destructiveHint") is True:
        badges.append(("DESTRUCTIVE (declared)", "danger"))
    if ann.get("readOnlyHint") is True:
        badges.append(("READ-ONLY (declared)", "ok"))
    if ann.get("openWorldHint") is True:
        badges.append(("EXTERNAL SYSTEMS", "warn"))
    if not ann and req.tool and _RISKY_WORDS.search(req.tool):
        badges.append(("MAY MODIFY DATA (heuristic)", "warn"))
    return badges


def origin_label(req: ApprovalRequest) -> str:
    labels = {
        "claude-desktop": "Claude Desktop (local MCP server)",
        "claude-web": "Claude Web / remote connector",
    }
    return labels.get(req.origin, req.origin)
