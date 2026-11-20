"""Unit tests for policy, JSON-RPC helpers, audit redaction, installer, broker."""

import asyncio
import json

import pytest

from mcp_guard import installer, jsonrpc
from mcp_guard.audit import redact
from mcp_guard.broker.client import BrokerClient
from mcp_guard.models import ApprovalRequest
from mcp_guard.policy import PolicyEngine


def _req(**kw):
    base = dict(server="fs", method="tools/call", tool="read_file", origin="claude-desktop", client="claude-ai")
    base.update(kw)
    return ApprovalRequest(**base)


def test_policy_first_match_wins(tmp_path):
    p = PolicyEngine([{"server": "fs", "tool": "read_*", "action": "allow"},
                      {"tool": "*", "action": "block"}], remembered_path=tmp_path / "r.json")
    assert p.evaluate(_req())[0] == "allow"
    assert p.evaluate(_req(tool="write_file"))[0] == "block"


def test_policy_default_and_invalid_rules(tmp_path):
    p = PolicyEngine([{"tool": "x"}, "junk"], default="ask", remembered_path=tmp_path / "r.json")
    assert p.rules == []
    assert p.evaluate(_req())[0] == "ask"


def test_policy_remember_is_exact_and_persistent(tmp_path):
    path = tmp_path / "r.json"
    p = PolicyEngine([], remembered_path=path)
    p.remember(_req(tool="rm*"), "block")
    assert p.evaluate(_req(tool="rm*"))[:3:2] == ("block", "remembered")
    assert p.evaluate(_req(tool="rm_everything"))[0] == "ask"  # glob chars were escaped
    assert PolicyEngine([], remembered_path=path).evaluate(_req(tool="rm*"))[0] == "block"


def test_block_response_shapes():
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x"}}
    tool_err = jsonrpc.block_response(msg, "nope")
    assert tool_err["id"] == 7 and tool_err["result"]["isError"] is True
    rpc_err = jsonrpc.block_response(msg, "nope", style="jsonrpc_error")
    assert rpc_err["error"]["code"] == jsonrpc.BLOCKED_ERROR_CODE
    res = jsonrpc.block_response({"id": 1, "method": "resources/read"}, "nope")
    assert "error" in res  # non-tool methods always get a JSON-RPC error


def test_request_target_and_arguments():
    msg = {"method": "resources/read", "id": 1, "params": {"uri": "file:///etc/hosts", "_meta": {}}}
    assert jsonrpc.request_target(msg) == "file:///etc/hosts"
    assert jsonrpc.request_arguments(msg) == {"uri": "file:///etc/hosts"}


def test_redaction():
    out = redact({"user": "a", "api_key": "sk-1", "nested": [{"Password": "p"}], "big": "x" * 5000},
                 ["api_key", "password"])
    assert out["api_key"] == "***" and out["nested"][0]["Password"] == "***"
    assert out["user"] == "a" and len(out["big"]) < 2100


def test_installer_roundtrip(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
               "env": {"A": "1"}},
        "remote": {"url": "https://example.com/mcp"},
    }, "otherSetting": True}
    cfg.write_text(json.dumps(original))

    wrapped, skipped, backup = installer.install(cfg, python="/usr/bin/python3")
    assert wrapped == ["fs"] and backup.exists()
    data = json.loads(cfg.read_text())
    fs = data["mcpServers"]["fs"]
    assert fs["command"] == "/usr/bin/python3"
    assert fs["args"][:5] == ["-m", "mcp_guard", "proxy", "--name", "fs"]
    assert fs["env"] == {"A": "1"} and data["otherSetting"] is True

    assert installer.install(cfg, python="/usr/bin/python3")[0] == []  # idempotent
    restored, _ = installer.uninstall(cfg)
    assert restored == ["fs"]
    assert json.loads(cfg.read_text()) == original


def test_cli_proxy_argument_parsing():
    from mcp_guard.cli import build_parser

    # Server flags after "--" must not be swallowed by our own parser
    # (_cmd_proxy strips the leading "--").
    args = build_parser().parse_args(["proxy", "--name", "fs", "--", "npx", "-y", "pkg", "--flag"])
    assert args.name == "fs" and args.command == ["--", "npx", "-y", "pkg", "--flag"]


async def test_broker_rejects_bad_token(make_broker, guard_home):
    from mcp_guard import paths

    broker, ui, cfg = await make_broker()
    state = json.loads(paths.daemon_state_file().read_text())
    state["token"] = "forged"
    paths.daemon_state_file().write_text(json.dumps(state))
    decision = await BrokerClient(cfg).decide(_req())
    assert decision.action == "block" and decision.decided_by == "error"
    assert ui.asked == []


async def test_broker_shutdown_blocks_pending(make_broker):
    broker, ui, cfg = await make_broker({"read_file": ("allow", None)})
    task = asyncio.ensure_future(BrokerClient(cfg).decide(_req()))
    for _ in range(50):
        if broker.pending:
            break
        await asyncio.sleep(0.05)
    await broker.close()
    decision = await asyncio.wait_for(task, 5)
    assert decision.action == "block" and "shutting down" in decision.reason


async def test_many_simultaneous_decisions(make_broker):
    broker, ui, cfg = await make_broker({f"t{i}": ("allow" if i % 2 else "block", 0.05 * (20 - i))
                                         for i in range(20)})
    client = BrokerClient(cfg)
    decisions = await asyncio.gather(*(client.decide(_req(tool=f"t{i}")) for i in range(20)))
    assert [d.action for d in decisions] == ["allow" if i % 2 else "block" for i in range(20)]
