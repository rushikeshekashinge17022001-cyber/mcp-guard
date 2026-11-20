"""Transparent stdio proxy for local MCP servers (Claude Desktop).

Claude Desktop launches every local MCP server as a child process and speaks
newline-delimited JSON-RPC over its stdin/stdout. ``mcp-guard install``
rewrites each server entry so Claude launches *this* proxy instead, which in
turn launches the real server::

    Claude Desktop ──stdin──► StdioProxy ──stdin──► real MCP server
                   ◄─stdout──            ◄─stdout──
                                  │
                                  └── tools/call? ──► approval daemon ──► popup

Rules:

* Every message is forwarded byte-for-byte, except gated requests
  (``tools/call`` by default), which are parked until a decision arrives.
* Parked requests never block the pipe: other messages (``ping``,
  ``tools/list``, other tool calls...) keep flowing, and every parked request
  is decided independently - so concurrent calls are handled correctly and
  can be answered in any order.
* Blocked requests are answered by the proxy itself; the server never sees
  them.
* ``notifications/cancelled`` for a parked request withdraws its popup and is
  not forwarded (the server never saw the request).
* Server stderr is passed through untouched (Claude writes it to its logs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional

from mcp_guard import jsonrpc
from mcp_guard.broker.client import BrokerClient
from mcp_guard.broker.protocol import STREAM_LIMIT
from mcp_guard.models import Decision
from mcp_guard.proxy.interceptor import SessionContext, _key

log = logging.getLogger(__name__)

# GUI apps on macOS start with a minimal PATH; make `npx`, `uvx`, ... resolvable.
_EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
               str(Path.home() / ".local" / "bin"), str(Path.home() / ".cargo" / "bin"),
               str(Path.home() / ".volta" / "bin"), str(Path.home() / ".bun" / "bin")]


def augmented_path() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for extra in _EXTRA_PATH:
        if extra not in parts and os.path.isdir(extra):
            parts.append(extra)
    return os.pathsep.join(p for p in parts if p)


def resolve_command(command: str) -> Optional[str]:
    if os.path.dirname(command):
        return command if os.path.exists(command) else None
    return shutil.which(command, path=augmented_path())


class StdioProxy:
    def __init__(self, name: str, command: str, args: List[str], cfg: Dict[str, Any],
                 broker: Optional[BrokerClient] = None,
                 stdin: Optional[BinaryIO] = None, stdout: Optional[BinaryIO] = None) -> None:
        self.name = name
        self.command = command
        self.args = list(args)
        self.block_style = cfg["proxy"].get("block_style", "tool_error")
        self.ctx = SessionContext(name, origin="claude-desktop", transport="stdio",
                                  gated_methods=cfg["policy"].get("gated_methods", ["tools/call"]))
        self.broker = broker or BrokerClient(cfg)
        self._stdin = stdin or sys.stdin.buffer
        self._stdout = stdout or sys.stdout.buffer
        self._out_lock = threading.Lock()
        self._pending: Dict[Any, "asyncio.Task[None]"] = {}
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._child_lock: Optional[asyncio.Lock] = None

    # ================================================================ run
    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self._child_lock = asyncio.Lock()
        exe = resolve_command(self.command)
        if exe is None:
            log.error("Cannot find MCP server command %r (PATH=%s)", self.command, augmented_path())
            return 127
        env = dict(os.environ, PATH=augmented_path())
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None, env=env, limit=STREAM_LIMIT)
        except OSError as exc:
            log.error("Failed to start MCP server %r: %s", self.command, exc)
            return 126
        log.info("[%s] guarding %s %s (pid %s)", self.name, exe, " ".join(self.args), self._proc.pid)

        inbox: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()
        self._start_stdin_reader(loop, inbox)
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):  # Windows / non-main thread
                pass

        client_task = asyncio.ensure_future(self._pump_client(inbox))
        server_task = asyncio.ensure_future(self._pump_server())
        stop_task = asyncio.ensure_future(stop.wait())
        done, _ = await asyncio.wait({client_task, server_task, stop_task},
                                     return_when=asyncio.FIRST_COMPLETED)

        if client_task in done:
            log.info("[%s] client closed stdin; shutting server down", self.name)
        elif server_task in done:
            log.info("[%s] server closed stdout (exited?)", self.name)
        else:
            log.info("[%s] terminated by signal", self.name)

        for task in list(self._pending.values()):
            task.cancel()
        await self._shutdown_child(graceful=client_task in done)
        # Let any remaining server output reach the client.
        try:
            await asyncio.wait_for(server_task, timeout=2)
        except (asyncio.TimeoutError, Exception):
            server_task.cancel()
        client_task.cancel()
        stop_task.cancel()
        return self._proc.returncode if self._proc.returncode is not None else 0

    async def _shutdown_child(self, graceful: bool) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        if graceful and proc.stdin is not None:
            try:  # MCP spec: close stdin first and give the server time to exit
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
                return
            except asyncio.TimeoutError:
                pass
        for step in (proc.terminate, proc.kill):
            try:
                step()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
                return
            except asyncio.TimeoutError:
                continue

    # ======================================================= stdin reader
    def _start_stdin_reader(self, loop: asyncio.AbstractEventLoop,
                            inbox: "asyncio.Queue[Optional[bytes]]") -> None:
        """Read the client's stdin on a thread (portable: works on Windows and
        with any kind of pipe, unlike ``connect_read_pipe``)."""

        def post(item: Optional[bytes]) -> None:
            try:
                loop.call_soon_threadsafe(inbox.put_nowait, item)
            except RuntimeError:  # loop already closed
                pass

        def reader() -> None:
            try:
                for line in iter(self._stdin.readline, b""):
                    post(line)
            except (OSError, ValueError) as exc:
                log.debug("stdin reader stopped: %s", exc)
            finally:
                post(None)

        threading.Thread(target=reader, name="mcp-guard-stdin", daemon=True).start()

    # ================================================= client -> server
    async def _pump_client(self, inbox: "asyncio.Queue[Optional[bytes]]") -> None:
        while True:
            line = await inbox.get()
            if line is None:
                return
            if not line.strip():
                continue
            msg = jsonrpc.try_parse(line)
            if msg is None:  # not JSON - let the server produce the parse error
                await self._send_server(line)
                continue
            self.ctx.observe_client(msg)

            if isinstance(msg, list):
                if any(self.ctx.is_gated(m) for m in msg):
                    self._track(("batch", id(msg)), self._gate_batch(msg, line))
                else:
                    await self._send_server(line)
                continue

            if self.ctx.is_gated(msg):
                self._track(_key(msg["id"]), self._gate(msg, line))
                continue

            if msg.get("method") == "notifications/cancelled":
                rid = (msg.get("params") or {}).get("requestId")
                task = self._pending.pop(_key(rid), None)
                if task is not None:
                    log.info("[%s] client cancelled pending request %r", self.name, rid)
                    task.cancel()
                    continue
            await self._send_server(line)

    def _track(self, key: Any, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._pending[key] = task

        def _done(t: "asyncio.Task[None]") -> None:
            if self._pending.get(key) is t:
                del self._pending[key]
            if not t.cancelled() and t.exception() is not None:
                log.error("[%s] gate task failed: %r", self.name, t.exception())

        task.add_done_callback(_done)

    async def _gate(self, msg: Dict[str, Any], raw: bytes) -> None:
        req = self.ctx.build_request(msg)
        log.info("[%s] holding %s %s for approval", self.name, req.method, req.tool)
        decision: Decision = await self.broker.decide(req)
        if decision.allowed:
            await self._send_server(raw)
        else:
            self._write_client(jsonrpc.dumps(jsonrpc.block_response(msg, decision.reason, self.block_style)))

    async def _gate_batch(self, batch: List[Any], raw: bytes) -> None:
        """JSON-RPC batches (MCP 2025-03-26). All gated items must be allowed
        for the batch to be forwarded; otherwise no item is executed."""
        gated = [m for m in batch if self.ctx.is_gated(m)]
        decisions = await asyncio.gather(*(self.broker.decide(self.ctx.build_request(m)) for m in gated))
        if all(d.allowed for d in decisions):
            await self._send_server(raw)
            return
        reasons = {id(m): d.reason for m, d in zip(gated, decisions) if not d.allowed}
        replies = []
        for m in batch:
            if not jsonrpc.is_request(m):
                continue
            if id(m) in reasons:
                replies.append(jsonrpc.block_response(m, reasons[id(m)], self.block_style))
            else:
                replies.append(jsonrpc.error_response(
                    m.get("id"), jsonrpc.BLOCKED_ERROR_CODE,
                    "Not executed: batch contained a request blocked by MCP Guard"))
        self._write_client(jsonrpc.dumps(replies))

    async def _send_server(self, raw: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return
        if not raw.endswith(b"\n"):
            raw += b"\n"
        assert self._child_lock is not None
        async with self._child_lock:
            try:
                proc.stdin.write(raw)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                log.warning("[%s] server stdin closed: %s", self.name, exc)

    # ================================================= server -> client
    async def _pump_server(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        while True:
            try:
                line = await stdout.readline()
            except ValueError:  # line longer than STREAM_LIMIT
                log.error("[%s] oversized message from server dropped", self.name)
                continue
            if not line:
                return
            msg = jsonrpc.try_parse(line)
            if msg is not None:
                self.ctx.observe_server(msg)
            self._write_client(line)

    def _write_client(self, data: bytes) -> None:
        with self._out_lock:
            try:
                self._stdout.write(data)
                self._stdout.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                log.debug("client stdout closed: %s", exc)


def run_proxy(name: str, command: str, args: List[str], cfg: Dict[str, Any]) -> int:
    return asyncio.run(StdioProxy(name, command, args, cfg).run())
