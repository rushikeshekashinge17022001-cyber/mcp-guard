"""Filesystem locations used by MCP Guard.

Everything lives under a single state directory (``~/.mcp-guard`` by default,
override with the ``MCP_GUARD_HOME`` environment variable). The directory is
created with ``0700`` permissions because it holds the daemon auth token and
the audit log (which may contain tool arguments).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def home() -> Path:
    override = os.environ.get("MCP_GUARD_HOME")
    path = Path(override).expanduser() if override else Path.home() / ".mcp-guard"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # pragma: no cover - e.g. Windows / foreign owner
        pass
    return path


def config_file() -> Path:
    return home() / "config.json"


def daemon_state_file() -> Path:
    """Port + auth token of the running daemon (mode 0600)."""
    return home() / "daemon.json"


def remembered_rules_file() -> Path:
    return home() / "remembered.json"


def audit_log_file() -> Path:
    return home() / "audit.jsonl"


def daemon_log_file() -> Path:
    return home() / "daemon.log"


def gateway_secret_file() -> Path:
    return home() / "gateway_secret"


def claude_desktop_config_file() -> Path:
    """Platform specific location of ``claude_desktop_config.json``."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "Claude" / "claude_desktop_config.json"


def write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` readable only by the current user."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
