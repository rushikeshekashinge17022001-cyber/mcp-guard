"""Daemon side of the approval broker.

The broker owns the decision pipeline for every intercepted request::

    policy rules ──► (allow/block)            ─┐
         │                                     ├─► audit log ─► reply to proxy
         └─► ask ─► UI popup ─► user / timeout ┘

Each request is handled in its own task, so any number of requests can be
pending at the same time; each gets its own popup and its own timeout.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional

from mcp_guard import __version__, paths
from mcp_guard.audit import AuditLog
from mcp_guard.broker.protocol import STREAM_LIMIT, ProtocolError, read_message, write_message
from mcp_guard.models import ALLOW, ASK, BLOCK, ApprovalRequest, Decision, UserChoice
from mcp_guard.policy import PolicyEngine
from mcp_guard.ui.base import UIBackend

log = logging.getLogger(__name__)


class BrokerServer:
    def __init__(self, ui: UIBackend, policy: PolicyEngine, audit: AuditLog,
                 host: str = "127.0.0.1", port: int = 0, timeout: float = 55.0) -> None:
        self.ui = ui
        self.policy = policy
        self.audit = audit
        self.host = host
        self.port = port
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.pending: Dict[str, ApprovalRequest] = {}
        self._server: Optional[asyncio.base_events.Server] = None
        self._shutdown_event: Optional[asyncio.Event] = None

    @property
    def _shutdown(self) -> asyncio.Event:
        # Lazily created so it binds to the running loop (Python 3.9 quirk).
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        return self._shutdown_event

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        """Bind the listening socket. Raises ``OSError`` if the port is taken
        (i.e. another daemon is already running)."""
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, limit=STREAM_LIMIT, reuse_address=False)
        self.port = self._server.sockets[0].getsockname()[1]
        state = {"host": self.host, "port": self.port, "token": self.token,
                 "pid": os.getpid(), "version": __version__, "started_at": time.time()}
        paths.write_private(paths.daemon_state_file(), json.dumps(state))
        log.info("Approval broker listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        """Stop accepting work; every pending request is answered with BLOCK."""
        self._shutdown.set()
        if self._server is not None:
            self._server.close()
        # Give in-flight handlers a moment to send their "block" replies.
        for _ in range(20):
            if not self.pending:
                break
            await asyncio.sleep(0.05)
        try:
            state = json.loads(paths.daemon_state_file().read_text())
            if state.get("pid") == os.getpid():
                paths.daemon_state_file().unlink()
        except (OSError, ValueError):
            pass

    # --------------------------------------------------------------- handler
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = await asyncio.wait_for(read_message(reader), timeout=10)
            if not hello or hello.get("op") != "hello" or not hmac.compare_digest(
                    str(hello.get("token", "")), self.token):
                await write_message(writer, {"op": "error", "error": "unauthorized"})
                return
            msg = await asyncio.wait_for(read_message(reader), timeout=10)
            if msg is None:
                return
            op = msg.get("op")
            if op == "ping":
                await write_message(writer, {"op": "pong", "version": __version__, "pid": os.getpid()})
            elif op == "status":
                await write_message(writer, {"op": "status",
                                             "pending": [r.to_dict() for r in self.pending.values()]})
            elif op == "decide":
                await self._handle_decide(msg, reader, writer)
            else:
                await write_message(writer, {"op": "error", "error": f"unknown op {op!r}"})
        except (ProtocolError, asyncio.TimeoutError, ConnectionError) as exc:
            log.debug("Broker connection error: %s", exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected broker error")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_decide(self, msg: Dict[str, Any], reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            req = ApprovalRequest.from_dict(msg["request"])
        except (KeyError, TypeError) as exc:
            await write_message(writer, {"op": "error", "error": f"bad request: {exc}"})
            return

        decide_task = asyncio.ensure_future(self.decide(req))
        # EOF on the connection means the requester gave up (client cancelled
        # the call, proxy exited...). Watch for it while the user deliberates.
        eof_task = asyncio.ensure_future(reader.read())
        try:
            done, _ = await asyncio.wait({decide_task, eof_task}, return_when=asyncio.FIRST_COMPLETED)
            if decide_task in done:
                await write_message(writer, {"op": "decision", "decision": decide_task.result().to_dict()})
            else:
                log.info("Requester went away; withdrawing %s", req.target)
                decide_task.cancel()
                await asyncio.gather(decide_task, return_exceptions=True)
        finally:
            eof_task.cancel()
            if not decide_task.done():
                decide_task.cancel()

    # -------------------------------------------------------------- pipeline
    async def decide(self, req: ApprovalRequest) -> Decision:
        """Run ``req`` through policy and (if needed) the UI. Never raises,
        except ``CancelledError`` when the requester withdraws."""
        started = time.monotonic()
        try:
            decision = await self._decide_inner(req)
        except asyncio.CancelledError:
            decision = Decision(req.request_id, BLOCK, "request withdrawn by client", "cancelled")
            decision.latency_ms = int((time.monotonic() - started) * 1000)
            self.audit.record(req, decision)
            raise
        except Exception as exc:  # fail closed on any internal failure
            log.exception("Decision pipeline failed for %s", req.target)
            decision = Decision(req.request_id, BLOCK, f"internal error: {exc}", "error")
        decision.latency_ms = int((time.monotonic() - started) * 1000)
        self.audit.record(req, decision)
        log.info("%s %s (%s: %s)", decision.action.upper(), req.target, decision.decided_by, decision.reason)
        return decision

    async def _decide_inner(self, req: ApprovalRequest) -> Decision:
        if self._shutdown.is_set():
            return Decision(req.request_id, BLOCK, "MCP Guard is shutting down", "error")

        action, reason, source = self.policy.evaluate(req)
        if action in (ALLOW, BLOCK):
            return Decision(req.request_id, action, reason, source)
        assert action == ASK

        self.pending[req.request_id] = req
        ask_future = self.ui.ask(req, self.timeout)
        shutdown_wait = asyncio.ensure_future(self._shutdown.wait())
        try:
            done, _ = await asyncio.wait({ask_future, shutdown_wait}, timeout=self.timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if ask_future in done:
                choice: UserChoice = ask_future.result()
                if choice.remember:
                    self.policy.remember(req, choice.action)
                verb = "allowed" if choice.action == ALLOW else "blocked"
                suffix = " (remembered)" if choice.remember else ""
                return Decision(req.request_id, choice.action, f"{verb} by user{suffix}", "user")
            if shutdown_wait in done:
                return Decision(req.request_id, BLOCK, "MCP Guard is shutting down", "error")
            return Decision(req.request_id, BLOCK,
                            f"no decision within {int(self.timeout)}s", "timeout")
        finally:
            self.pending.pop(req.request_id, None)
            shutdown_wait.cancel()
            if not ask_future.done():
                ask_future.cancel()
                self.ui.cancel(req.request_id)
