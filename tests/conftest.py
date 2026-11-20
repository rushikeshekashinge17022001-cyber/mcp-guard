import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEMO_SERVER = str(ROOT / "examples" / "demo_server.py")
sys.path.insert(0, str(ROOT))

from mcp_guard import config  # noqa: E402
from mcp_guard.audit import AuditLog  # noqa: E402
from mcp_guard.broker.server import BrokerServer  # noqa: E402
from mcp_guard.models import UserChoice  # noqa: E402
from mcp_guard.policy import PolicyEngine  # noqa: E402
from mcp_guard.ui.base import UIBackend  # noqa: E402


class ScriptedUI(UIBackend):
    """Fake popup: answers per tool name after a delay (``None`` = never)."""

    name = "scripted"

    def __init__(self, script: Optional[Dict[str, Tuple[str, Optional[float]]]] = None) -> None:
        super().__init__()
        self.script = script or {}
        self.asked: List[Any] = []
        self.cancelled: List[str] = []

    def ask(self, req, timeout):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.asked.append(req)
        action, delay = self.script.get(req.tool, ("allow", 0))
        if delay is not None:
            loop.call_later(delay, lambda: fut.done() or fut.set_result(UserChoice(action)))
        return fut

    def cancel(self, request_id):
        self.cancelled.append(request_id)


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    home = tmp_path / "guard-home"
    home.mkdir()
    monkeypatch.setenv("MCP_GUARD_HOME", str(home))
    return home


def write_config(home: Path, overrides: Dict[str, Any]) -> Dict[str, Any]:
    base = {"daemon": {"port": 0}, "proxy": {"autostart_daemon": False},
            "approval": {"timeout_seconds": 10}}
    for key, value in overrides.items():
        base.setdefault(key, {}).update(value)
    (home / "config.json").write_text(json.dumps(base))
    return config.load()


@pytest.fixture
def make_broker(guard_home):
    brokers = []

    async def factory(script=None, overrides=None):
        cfg = write_config(guard_home, overrides or {})
        ui = ScriptedUI(script)
        ui.bind_loop(asyncio.get_running_loop())
        broker = BrokerServer(ui, PolicyEngine(cfg["policy"]["rules"], cfg["policy"]["default"]),
                              AuditLog(), host="127.0.0.1", port=0,
                              timeout=cfg["approval"]["timeout_seconds"])
        await broker.start()
        brokers.append(broker)
        return broker, ui, cfg

    yield factory
    for b in brokers:
        if b._server is not None:
            b._server.close()


class McpStdioClient:
    """Speaks MCP over stdio to `mcp-guard proxy` like Claude Desktop does."""

    def __init__(self, proc):
        self.proc = proc
        self.responses: Dict[Any, Dict[str, Any]] = {}
        self.order: List[Any] = []
        self._event = asyncio.Event()
        self._reader = asyncio.ensure_future(self._read())

    @classmethod
    async def start(cls, name="demo"):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_guard", "proxy", "--name", name, "--", sys.executable, DEMO_SERVER,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(ROOT), env=dict(os.environ))
        return cls(proc)

    async def _read(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            msg = json.loads(line)
            for m in msg if isinstance(msg, list) else [msg]:
                self.responses[m.get("id")] = m
                self.order.append(m.get("id"))
            self._event.set()

    async def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def wait_for(self, req_id, timeout=10.0):
        async def _wait():
            while req_id not in self.responses:
                self._event.clear()
                await self._event.wait()
            return self.responses[req_id]
        return await asyncio.wait_for(_wait(), timeout)

    async def request(self, req_id, method, params=None, timeout=10.0):
        await self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        return await self.wait_for(req_id, timeout)

    async def handshake(self):
        await self.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "test-client", "version": "1.0"}})
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await self.request(2, "tools/list")

    async def call(self, req_id, tool, arguments=None):
        await self.send({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments or {}}})

    async def close(self):
        if self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 8)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        self._reader.cancel()


@pytest.fixture
async def mcp_client():
    clients = []

    async def factory(name="demo"):
        c = await McpStdioClient.start(name)
        clients.append(c)
        return c

    yield factory
    for c in clients:
        await c.close()


def result_text(resp):
    if "error" in resp:
        return resp["error"]["message"]
    return resp["result"]["content"][0]["text"]
