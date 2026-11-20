"""Native macOS dialog via ``osascript`` (fallback when Tk is unavailable).

Each request spawns its own ``display dialog`` process, so concurrent
requests produce concurrent dialogs; withdrawing a request kills its process.
"Remember" is not supported by this backend.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict

from mcp_guard.models import ALLOW, BLOCK, ApprovalRequest, UserChoice
from mcp_guard.ui.base import UIBackend, format_arguments, origin_label, risk_badges

log = logging.getLogger(__name__)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class OsaScriptUI(UIBackend):
    name = "osascript"
    supports_remember = False

    def __init__(self) -> None:
        super().__init__()
        self._tasks: Dict[str, "asyncio.Task[None]"] = {}

    def ask(self, req: ApprovalRequest, timeout: float) -> "asyncio.Future[UserChoice]":
        assert self.loop is not None
        fut: "asyncio.Future[UserChoice]" = self.loop.create_future()
        self._tasks[req.request_id] = self.loop.create_task(self._run(req, timeout, fut))
        return fut

    def cancel(self, request_id: str) -> None:
        task = self._tasks.pop(request_id, None)
        if task:
            task.cancel()

    async def _run(self, req: ApprovalRequest, timeout: float, fut: "asyncio.Future[UserChoice]") -> None:
        args = format_arguments(req.arguments)
        if len(args) > 1200:
            args = args[:1200] + "\n..."
        badges = ", ".join(label for label, _ in risk_badges(req))
        message = (
            f"{origin_label(req)} wants to call an MCP tool.\n\n"
            f"Server: {req.server}\nTool: {req.tool or req.method}\n"
            + (f"Risk: {badges}\n" if badges else "")
            + f"\nArguments:\n{args}"
        )
        script = (
            f"display dialog {_quote(message)} with title {_quote('MCP Guard - approve tool call?')} "
            f'buttons {{"Block", "Allow"}} default button "Block" cancel button "Block" '
            f"giving up after {max(1, int(timeout))} with icon caution"
        )
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            raise
        finally:
            self._tasks.pop(req.request_id, None)
        text = out.decode("utf-8", "replace")
        action = ALLOW if "button returned:Allow" in text and "gave up:true" not in text else BLOCK
        if not fut.done():
            fut.set_result(UserChoice(action))
