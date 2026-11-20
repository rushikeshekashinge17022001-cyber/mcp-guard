"""Wrap / unwrap the MCP servers configured in Claude Desktop.

``install`` rewrites every stdio server entry of ``claude_desktop_config.json``::

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

into::

    "filesystem": {"command": "/path/to/python",
                   "args": ["-m", "mcp_guard", "proxy", "--name", "filesystem", "--",
                            "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

The original command line is kept verbatim after ``--``, so ``uninstall`` can
restore it exactly, even without the backup file. ``env`` and any other keys
are left untouched. A timestamped backup of the whole file is written first,
and the file is replaced atomically.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mcp_guard import paths

MARKER = ["-m", "mcp_guard", "proxy"]


def is_wrapped(entry: Dict[str, Any]) -> bool:
    args = entry.get("args") or []
    return args[:3] == MARKER and "--" in args


def wrap_entry(name: str, entry: Dict[str, Any], python: str) -> Dict[str, Any]:
    new = dict(entry)
    new["command"] = python
    new["args"] = MARKER + ["--name", name, "--", entry["command"], *(entry.get("args") or [])]
    return new


def unwrap_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    args = list(entry.get("args") or [])
    sep = args.index("--")
    original = args[sep + 1:]
    new = dict(entry)
    new["command"] = original[0]
    new["args"] = original[1:]
    return new


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Install Claude Desktop and add at least one MCP server first "
            f"(Settings -> Developer -> Edit Config).")
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError("claude_desktop_config.json must contain a JSON object")
    return data


def _save(path: Path, data: Dict[str, Any]) -> Path:
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


def check_importable(python: str) -> Optional[str]:
    """Claude Desktop starts servers from ``/`` with a minimal environment, so
    the package must be *installed* into ``python``, not just on our CWD."""
    try:
        subprocess.run([python, "-c", "import mcp_guard"], cwd="/", check=True,
                       capture_output=True, timeout=30)
        return None
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
        return (f"`{python} -c 'import mcp_guard'` failed ({exc}). "
                f"Run `pip install -e .` inside this interpreter's environment first.")


def install(config_path: Optional[Path] = None, only: Optional[List[str]] = None,
            dry_run: bool = False, python: Optional[str] = None) -> Tuple[List[str], List[str], Optional[Path]]:
    """Return ``(wrapped, skipped, backup_path)``."""
    path = config_path or paths.claude_desktop_config_file()
    python = python or sys.executable
    data = _load(path)
    servers = data.get("mcpServers") or {}
    wrapped, skipped = [], []
    for name, entry in servers.items():
        if only and name not in only:
            continue
        if not isinstance(entry, dict) or "command" not in entry:
            skipped.append(f"{name} (not a stdio server)")
        elif is_wrapped(entry):
            skipped.append(f"{name} (already guarded)")
        else:
            servers[name] = wrap_entry(name, entry, python)
            wrapped.append(name)
    backup = None
    if wrapped and not dry_run:
        backup = _save(path, data)
    return wrapped, skipped, backup


def uninstall(config_path: Optional[Path] = None, dry_run: bool = False) -> Tuple[List[str], Optional[Path]]:
    path = config_path or paths.claude_desktop_config_file()
    data = _load(path)
    servers = data.get("mcpServers") or {}
    restored = []
    for name, entry in servers.items():
        if isinstance(entry, dict) and is_wrapped(entry):
            servers[name] = unwrap_entry(entry)
            restored.append(name)
    backup = None
    if restored and not dry_run:
        backup = _save(path, data)
    return restored, backup
