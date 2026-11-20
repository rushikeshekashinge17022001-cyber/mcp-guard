"""Command line interface: ``mcp-guard <command>`` (or ``python -m mcp_guard``)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import List, Optional

from mcp_guard import __version__, config, paths
from mcp_guard.daemon import setup_logging


def _cmd_daemon(args: argparse.Namespace) -> int:
    from mcp_guard.daemon import run_daemon

    return run_daemon(config.load(), ui_name=args.ui, with_gateway=args.gateway)


def _cmd_proxy(args: argparse.Namespace) -> int:
    from mcp_guard.proxy.stdio_proxy import run_proxy

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("usage: mcp-guard proxy --name NAME -- <server command> [args...]", file=sys.stderr)
        return 2
    return run_proxy(args.name, command[0], command[1:], config.load())


def _cmd_gateway(_args: argparse.Namespace) -> int:
    from mcp_guard.proxy.http_gateway import serve_forever

    try:
        asyncio.run(serve_forever(config.load()))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    from mcp_guard import installer

    if not args.dry_run:
        problem = installer.check_importable(sys.executable)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 1
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    try:
        wrapped, skipped, backup = installer.install(args.config, only=only, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name in wrapped:
        print(f"  guarded  {name}")
    for item in skipped:
        print(f"  skipped  {item}")
    if args.dry_run:
        print("(dry run - nothing written)")
    elif wrapped:
        print(f"Backup written to {backup}")
        print("Restart Claude Desktop (Cmd+Q / quit from tray, then reopen) to apply.")
    else:
        print("Nothing to change.")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    from mcp_guard import installer

    try:
        restored, backup = installer.uninstall(args.config, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name in restored:
        print(f"  restored {name}")
    if restored and not args.dry_run:
        print(f"Backup written to {backup}. Restart Claude Desktop to apply.")
    elif not restored:
        print("No guarded servers found.")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    from mcp_guard.broker.client import BrokerClient, BrokerUnavailable

    client = BrokerClient(config.load())

    async def run() -> int:
        try:
            pong = await client.ping()
            status = await client.status()
        except (BrokerUnavailable, OSError) as exc:
            print(f"daemon: NOT RUNNING ({exc})")
            return 1
        print(f"daemon: running (pid {pong.get('pid')}, v{pong.get('version')})")
        pending = status.get("pending", [])
        print(f"pending requests: {len(pending)}")
        for req in pending:
            print(f"  - {req['server']}/{req.get('tool') or req['method']}  from {req['origin']}")
        return 0

    return asyncio.run(run())


def _cmd_logs(args: argparse.Namespace) -> int:
    path = paths.audit_log_file()
    if not path.exists():
        print("No audit log yet.")
        return 0

    def show(line: str) -> None:
        try:
            e = json.loads(line)
        except ValueError:
            return
        mark = "ALLOW" if e["decision"] == "allow" else "BLOCK"
        print(f"{e['ts']}  {mark:<5}  {e['server']}/{e.get('tool') or e['method']:<28} "
              f"by {e['decided_by']:<10} {e['origin']:<15} {e.get('latency_ms', 0):>6}ms  {e.get('reason', '')}")

    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
        for line in lines[-args.n:]:
            show(line)
        if args.follow:
            try:
                while True:
                    line = fh.readline()
                    if line:
                        show(line)
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass
    return 0


def _cmd_test_popup(args: argparse.Namespace) -> int:
    """Send a synthetic request to the daemon - handy to verify the popup."""
    from mcp_guard.broker.client import BrokerClient
    from mcp_guard.models import ApprovalRequest

    cfg = config.load()
    client = BrokerClient(cfg)

    async def one(i: int):
        req = ApprovalRequest(server=args.server, method="tools/call", tool=args.tool,
                              arguments={"path": f"/Users/demo/project/file_{i}.txt", "recursive": True},
                              client="mcp-guard test-popup", origin="claude-desktop", transport="stdio",
                              tool_description="Synthetic request sent by `mcp-guard test-popup`.",
                              annotations={"destructiveHint": True} if "delete" in args.tool else {})
        decision = await client.decide(req)
        print(f"request {i + 1}: {decision.action.upper()}  ({decision.decided_by}: {decision.reason})")

    async def run() -> None:
        await asyncio.gather(*(one(i) for i in range(args.count)))

    asyncio.run(run())
    return 0


def _cmd_init_config(args: argparse.Namespace) -> int:
    if config.write_default(force=args.force):
        print(f"Wrote {paths.config_file()}")
    else:
        print(f"{paths.config_file()} already exists (use --force to overwrite)")
    return 0


def _cmd_service(args: argparse.Namespace) -> int:
    from mcp_guard import service

    print(service.install() if args.action == "install" else service.uninstall())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-guard", description="Interactive authorization for MCP tool calls.")
    p.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    p.add_argument("--log-level", default=os.environ.get("MCP_GUARD_LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="run the approval daemon (popup UI)")
    d.add_argument("--ui", choices=["auto", "tk", "osascript", "console"], default=None)
    d.add_argument("--gateway", action="store_true", help="also run the HTTP gateway in this process")
    d.set_defaults(func=_cmd_daemon)

    x = sub.add_parser("proxy", help="(used by Claude Desktop) guard a stdio MCP server")
    x.add_argument("--name", required=True, help="logical server name shown in the popup")
    x.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args...]")
    x.set_defaults(func=_cmd_proxy)

    g = sub.add_parser("gateway", help="run the HTTP gateway for Claude Web / remote connectors")
    g.set_defaults(func=_cmd_gateway)

    for name, fn, helptext in (("install", _cmd_install, "guard all servers in claude_desktop_config.json"),
                               ("uninstall", _cmd_uninstall, "restore claude_desktop_config.json")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--config", type=lambda v: __import__("pathlib").Path(v).expanduser(), default=None,
                       help="path to claude_desktop_config.json (auto-detected by default)")
        s.add_argument("--dry-run", action="store_true")
        if name == "install":
            s.add_argument("--only", help="comma-separated server names")
        s.set_defaults(func=fn)

    st = sub.add_parser("status", help="show daemon status and pending requests")
    st.set_defaults(func=_cmd_status)

    lg = sub.add_parser("logs", help="show the audit log")
    lg.add_argument("-n", type=int, default=20)
    lg.add_argument("-f", "--follow", action="store_true")
    lg.set_defaults(func=_cmd_logs)

    tp = sub.add_parser("test-popup", help="send synthetic request(s) to the daemon")
    tp.add_argument("--server", default="demo")
    tp.add_argument("--tool", default="delete_file")
    tp.add_argument("--count", type=int, default=1, help="number of simultaneous requests")
    tp.set_defaults(func=_cmd_test_popup)

    ic = sub.add_parser("init-config", help="write a default config.json")
    ic.add_argument("--force", action="store_true")
    ic.set_defaults(func=_cmd_init_config)

    sv = sub.add_parser("service", help="start the daemon automatically at login")
    sv.add_argument("action", choices=["install", "uninstall"])
    sv.set_defaults(func=_cmd_service)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, prefix="mcp-guard" if args.cmd != "proxy" else f"mcp-guard:{args.name}")
    return int(args.func(args) or 0)
