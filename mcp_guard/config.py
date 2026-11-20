"""Configuration loading.

Configuration is a JSON file (``~/.mcp-guard/config.json``). Every key is
optional; user values are deep-merged over :data:`DEFAULTS`, so a config file
only needs to contain what it changes.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict

from mcp_guard import paths

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "daemon": {
        # The daemon binds a fixed loopback port; a second instance fails to
        # bind and exits, which gives us single-instance semantics for free.
        "host": "127.0.0.1",
        "port": 47821,
    },
    "approval": {
        # Seconds the popup waits for the user before auto-blocking. Keep this
        # below the client's own request timeout (60s for most MCP clients).
        "timeout_seconds": 55,
        # "auto" picks tk, then osascript (macOS), then console.
        "ui": "auto",
    },
    "policy": {
        # What happens when no rule matches: "ask" | "allow" | "block".
        "default": "ask",
        # JSON-RPC methods that require approval. Add "resources/read" or
        # "prompts/get" to gate those as well.
        "gated_methods": ["tools/call"],
        # First matching rule wins. Fields are glob patterns, all optional:
        #   {"server": "filesystem", "tool": "read_*", "action": "allow"}
        "rules": [],
    },
    "proxy": {
        # Behaviour when the daemon cannot be reached: "closed" blocks the
        # request (secure default), "open" lets it through.
        "fail_mode": "closed",
        # Spawn the daemon automatically if a proxy cannot reach it.
        "autostart_daemon": True,
        # How a blocked tools/call is reported back to the client:
        #   "tool_error"   -> successful JSON-RPC result with isError=true
        #                     (the model sees the denial and can react)
        #   "jsonrpc_error"-> JSON-RPC error object
        "block_style": "tool_error",
    },
    "audit": {
        "enabled": True,
        "log_arguments": True,
        # Argument keys whose values are replaced by "***" in the audit log.
        "redact_keys": ["password", "passwd", "secret", "token", "api_key", "apikey",
                        "authorization", "cookie", "private_key", "credential"],
    },
    "gateway": {
        "host": "127.0.0.1",
        "port": 8787,
        # Random URL path segment that acts as a capability token for remote
        # clients. "auto" generates one and stores it in ~/.mcp-guard.
        "path_secret": "auto",
        # name -> {"url": "https://...", "headers": {...}}
        #      or {"command": "npx", "args": [...], "env": {...}}
        "routes": {},
        # Stdio-backed sessions idle longer than this are reaped.
        "session_idle_seconds": 1800,
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load() -> Dict[str, Any]:
    """Return the effective configuration (defaults + user file)."""
    path = paths.config_file()
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        user = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(user, dict):
            raise ValueError("top level must be a JSON object")
    except (OSError, ValueError) as exc:
        # A broken config must never silently disable protection: we fall
        # back to the (secure) defaults and complain loudly.
        log.error("Ignoring invalid config %s: %s", path, exc)
        return copy.deepcopy(DEFAULTS)
    return _deep_merge(DEFAULTS, user)


def write_default(force: bool = False) -> bool:
    path = paths.config_file()
    if path.exists() and not force:
        return False
    example = copy.deepcopy(DEFAULTS)
    example["policy"]["rules"] = [
        {"server": "*", "tool": "get_*", "action": "ask"},
    ]
    paths.write_private(path, json.dumps(example, indent=2) + "\n")
    return True
