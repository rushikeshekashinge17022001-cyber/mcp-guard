"""Rule-based pre-filtering of approval requests.

Rules let users skip the popup for things they always trust (or never trust).
Evaluation order:

1. "Remembered" decisions the user ticked in the popup (persisted to
   ``~/.mcp-guard/remembered.json``).
2. Configured ``policy.rules`` - first match wins.
3. ``policy.default`` (``ask`` unless changed).

All rule fields are shell-style glob patterns (``fnmatch``), matched
case-insensitively. Missing fields match everything.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_guard import paths
from mcp_guard.models import ACTIONS, ALLOW, ASK, BLOCK, ApprovalRequest

log = logging.getLogger(__name__)

_MATCH_FIELDS = ("server", "tool", "method", "client", "origin")


def _matches(rule: Dict[str, Any], req: ApprovalRequest) -> bool:
    for name in _MATCH_FIELDS:
        pattern = rule.get(name)
        if pattern is None:
            continue
        value = getattr(req, name) or ""
        if not fnmatch.fnmatchcase(str(value).lower(), str(pattern).lower()):
            return False
    return True


class PolicyEngine:
    def __init__(self, rules: List[Dict[str, Any]], default: str = ASK,
                 remembered_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self.rules = [r for r in rules if self._valid(r)]
        self.default = default if default in ACTIONS else ASK
        self._remembered_path = remembered_path or paths.remembered_rules_file()
        self.remembered: List[Dict[str, Any]] = self._load_remembered()

    @staticmethod
    def _valid(rule: Any) -> bool:
        ok = isinstance(rule, dict) and rule.get("action") in ACTIONS
        if not ok:
            log.warning("Ignoring invalid policy rule: %r", rule)
        return ok

    def _load_remembered(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self._remembered_path.read_text(encoding="utf-8"))
            return [r for r in data if self._valid(r)]
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("Could not read remembered rules: %s", exc)
            return []

    def evaluate(self, req: ApprovalRequest) -> Tuple[str, str, str]:
        """Return ``(action, reason, source)`` for ``req``."""
        with self._lock:
            for rule in self.remembered:
                if _matches(rule, req):
                    return rule["action"], "remembered decision", "remembered"
            for idx, rule in enumerate(self.rules):
                if _matches(rule, req):
                    reason = rule.get("reason") or f"policy rule #{idx + 1}"
                    return rule["action"], reason, "policy"
        return self.default, "default policy", "policy"

    def remember(self, req: ApprovalRequest, action: str) -> None:
        """Persist an exact-match rule for this server + tool."""
        if action not in (ALLOW, BLOCK):
            return
        rule = {"server": req.server, "method": req.method, "action": action}
        if req.tool:
            # Escape glob metacharacters so the rule matches this tool only.
            rule["tool"] = "".join(f"[{c}]" if c in "*?[" else c for c in req.tool)
        with self._lock:
            self.remembered = [r for r in self.remembered
                               if not (r.get("server") == rule["server"]
                                       and r.get("tool") == rule.get("tool")
                                       and r.get("method") == rule["method"])]
            self.remembered.insert(0, rule)
            snapshot = list(self.remembered)
        try:
            paths.write_private(self._remembered_path, json.dumps(snapshot, indent=2) + "\n")
        except OSError as exc:
            log.warning("Could not persist remembered rule: %s", exc)
