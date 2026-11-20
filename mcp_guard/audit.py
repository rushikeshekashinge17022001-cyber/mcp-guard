"""Append-only JSONL audit trail of every decision."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from mcp_guard import paths
from mcp_guard.models import ApprovalRequest, Decision

log = logging.getLogger(__name__)

_MAX_STRING = 2000


def redact(value: Any, keys: Iterable[str]) -> Any:
    """Recursively mask values whose key looks sensitive and clip huge strings."""
    lowered = [k.lower() for k in keys]

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: ("***" if any(s in str(k).lower() for s in lowered) else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and len(v) > _MAX_STRING:
            return v[:_MAX_STRING] + f"...<{len(v) - _MAX_STRING} more chars>"
        return v

    return walk(value)


class AuditLog:
    def __init__(self, enabled: bool = True, log_arguments: bool = True,
                 redact_keys: Iterable[str] = (), path: Optional[Path] = None) -> None:
        self.enabled = enabled
        self.log_arguments = log_arguments
        self.redact_keys = list(redact_keys)
        self.path = path or paths.audit_log_file()
        self._lock = threading.Lock()

    def record(self, req: ApprovalRequest, decision: Decision) -> None:
        if not self.enabled:
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": req.request_id,
            "origin": req.origin,
            "client": req.client,
            "transport": req.transport,
            "server": req.server,
            "method": req.method,
            "tool": req.tool,
            "decision": decision.action,
            "decided_by": decision.decided_by,
            "reason": decision.reason,
            "latency_ms": decision.latency_ms,
        }
        if self.log_arguments:
            entry["arguments"] = redact(req.arguments, self.redact_keys)
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError as exc:  # auditing must never break request handling
            log.warning("Audit write failed: %s", exc)
