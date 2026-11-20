"""UI backend selection."""

from __future__ import annotations

import logging
import os
import shutil
import sys

from mcp_guard.ui.base import UIBackend

log = logging.getLogger(__name__)


def create_ui(name: str = "auto") -> UIBackend:
    """Instantiate a UI backend. Must be called on the main thread.

    ``auto`` prefers Tk, then the native macOS dialog, then the console.
    """
    # On macOS, prefer osascript (native dialogs) over Tk. The system Python
    # ships Tk 8.5.9 which has broken widget rendering on macOS 13+/Sequoia.
    if name == "auto" and sys.platform == "darwin" and shutil.which("osascript"):
        from mcp_guard.ui.osascript_ui import OsaScriptUI

        return OsaScriptUI()
    if name in ("auto", "tk"):
        headless_linux = sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not headless_linux:
            try:
                from mcp_guard.ui.tk_ui import TkUI

                return TkUI()
            except Exception as exc:  # ImportError, TclError (no display)...
                if name == "tk":
                    raise
                log.warning("Tk UI unavailable (%s); falling back", exc)
        elif name == "tk":
            raise RuntimeError("no display available for the Tk UI")
    if name in ("auto", "osascript") and sys.platform == "darwin" and shutil.which("osascript"):
        from mcp_guard.ui.osascript_ui import OsaScriptUI

        return OsaScriptUI()
    if name in ("auto", "console"):
        from mcp_guard.ui.console_ui import ConsoleUI

        return ConsoleUI()
    raise ValueError(f"unknown or unavailable UI backend: {name}")
