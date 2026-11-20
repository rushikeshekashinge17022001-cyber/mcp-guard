"""Run the daemon at login (macOS LaunchAgent).

A LaunchAgent (not a LaunchDaemon) runs inside the user's GUI session, which
is required for the popup to appear. ``KeepAlive`` restarts it if it crashes.
On other platforms we print the equivalent manual setup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from mcp_guard import paths

LABEL = "ai.walled.mcp-guard"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _plist() -> str:
    env = ""
    if os.environ.get("MCP_GUARD_HOME"):
        env = (f"  <key>EnvironmentVariables</key>\n  <dict>\n"
               f"    <key>MCP_GUARD_HOME</key><string>{os.environ['MCP_GUARD_HOME']}</string>\n  </dict>\n")
    log = paths.daemon_log_file()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string><string>-m</string><string>mcp_guard</string><string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
{env}</dict>
</plist>
"""


def install() -> str:
    if sys.platform != "darwin":
        return manual_instructions()
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plist(), encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return f"Wrote {path}, but launchctl failed: {result.stderr.strip()}"
    return f"Installed and started LaunchAgent {LABEL} ({path}). Logs: {paths.daemon_log_file()}"


def uninstall() -> str:
    if sys.platform != "darwin":
        return "Remove the autostart entry you created manually."
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    if path.exists():
        path.unlink()
        return f"Removed LaunchAgent {LABEL}"
    return "LaunchAgent was not installed"


def manual_instructions() -> str:
    cmd = f'"{sys.executable}" -m mcp_guard daemon'
    if sys.platform.startswith("win"):
        pyw = Path(sys.executable).with_name("pythonw.exe")
        return ("Create a shortcut in shell:startup (Win+R -> shell:startup) with target:\n"
                f'  "{pyw}" -m mcp_guard daemon')
    return ("Create ~/.config/systemd/user/mcp-guard.service with:\n"
            "  [Service]\n"
            f"  ExecStart={cmd}\n  Restart=on-failure\n"
            "  [Install]\n  WantedBy=graphical-session.target\n"
            "then: systemctl --user enable --now mcp-guard")
