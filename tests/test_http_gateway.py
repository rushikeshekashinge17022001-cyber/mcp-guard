"""Gateway tests: Streamable-HTTP client -> gateway -> (stdio | HTTP) upstream."""

import asyncio
import json
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import DEMO_SERVER, result_text

from mcp_guard.proxy.http_gateway import Gateway

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}


def call(req_id, tool, args):
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": tool, "arguments": args}}


async def _gateway_client(cfg, routes):
    cfg = dict(cfg)
    cfg["gateway"] = dict(cfg["gateway"], routes=routes, path_secret="s3cret")
    gw = Gateway(cfg)
    client = TestClient(TestServer(gw.build_app()))
    await client.start_server()
    return gw, client


async def test_stdio_route_allow_block_and_sessions(make_broker):
    _, ui, cfg = await make_broker({"get_weather": ("allow", 0.1), "delete_file": ("block", 0.1)})
    gw, client = await _gateway_client(cfg, {"demo": {"command": sys.executable, "args": [DEMO_SERVER]}})
    try:
        url = "/s3cret/mcp/demo"
        assert (await client.post("/mcp/demo", json=INIT)).status == 404  # secret required

        r = await client.post(url, json=INIT)
        assert r.status == 200
        sid = r.headers["Mcp-Session-Id"]
        assert (await r.json())["result"]["serverInfo"]["name"] == "mcp-guard-demo"
        h = {"Mcp-Session-Id": sid}

        assert (await client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                  headers=h)).status == 202
        tools = await (await client.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                         headers=h)).json()
        assert len(tools["result"]["tools"]) == 4

        ok, denied = await asyncio.gather(
            client.post(url, json=call(3, "get_weather", {"city": "Mumbai"}), headers=h),
            client.post(url, json=call(4, "delete_file", {"path": "/x"}), headers=h))
        assert "Weather in Mumbai" in result_text(await ok.json())
        denied = await denied.json()
        assert denied["result"]["isError"] and "blocked by MCP Guard" in result_text(denied)

        req = [a for a in ui.asked if a.tool == "delete_file"][0]
        assert req.origin == "claude-web" and req.transport == "http"
        assert req.client == "claude-ai 0.1.0"
        assert req.annotations.get("destructiveHint") is True

        assert (await client.post(url, json=call(5, "get_weather", {}), headers={"Mcp-Session-Id": "nope"})).status == 404
        assert (await client.delete(url, headers=h)).status == 200
    finally:
        await client.close()


async def _fake_upstream():
    """Minimal Streamable-HTTP MCP server answering tools/list over SSE."""
    seen = []

    async def handler(request):
        msg = await request.json()
        seen.append(msg)
        if msg.get("method") == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "remote", "version": "1"}}}, headers={"Mcp-Session-Id": "up-1"})
        if "id" not in msg:
            return web.Response(status=202)
        if msg["method"] == "tools/list":
            payload = {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "send_email", "description": "Send an email", "inputSchema": {"type": "object"},
                 "annotations": {"openWorldHint": True}}]}}
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(f"event: message\ndata: {json.dumps(payload)}\n\n".encode())
            await resp.write_eof()
            return resp
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": f"sent to {msg['params']['arguments']['to']}"}]}})

    app = web.Application()
    app.router.add_post("/mcp", handler)
    server = TestServer(app)
    await server.start_server()
    return server, seen


async def test_http_route_relays_and_gates(make_broker):
    upstream, seen = await _fake_upstream()
    _, ui, cfg = await make_broker({"send_email": ("allow", 0.1)})
    gw, client = await _gateway_client(cfg, {"remote": {"url": str(upstream.make_url("/mcp")),
                                                        "headers": {"X-Api-Key": "k"}}})
    try:
        url = "/s3cret/mcp/remote"
        r = await client.post(url, json=INIT, headers={"Accept": "application/json, text/event-stream"})
        sid = r.headers["Mcp-Session-Id"]
        assert sid == "up-1"
        h = {"Mcp-Session-Id": sid, "Accept": "application/json, text/event-stream"}
        r = await client.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
        assert r.headers["Content-Type"].startswith("text/event-stream")
        assert "send_email" in await r.text()  # SSE relayed verbatim

        r = await client.post(url, json=call(3, "send_email", {"to": "bob@example.com"}), headers=h)
        assert "sent to bob@example.com" in result_text(await r.json())
        # Tool metadata was sniffed out of the SSE stream for the popup.
        assert ui.asked[0].tool_description == "Send an email"
        assert ui.asked[0].annotations == {"openWorldHint": True}
        assert seen[-1]["method"] == "tools/call"
    finally:
        await client.close()
        await upstream.close()


async def test_http_route_block_never_reaches_upstream(make_broker):
    upstream, seen = await _fake_upstream()
    _, ui, cfg = await make_broker({"send_email": ("block", 0)})
    gw, client = await _gateway_client(cfg, {"remote": {"url": str(upstream.make_url("/mcp"))}})
    try:
        r = await client.post("/s3cret/mcp/remote", json=call(9, "send_email", {"to": "eve@example.com"}))
        body = await r.json()
        assert body["result"]["isError"] is True
        assert not any(m.get("method") == "tools/call" for m in seen)
    finally:
        await client.close()
        await upstream.close()


async def test_upstream_down_returns_jsonrpc_error(make_broker):
    _, _, cfg = await make_broker()
    gw, client = await _gateway_client(cfg, {"dead": {"url": "http://127.0.0.1:9/mcp"}})
    try:
        r = await client.post("/s3cret/mcp/dead", json=INIT)
        assert r.status == 502
        assert (await r.json())["error"]["code"] == -32603
    finally:
        await client.close()
