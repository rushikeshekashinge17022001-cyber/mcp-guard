"""Proxy side of the approval broker.

:meth:`BrokerClient.decide` never raises for operational failures: if the
daemon is unreachable or dies mid-request the configured ``fail_mode``
decides (``closed`` -> BLOCK by default). It only propagates
``CancelledError`` - cancelling the call closes the connection, which makes
the daemon dismiss the popup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Tuple

from mcp_guard import paths
from mcp_guard.broker.protocol import STREAM_LIMIT, ProtocolError, read_message, write_message
from mcp_guard.models import ALLOW, BLOCK, ApprovalRequest, Decision

log = logging.getLogger(__name__)


class BrokerUnavailable(Exception):
    pass


class BrokerClient:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        self.fail_mode = cfg["proxy"].get("fail_mode", "closed")
        self.autostart = bool(cfg["proxy"].get("autostart_daemon", True))
        # Our own safety net in case the daemon hangs: its timeout + margin.
        self.reply_timeout = float(cfg["approval"].get("timeout_seconds", 55)) + 15
        # Created lazily: on Python 3.9 asyncio primitives bind to the loop
        # that exists at construction time.
        self._autostart_lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------ connection
    def _endpoint(self) -> Tuple[str, int, str]:
        try:
            state = json.loads(paths.daemon_state_file().read_text(encoding="utf-8"))
            return state["host"], int(state["port"]), state["token"]
        except (OSError, ValueError, KeyError) as exc:
            raise BrokerUnavailable(f"daemon not running ({exc.__class__.__name__})") from exc

    async def _connect(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        host, port, token = self._endpoint()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, limit=STREAM_LIMIT), timeout=3)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BrokerUnavailable(f"cannot connect to daemon at {host}:{port}: {exc}") from exc
        await write_message(writer, {"op": "hello", "token": token})
        return reader, writer

    async def _connect_or_start(self) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await self._connect()
        except BrokerUnavailable:
            if not self.autostart:
                raise
        if self._autostart_lock is None:
            self._autostart_lock = asyncio.Lock()
        async with self._autostart_lock:
            try:  # another coroutine may have started it meanwhile
                return await self._connect()
            except BrokerUnavailable:
                pass
            log.info("Approval daemon not reachable - starting it")
            spawn_daemon()
            deadline = time.monotonic() + 10
            last: Optional[Exception] = None
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                try:
                    return await self._connect()
                except BrokerUnavailable as exc:
                    last = exc
            raise BrokerUnavailable(f"daemon did not start: {last}")

    # ------------------------------------------------------------------- ops
    async def decide(self, req: ApprovalRequest) -> Decision:
        writer: Optional[asyncio.StreamWriter] = None
        try:
            reader, writer = await self._connect_or_start()
            await write_message(writer, {"op": "decide", "request": req.to_dict()})
            reply = await asyncio.wait_for(read_message(reader), timeout=self.reply_timeout)
            if reply is None:
                raise BrokerUnavailable("daemon closed the connection before deciding")
            if reply.get("op") != "decision":
                raise BrokerUnavailable(f"daemon error: {reply.get('error', reply)}")
            return Decision.from_dict(reply["decision"])
        except asyncio.CancelledError:
            raise
        except (BrokerUnavailable, ProtocolError, ConnectionError, OSError,
                asyncio.TimeoutError, KeyError, TypeError) as exc:
            return self._fail(req, exc)
        finally:
            if writer is not None:
                writer.close()

    def _fail(self, req: ApprovalRequest, exc: BaseException) -> Decision:
        action = ALLOW if self.fail_mode == "open" else BLOCK
        reason = f"approval service unavailable ({exc or exc.__class__.__name__})"
        log.error("%s -> %s %s", reason, action.upper(), req.target)
        return Decision(req.request_id, action, reason, "error")

    async def _simple(self, op: str) -> Dict[str, Any]:
        reader, writer = await self._connect()
        try:
            await write_message(writer, {"op": op})
            reply = await asyncio.wait_for(read_message(reader), timeout=5)
            if reply is None:
                raise BrokerUnavailable("no reply")
            return reply
        finally:
            writer.close()

    async def ping(self) -> Dict[str, Any]:
        return await self._simple("ping")

    async def status(self) -> Dict[str, Any]:
        return await self._simple("status")


def spawn_daemon() -> None:
    """Start ``mcp-guard daemon`` fully detached from the calling process."""
    log_fh = open(paths.daemon_log_file(), "ab")
    kwargs: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh,
                                  cwd=str(paths.home()), env=dict(os.environ))
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "mcp_guard", "daemon"], **kwargs)
    log_fh.close()
