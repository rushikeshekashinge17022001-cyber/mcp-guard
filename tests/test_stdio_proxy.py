"""End-to-end tests: real proxy subprocess + real demo server + in-process broker."""

import asyncio
import json

from conftest import result_text

from mcp_guard import paths


async def test_allow_and_block(make_broker, mcp_client):
    _, ui, _ = await make_broker({"get_weather": ("allow", 0.1), "delete_file": ("block", 0.1)})
    client = await mcp_client()
    await client.handshake()

    await client.call(10, "get_weather", {"city": "Pune"})
    allowed = await client.wait_for(10)
    assert "Weather in Pune" in result_text(allowed)
    assert not allowed["result"].get("isError")

    await client.call(11, "delete_file", {"path": "/etc/passwd"})
    blocked = await client.wait_for(11)
    assert blocked["result"]["isError"] is True
    assert "blocked by MCP Guard" in result_text(blocked)

    # The popup received the full context learned from the session.
    req = ui.asked[1]
    assert req.server == "demo" and req.tool == "delete_file"
    assert req.arguments == {"path": "/etc/passwd"}
    assert req.client == "test-client 1.0"
    assert req.origin == "claude-desktop" and req.transport == "stdio"
    assert req.annotations.get("destructiveHint") is True
    assert "SIMULATED" in req.tool_description

    # ...and both decisions were audited.
    entries = [json.loads(l) for l in paths.audit_log_file().read_text().splitlines()]
    assert [(e["tool"], e["decision"]) for e in entries] == [("get_weather", "allow"), ("delete_file", "block")]


async def test_concurrent_requests_resolved_independently(make_broker, mcp_client):
    # Decisions arrive in a different order than the requests were sent.
    _, ui, _ = await make_broker({
        "get_weather": ("allow", 1.2),
        "delete_file": ("block", 0.2),
        "write_note": ("allow", 0.6),
    })
    client = await mcp_client()
    await client.handshake()
    await client.call(10, "get_weather", {"city": "Delhi"})
    await client.call(11, "delete_file", {"path": "/x"})
    await client.call(12, "write_note", {"title": "t", "body": "b"})

    # Un-gated traffic keeps flowing while approvals are pending.
    pong = await client.request(3, "ping", timeout=2)
    assert pong["result"] == {}

    r10, r11, r12 = [await client.wait_for(i) for i in (10, 11, 12)]
    assert "Weather in Delhi" in result_text(r10)
    assert r11["result"]["isError"] is True
    assert "Saved note" in result_text(r12)
    tool_order = [i for i in client.order if i in (10, 11, 12)]
    assert tool_order == [11, 12, 10]
    assert len(ui.asked) == 3


async def test_client_cancellation_withdraws_popup(make_broker, mcp_client):
    broker, ui, _ = await make_broker({"slow_task": ("allow", None)})  # user never answers
    client = await mcp_client()
    await client.handshake()
    await client.call(20, "slow_task", {"seconds": 1})
    for _ in range(50):
        if broker.pending:
            break
        await asyncio.sleep(0.1)
    assert len(broker.pending) == 1

    await client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 20}})
    for _ in range(50):
        if ui.cancelled:
            break
        await asyncio.sleep(0.1)
    assert ui.cancelled == [ui.asked[0].request_id]
    assert not broker.pending
    await asyncio.sleep(0.3)
    assert 20 not in client.responses  # cancelled requests get no response


async def test_timeout_blocks(make_broker, mcp_client):
    _, ui, _ = await make_broker({"get_weather": ("allow", None)}, {"approval": {"timeout_seconds": 1}})
    client = await mcp_client()
    await client.handshake()
    await client.call(30, "get_weather", {"city": "X"})
    resp = await client.wait_for(30, timeout=8)
    assert resp["result"]["isError"] is True
    assert "no decision" in result_text(resp)
    assert ui.cancelled  # popup dismissed on timeout


async def test_policy_rule_skips_popup(make_broker, mcp_client):
    _, ui, _ = await make_broker(overrides={"policy": {"rules": [
        {"server": "demo", "tool": "get_*", "action": "allow"},
        {"tool": "delete_*", "action": "block", "reason": "deletes are never allowed"},
    ]}})
    client = await mcp_client()
    await client.handshake()
    await client.call(40, "get_weather", {"city": "Goa"})
    await client.call(41, "delete_file", {"path": "/x"})
    assert "Weather in Goa" in result_text(await client.wait_for(40))
    assert "never allowed" in result_text(await client.wait_for(41))
    assert ui.asked == []


async def test_fail_closed_when_daemon_unreachable(guard_home, mcp_client):
    from conftest import write_config

    write_config(guard_home, {})  # no daemon.json -> daemon "not running", autostart disabled
    client = await mcp_client()
    await client.handshake()
    await client.call(50, "get_weather", {"city": "Y"})
    resp = await client.wait_for(50)
    assert resp["result"]["isError"] is True
    assert "unavailable" in result_text(resp)


async def test_jsonrpc_error_block_style(make_broker, mcp_client):
    await make_broker({"delete_file": ("block", 0)}, {"proxy": {"block_style": "jsonrpc_error"}})
    client = await mcp_client()
    await client.handshake()
    await client.call(60, "delete_file", {"path": "/x"})
    resp = await client.wait_for(60)
    assert resp["error"]["code"] == -32001


async def test_client_eof_shuts_down_cleanly(make_broker, mcp_client):
    await make_broker()
    client = await mcp_client()
    await client.handshake()
    client.proc.stdin.close()
    code = await asyncio.wait_for(client.proc.wait(), 8)
    assert code == 0


async def test_missing_server_command(guard_home):
    import sys

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mcp_guard", "proxy", "--name", "x", "--", "definitely-not-a-real-binary",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), 10)
    assert proc.returncode == 127
    assert b"Cannot find MCP server command" in err
