"""The background approval daemon.

Thread layout::

    main thread ── UI toolkit event loop (Tk mainloop / dialog waiter)
    worker thread ── asyncio loop: BrokerServer (+ optional HTTP gateway)

GUI toolkits must own the main thread (mandatory on macOS), hence the
asyncio side runs on a worker thread and talks to the UI through queues.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import signal
import sys
import threading
from typing import Any, Dict, List, Optional

from mcp_guard.audit import AuditLog
from mcp_guard.broker.server import BrokerServer
from mcp_guard.policy import PolicyEngine
from mcp_guard.ui import create_ui

log = logging.getLogger(__name__)


def run_daemon(cfg: Dict[str, Any], ui_name: Optional[str] = None, with_gateway: bool = False) -> int:
    ui = create_ui(ui_name or cfg["approval"].get("ui", "auto"))
    log.info("Using '%s' approval UI", ui.name)
    policy = PolicyEngine(cfg["policy"].get("rules", []), cfg["policy"].get("default", "ask"))
    audit_cfg = cfg["audit"]
    audit = AuditLog(audit_cfg.get("enabled", True), audit_cfg.get("log_arguments", True),
                     audit_cfg.get("redact_keys", []))

    loop = asyncio.new_event_loop()
    ui.bind_loop(loop)
    broker = BrokerServer(ui, policy, audit, host=cfg["daemon"]["host"], port=int(cfg["daemon"]["port"]),
                          timeout=float(cfg["approval"].get("timeout_seconds", 55)))

    stop_event = threading.Event()
    started = threading.Event()
    errors: List[BaseException] = []
    async_stop: Dict[str, asyncio.Event] = {}

    async def main() -> None:
        async_stop["event"] = asyncio.Event()
        gateway = None
        try:
            await broker.start()
            if with_gateway:
                from mcp_guard.proxy.http_gateway import Gateway

                gateway = Gateway(cfg)
                await gateway.start()
        except BaseException as exc:
            errors.append(exc)
            started.set()
            return
        started.set()
        try:
            await async_stop["event"].wait()
        finally:
            log.info("Shutting down: blocking %d pending request(s)", len(broker.pending))
            await broker.close()
            if gateway is not None:
                await gateway.stop()

    def worker() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(main())
        except Exception:
            log.exception("Broker loop crashed")
        finally:
            stop_event.set()
            ui.stop()

    thread = threading.Thread(target=worker, name="mcp-guard-broker", daemon=True)
    thread.start()
    started.wait()
    if errors:
        exc = errors[0]
        ui.dispose()
        if isinstance(exc, OSError) and exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            log.info("Another MCP Guard daemon is already running on port %s", cfg["daemon"]["port"])
            return 0
        log.error("Failed to start daemon: %s", exc)
        return 1

    def request_stop(*_args: Any) -> None:
        stop_event.set()
        ui.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, OSError):  # pragma: no cover
            pass

    log.info("MCP Guard daemon running (Ctrl+C to stop)")
    try:
        ui.run_main(stop_event)
    except KeyboardInterrupt:
        pass
    finally:
        ev = async_stop.get("event")
        if ev is not None:
            loop.call_soon_threadsafe(ev.set)
        thread.join(timeout=5)
    log.info("MCP Guard daemon stopped")
    return 0


def setup_logging(level: str = "INFO", prefix: str = "mcp-guard") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format=f"%(asctime)s [{prefix}] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
