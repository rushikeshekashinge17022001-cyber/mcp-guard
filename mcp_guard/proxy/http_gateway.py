"""Streamable-HTTP MCP gateway (Claude Web / remote connectors).

Why a gateway? When you add a *custom connector* in claude.ai (or in Claude
Desktop's "Connectors" settings), the MCP traffic does **not** originate on
your machine - Anthropic's backend connects to the connector URL. There is no
local process to hook. The only reliable interception point is therefore the
URL itself: you register the gateway URL instead of the real server URL and
the gateway relays traffic after approval::

    claude.ai ──HTTPS──► tunnel ──► MCP Guard gateway ──► real MCP server
                                         │                (HTTP URL, or a
                                         │                 local stdio command)
                                         └─ tools/call? ─► approval daemon ─► popup

Per route, the upstream is either

* ``{"url": "https://..."}``   - a remote Streamable-HTTP server. Requests,
  responses and SSE streams are relayed verbatim (headers included), or
* ``{"command": "...", "args": [...]}`` - a local stdio server, spawned once
  per MCP session and answered with plain JSON responses.

The URL contains a random secret segment (``/<secret>/mcp/<route>``) which
acts as a capability token, since the gateway is reachable from the internet
through the tunnel.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web

from mcp_guard import jsonrpc, paths
from mcp_guard.broker.client import BrokerClient
from mcp_guard.models import BLOCK, Decision
from mcp_guard.proxy.interceptor import SessionContext, _key
from mcp_guard.proxy.stdio_upstream import StdioUpstreamSession, UpstreamClosed

log = logging.getLogger(__name__)

MAX_BODY = 32 * 1024 * 1024
MAX_CONTEXTS = 500
SESSION_HEADER = "Mcp-Session-Id"
_FORWARD_REQUEST_HEADERS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version",
                            "last-event-id", "user-agent"}
_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding",
               "upgrade", "proxy-authenticate", "proxy-authorization", "te", "trailer"}


@dataclass
class Route:
    name: str
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def is_stdio(self) -> bool:
        return self.command is not None

    @classmethod
    def parse(cls, name: str, spec: Dict[str, Any]) -> "Route":
        if not isinstance(spec, dict) or not (spec.get("url") or spec.get("command")):
            raise ValueError(f"gateway route {name!r} needs 'url' or 'command'")
        return cls(name=name, url=spec.get("url"), headers=dict(spec.get("headers") or {}),
                   command=spec.get("command"), args=list(spec.get("args") or []),
                   env=dict(spec.get("env") or {}))


def resolve_secret(cfg_value: Any) -> str:
    if cfg_value in (None, "", False):
        return ""
    if cfg_value != "auto":
        return str(cfg_value)
    path = paths.gateway_secret_file()
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_urlsafe(18)
    paths.write_private(path, secret)
    return secret


class _Sniffer:
    """Watches a relayed response body (JSON or SSE) for messages the
    SessionContext cares about, without altering the bytes."""

    def __init__(self, ctx: SessionContext, content_type: str) -> None:
        self.ctx = ctx
        self.sse = "text/event-stream" in content_type
        self.json = "application/json" in content_type
        self._buf = b""
        self._data: List[str] = []

    def feed(self, chunk: bytes) -> None:
        if not (self.sse or self.json):
            return
        self._buf += chunk
        if self.json:
            if len(self._buf) > MAX_BODY:
                self.json = False
                self._buf = b""
            return
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").rstrip("\r")
            if text.startswith("data:"):
                self._data.append(text[5:].lstrip(" "))
            elif text == "" and self._data:
                self._observe("\n".join(self._data))
                self._data = []

    def close(self) -> None:
        if self.json and self._buf:
            self._observe(self._buf.decode("utf-8", "replace"))

    def _observe(self, text: str) -> None:
        try:
            self.ctx.observe_server(json.loads(text))
        except ValueError:
            pass


class Gateway:
    def __init__(self, cfg: Dict[str, Any], broker: Optional[BrokerClient] = None) -> None:
        gw = cfg["gateway"]
        self.cfg = cfg
        self.host = gw.get("host", "127.0.0.1")
        self.port = int(gw.get("port", 8787))
        self.secret = resolve_secret(gw.get("path_secret", "auto"))
        self.routes = {name: Route.parse(name, spec) for name, spec in (gw.get("routes") or {}).items()}
        self.idle_seconds = float(gw.get("session_idle_seconds", 1800))
        self.gated_methods = cfg["policy"].get("gated_methods", ["tools/call"])
        self.block_style = cfg["proxy"].get("block_style", "tool_error")
        self.broker = broker or BrokerClient(cfg)
        self.contexts: "collections.OrderedDict[Tuple[str, Optional[str]], SessionContext]" = \
            collections.OrderedDict()
        self.stdio_sessions: Dict[str, StdioUpstreamSession] = {}
        self.pending: Dict[Tuple[str, Optional[str], Any], "asyncio.Task[Decision]"] = {}
        self.http: Optional[aiohttp.ClientSession] = None
        self._reaper: Optional["asyncio.Task[None]"] = None
        self._runner: Optional[web.AppRunner] = None

    # =============================================================== app
    @property
    def prefix(self) -> str:
        return f"/{self.secret}" if self.secret else ""

    def route_url(self, name: str, base: Optional[str] = None) -> str:
        base = base or f"http://{self.host}:{self.port}"
        return f"{base}{self.prefix}/mcp/{name}"

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_get("/healthz", self._health)
        app.router.add_route("*", self.prefix + "/mcp/{route}", self.handle)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        if not self.secret:
            log.warning("Gateway has NO path secret - anyone who can reach it can use your MCP servers")
        for name in self.routes:
            log.info("Gateway route %-12s -> %s", name, self.route_url(name))

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _on_startup(self, _app: web.Application) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
                                          auto_decompress=False)
        self._reaper = asyncio.ensure_future(self._reap_idle())

    async def _on_cleanup(self, _app: web.Application) -> None:
        if self._reaper:
            self._reaper.cancel()
        for task in list(self.pending.values()):
            task.cancel()
        await asyncio.gather(*(s.close() for s in self.stdio_sessions.values()), return_exceptions=True)
        self.stdio_sessions.clear()
        if self.http:
            await self.http.close()

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "routes": sorted(self.routes)})

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for sid, sess in list(self.stdio_sessions.items()):
                if now - sess.last_used > self.idle_seconds or not sess.alive:
                    log.info("Closing idle/dead stdio session %s", sid)
                    self.stdio_sessions.pop(sid, None)
                    self.contexts.pop((sess.label, sid), None)
                    await sess.close()

    # ========================================================== contexts
    def _context(self, route: str, sid: Optional[str], fresh: bool = False) -> SessionContext:
        key = (route, sid)
        if fresh or key not in self.contexts:
            ctx = SessionContext(route, origin="claude-web", transport="http", gated_methods=self.gated_methods)
            if not fresh:
                self._store_context(key, ctx)
            return ctx
        self.contexts.move_to_end(key)
        return self.contexts[key]

    def _store_context(self, key: Tuple[str, Optional[str]], ctx: SessionContext) -> None:
        self.contexts[key] = ctx
        self.contexts.move_to_end(key)
        while len(self.contexts) > MAX_CONTEXTS:
            self.contexts.popitem(last=False)

    # ========================================================== handling
    async def handle(self, request: web.Request) -> web.StreamResponse:
        route = self.routes.get(request.match_info["route"])
        if route is None:
            return web.json_response({"error": "unknown route"}, status=404)
        sid = request.headers.get(SESSION_HEADER)

        if request.method == "POST":
            return await self._post(request, route, sid)
        if route.is_stdio:
            if request.method == "DELETE" and sid:
                sess = self.stdio_sessions.pop(sid, None)
                self.contexts.pop((route.name, sid), None)
                if sess:
                    await sess.close()
                return web.Response(status=200 if sess else 404)
            # No server->client SSE stream for stdio-backed routes (allowed by spec).
            return web.Response(status=405, headers={"Allow": "POST, DELETE"})
        if request.method == "DELETE" and sid:
            self.contexts.pop((route.name, sid), None)
        return await self._forward(request, route, b"", self._context(route.name, sid), sid, None)

    async def _post(self, request: web.Request, route: Route, sid: Optional[str]) -> web.StreamResponse:
        body = await request.read()
        msg = jsonrpc.try_parse(body)
        if not isinstance(msg, (dict, list)) or msg == []:
            return web.json_response(jsonrpc.error_response(None, jsonrpc.PARSE_ERROR, "Parse error"), status=400)
        items = msg if isinstance(msg, list) else [msg]
        is_init = any(isinstance(m, dict) and m.get("method") == "initialize" for m in items)
        ctx = self._context(route.name, sid, fresh=is_init and not sid)
        ctx.observe_client(msg)

        # Cancellation of a request that is still waiting for approval.
        for m in items:
            if isinstance(m, dict) and m.get("method") == "notifications/cancelled":
                rid = (m.get("params") or {}).get("requestId")
                task = self.pending.get((route.name, sid, _key(rid)))
                if task is not None:
                    log.info("[%s] client cancelled pending request %r", route.name, rid)
                    task.cancel()

        gated = [m for m in items if ctx.is_gated(m)]
        if gated:
            decisions = await self._decide_all(request, route, sid, ctx, gated)
            if decisions is None:  # HTTP client disconnected while we waited
                return web.Response(status=499)
            blocked = {id(m): d for m, d in zip(gated, decisions) if not d.allowed}
            if blocked:
                replies = []
                for m in items:
                    if not jsonrpc.is_request(m):
                        continue
                    if id(m) in blocked:
                        replies.append(jsonrpc.block_response(m, blocked[id(m)].reason, self.block_style))
                    else:
                        replies.append(jsonrpc.error_response(
                            m.get("id"), jsonrpc.BLOCKED_ERROR_CODE,
                            "Not executed: batch contained a request blocked by MCP Guard"))
                headers = {SESSION_HEADER: sid} if sid else None
                return web.json_response(replies if isinstance(msg, list) else replies[0], headers=headers)

        if route.is_stdio:
            return await self._stdio_post(route, sid, msg, items, ctx, is_init)
        return await self._forward(request, route, body, ctx, sid, ctx if is_init and not sid else None)

    async def _decide_all(self, request: web.Request, route: Route, sid: Optional[str],
                          ctx: SessionContext, gated: List[Dict[str, Any]]) -> Optional[List[Decision]]:
        tasks: List["asyncio.Task[Decision]"] = []
        keys = []
        for m in gated:
            req = ctx.build_request(m)
            task = asyncio.ensure_future(self.broker.decide(req))
            key = (route.name, sid, _key(m["id"]))
            self.pending[key] = task
            keys.append(key)
            tasks.append(task)
        watcher = asyncio.ensure_future(_wait_disconnect(request))
        try:
            while not all(t.done() for t in tasks):
                done, _ = await asyncio.wait(set(tasks) | {watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    log.info("[%s] HTTP client disconnected; withdrawing %d request(s)", route.name, len(tasks))
                    for t in tasks:
                        t.cancel()
                    return None
            out = []
            for m, t in zip(gated, tasks):
                if t.cancelled():
                    out.append(Decision("", BLOCK, "cancelled by client", "cancelled"))
                else:
                    out.append(t.result())
            return out
        finally:
            watcher.cancel()
            for key in keys:
                self.pending.pop(key, None)

    # --------------------------------------------------------- stdio route
    async def _stdio_post(self, route: Route, sid: Optional[str], msg: Any, items: List[Any],
                          ctx: SessionContext, is_init: bool) -> web.StreamResponse:
        assert route.command is not None
        if sid is None:
            if not is_init:
                return web.json_response(jsonrpc.error_response(
                    None, jsonrpc.INVALID_REQUEST, f"Missing {SESSION_HEADER} header"), status=400)
            sid = uuid.uuid4().hex
            sess = StdioUpstreamSession(route.name, route.command, route.args, route.env)
            try:
                await sess.start()
            except (UpstreamClosed, OSError) as exc:
                log.error("[%s] cannot start upstream: %s", route.name, exc)
                return web.json_response(jsonrpc.error_response(
                    items[0].get("id") if isinstance(items[0], dict) else None,
                    jsonrpc.INTERNAL_ERROR, f"Upstream MCP server failed to start: {exc}"), status=502)
            self.stdio_sessions[sid] = sess
            self._store_context((route.name, sid), ctx)
        else:
            found = self.stdio_sessions.get(sid)
            if found is None:
                return web.json_response({"error": "unknown or expired session"}, status=404)
            sess = found

        headers = {SESSION_HEADER: sid}
        requests = [m for m in items if jsonrpc.is_request(m)]
        try:
            for m in items:
                if not jsonrpc.is_request(m):
                    await sess.send(m)
            if not requests:
                return web.Response(status=202, headers=headers)
            responses = await asyncio.gather(*(sess.request(m) for m in requests))
        except UpstreamClosed as exc:
            replies = [jsonrpc.error_response(m.get("id"), jsonrpc.INTERNAL_ERROR, str(exc)) for m in requests]
            return web.json_response(replies if isinstance(msg, list) else (replies[0] if replies else {}),
                                     status=200 if replies else 502, headers=headers)
        ctx.observe_server(list(responses))
        return web.json_response(list(responses) if isinstance(msg, list) else responses[0], headers=headers)

    # ---------------------------------------------------------- http route
    async def _forward(self, request: web.Request, route: Route, body: bytes, ctx: SessionContext,
                       sid: Optional[str], init_ctx: Optional[SessionContext]) -> web.StreamResponse:
        assert self.http is not None and route.url is not None
        headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST_HEADERS}
        headers.update(route.headers)
        headers["Accept-Encoding"] = "identity"
        try:
            upstream = await self.http.request(request.method, route.url, headers=headers,
                                               data=body or None, allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            log.error("[%s] upstream unreachable: %s", route.name, exc)
            parsed = jsonrpc.try_parse(body) if body else None
            rid = parsed.get("id") if isinstance(parsed, dict) else None
            return web.json_response(jsonrpc.error_response(
                rid, jsonrpc.INTERNAL_ERROR, f"MCP Guard gateway: upstream unreachable ({exc})"), status=502)
        try:
            new_sid = upstream.headers.get(SESSION_HEADER)
            if init_ctx is not None:
                self._store_context((route.name, new_sid), init_ctx)
            resp = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                if key.lower() not in _HOP_BY_HOP:
                    resp.headers.add(key, value)
            await resp.prepare(request)
            sniffer = _Sniffer(ctx, upstream.headers.get("Content-Type", ""))
            try:
                async for chunk in upstream.content.iter_any():
                    sniffer.feed(chunk)
                    await resp.write(chunk)
            except (ConnectionResetError, aiohttp.ClientPayloadError) as exc:
                log.debug("[%s] stream ended: %s", route.name, exc)
                return resp
            sniffer.close()
            await resp.write_eof()
            return resp
        finally:
            upstream.release()


async def _wait_disconnect(request: web.Request) -> None:
    while True:
        transport = request.transport
        if transport is None or transport.is_closing():
            return
        await asyncio.sleep(0.5)


async def serve_forever(cfg: Dict[str, Any]) -> None:
    gateway = Gateway(cfg)
    if not gateway.routes:
        log.warning("No gateway routes configured (config.json -> gateway.routes)")
    await gateway.start()
    try:
        await asyncio.Event().wait()
    finally:
        await gateway.stop()
