"""A stdio MCP server driven as a request/response backend.

Used by the HTTP gateway to expose a *local* stdio server to remote clients
(Claude Web custom connectors) - one child process per MCP session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from mcp_guard import jsonrpc
from mcp_guard.broker.protocol import STREAM_LIMIT
from mcp_guard.proxy.interceptor import _key
from mcp_guard.proxy.stdio_proxy import augmented_path, resolve_command

log = logging.getLogger(__name__)


class UpstreamClosed(ConnectionError):
    pass


class StdioUpstreamSession:
    def __init__(self, label: str, command: str, args: List[str], env: Optional[Dict[str, str]] = None) -> None:
        self.label = label
        self.command = command
        self.args = list(args)
        self.env = dict(env or {})
        self.last_used = time.monotonic()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[Any, "asyncio.Future[Dict[str, Any]]"] = {}
        self._reader: Optional["asyncio.Task[None]"] = None
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        exe = resolve_command(self.command)
        if exe is None:
            raise UpstreamClosed(f"command not found: {self.command}")
        env = dict(os.environ, PATH=augmented_path(), **self.env)
        self._proc = await asyncio.create_subprocess_exec(
            exe, *self.args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=None, env=env, limit=STREAM_LIMIT)
        self._reader = asyncio.ensure_future(self._read_loop())
        log.info("[%s] started stdio upstream pid %s", self.label, self._proc.pid)

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                msg = jsonrpc.try_parse(line)
                for m in msg if isinstance(msg, list) else [msg]:
                    if jsonrpc.is_response(m):
                        fut = self._pending.pop(_key(m.get("id")), None)
                        if fut is not None and not fut.done():
                            fut.set_result(m)
                    elif jsonrpc.is_request(m):
                        # Server->client requests (sampling, roots...) cannot be
                        # delivered without a GET stream; refuse them promptly
                        # instead of letting the server hang.
                        await self.send(jsonrpc.error_response(
                            m["id"], jsonrpc.METHOD_NOT_FOUND, "not supported by MCP Guard gateway"))
        except (ValueError, asyncio.CancelledError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(UpstreamClosed(f"MCP server '{self.label}' exited"))
            self._pending.clear()

    async def send(self, msg: Any) -> None:
        self.last_used = time.monotonic()
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise UpstreamClosed(f"MCP server '{self.label}' is not running")
        async with self._lock:
            try:
                self._proc.stdin.write(jsonrpc.dumps(msg))
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise UpstreamClosed(str(exc)) from exc

    async def request(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        self._pending[_key(msg["id"])] = fut
        try:
            await self.send(msg)
            return await fut
        finally:
            self._pending.pop(_key(msg["id"]), None)
            self.last_used = time.monotonic()

    async def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if self._reader is not None:
            self._reader.cancel()
