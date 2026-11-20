"""Tkinter confirmation popups (default backend, cross-platform).

Each pending request gets its own always-on-top window, cascaded so that
simultaneous requests are all visible. Security-relevant UX details:

* No keyboard shortcut approves a request - Enter does nothing, Escape
  blocks. A stray keystroke while typing elsewhere can never allow a call.
* Buttons are disarmed for a short moment after the window appears, so a
  click aimed at another app cannot land on "Allow" by accident.
* Closing the window counts as "Block".
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import time
from typing import Any, Dict, Optional

from mcp_guard.models import ALLOW, BLOCK, ApprovalRequest, UserChoice
from mcp_guard.ui.base import UIBackend, bmp_safe, format_arguments, origin_label, risk_badges

log = logging.getLogger(__name__)

ARM_DELAY_MS = 700
POLL_MS = 50

_LIGHT = {
    "bg": "#F8FAFC",
    "card": "#FFFFFF",
    "text": "#0F172A",
    "muted": "#64748B",
    "header": "#0F172A",
    "header_text": "#F8FAFC",
    "accent": "#F59E0B",
    "allow": "#15803D",
    "allow_hover": "#166534",
    "block": "#B91C1C",
    "block_hover": "#991B1B",
    "disabled": "#94A3B8",
    "code_bg": "#F1F5F9",
    "danger": "#B91C1C",
    "warn": "#B45309",
    "ok": "#15803D",
}

_DARK = {
    "bg": "#1C1C1E",
    "card": "#2C2C2E",
    "text": "#E5E5EA",
    "muted": "#8E8E93",
    "header": "#2C2C2E",
    "header_text": "#E5E5EA",
    "accent": "#FFD60A",
    "allow": "#34C759",
    "allow_hover": "#2DB44A",
    "block": "#FF453A",
    "block_hover": "#E03E34",
    "disabled": "#48484A",
    "code_bg": "#2C2C2E",
    "danger": "#FF453A",
    "warn": "#FF9F0A",
    "ok": "#34C759",
}


def _is_dark_mode() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import subprocess
        r = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2,
        )
        return r.stdout.strip().lower() == "dark"
    except Exception:
        return False


COLORS = _DARK if _is_dark_mode() else _LIGHT


def _activate_app() -> None:
    """Bring our process to the foreground so the popup gets keyboard focus.

    ``-topmost`` already floats the window above other applications; this is
    a best-effort extra on macOS when PyObjC is installed.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication  # type: ignore

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


class _FlatButton:
    """Label-based button: native macOS buttons ignore background colours."""

    def __init__(self, parent: Any, text: str, color: str, hover: str, command: Any) -> None:
        import tkinter as tk

        self.color, self.hover, self.command = color, hover, command
        self.enabled = False
        self.widget = tk.Label(parent, text=text, fg="white", bg=COLORS["disabled"],
                               font=("Helvetica", 13, "bold"), padx=22, pady=9, cursor="arrow")
        self.widget.bind("<Button-1>", self._click)
        self.widget.bind("<Enter>", lambda _e: self.enabled and self.widget.configure(bg=self.hover))
        self.widget.bind("<Leave>", lambda _e: self.enabled and self.widget.configure(bg=self.color))

    def arm(self) -> None:
        self.enabled = True
        self.widget.configure(bg=self.color, cursor="hand2")

    def _click(self, _event: Any) -> None:
        if self.enabled:
            self.command()


class _Popup:
    def __init__(self, ui: "TkUI", req: ApprovalRequest, fut: "asyncio.Future[UserChoice]",
                 timeout: float, slot: int) -> None:
        import tkinter as tk

        self.ui, self.req, self.fut = ui, req, fut
        self.deadline = time.monotonic() + timeout
        self.closed = False
        c = COLORS

        win = self.win = tk.Toplevel(ui.root)
        win.title("MCP Guard - approve tool call?")
        win.configure(bg=c["bg"])
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", lambda: self.decide(BLOCK))
        win.bind("<Escape>", lambda _e: self.decide(BLOCK))

        # On macOS, force a consistent NSAppearance so the system cannot
        # override widget bg/fg colours in dark mode.
        if sys.platform == "darwin":
            try:
                ns_appearance = "NSAppearanceNameDarkAqua" if COLORS is _DARK else "NSAppearanceNameAqua"
                win.tk.call("::tk::unsupported::MacWindowStyle", "appearance", win._w, ns_appearance)
            except Exception:
                pass

        # Header -----------------------------------------------------------
        header = tk.Frame(win, bg=c["header"])
        header.pack(fill="x")
        tk.Frame(header, bg=c["accent"], width=6).pack(side="left", fill="y")
        tk.Label(header, text="Claude wants to use an MCP tool", bg=c["header"], fg=c["header_text"],
                 font=("Helvetica", 15, "bold"), padx=14, pady=12).pack(side="left")
        self.queue_label = tk.Label(header, text="", bg=c["header"], fg=c["accent"],
                                    font=("Helvetica", 11, "bold"), padx=14)
        self.queue_label.pack(side="right")

        body = tk.Frame(win, bg=c["bg"], padx=18, pady=12)
        body.pack(fill="both", expand=True)

        # Details grid -------------------------------------------------------
        grid = tk.Frame(body, bg=c["bg"])
        grid.pack(fill="x")
        what = "Tool" if req.method == "tools/call" else "Target"
        rows = [
            ("Requested by", f"{origin_label(req)}"),
            ("Client", f"{req.client}  ·  via {req.transport}"),
            ("MCP server", req.server),
            (what, req.tool or "-"),
            ("Method", req.method),
        ]
        if req.tool_description:
            desc = req.tool_description.strip()
            rows.append(("Description", desc[:400] + ("..." if len(desc) > 400 else "")))
        for r, (key, value) in enumerate(rows):
            tk.Label(grid, text=key, bg=c["bg"], fg=c["muted"], font=("Helvetica", 12),
                     anchor="nw").grid(row=r, column=0, sticky="nw", padx=(0, 14), pady=2)
            bold = key in (what, "MCP server")
            tk.Label(grid, text=bmp_safe(str(value)), bg=c["bg"], fg=c["text"], justify="left",
                     font=("Helvetica", 13, "bold" if bold else "normal"), anchor="w",
                     wraplength=470).grid(row=r, column=1, sticky="w", pady=2)

        badges = risk_badges(req)
        if badges:
            bar = tk.Frame(body, bg=c["bg"])
            bar.pack(fill="x", pady=(8, 0))
            for label, level in badges:
                tk.Label(bar, text=f" {label} ", bg=c[level], fg="white",
                         font=("Helvetica", 10, "bold"), padx=4, pady=2).pack(side="left", padx=(0, 6))

        # Arguments ------------------------------------------------------------
        tk.Label(body, text="Arguments", bg=c["bg"], fg=c["muted"], font=("Helvetica", 12),
                 anchor="w").pack(fill="x", pady=(12, 4))
        text_frame = tk.Frame(body, bg=c["code_bg"], highlightthickness=1, highlightbackground="#CBD5E1")
        text_frame.pack(fill="both", expand=True)
        scroll = tk.Scrollbar(text_frame)
        scroll.pack(side="right", fill="y")
        text = tk.Text(text_frame, height=10, width=72, wrap="word", font="TkFixedFont",
                       bg=c["code_bg"], fg=c["text"], relief="flat", padx=8, pady=6,
                       yscrollcommand=scroll.set, highlightthickness=0, borderwidth=0)
        text.insert("1.0", format_arguments(req.arguments))
        text.configure(state="disabled")  # display only - nothing the user types is sent
        text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=text.yview)

        # Footer ---------------------------------------------------------------
        footer = tk.Frame(body, bg=c["bg"])
        footer.pack(fill="x", pady=(12, 0))
        self.remember = tk.BooleanVar(value=False)
        tk.Checkbutton(footer, text=f"Remember my choice for {bmp_safe(req.target)}", variable=self.remember,
                       bg=c["bg"], fg=c["text"], activebackground=c["bg"], font=("Helvetica", 12),
                       highlightthickness=0).pack(side="left")

        actions = tk.Frame(body, bg=c["bg"])
        actions.pack(fill="x", pady=(10, 0))
        self.countdown = tk.Label(actions, text="", bg=c["bg"], fg=c["muted"], font=("Helvetica", 12))
        self.countdown.pack(side="left")
        self.allow_btn = _FlatButton(actions, "Allow", c["allow"], c["allow_hover"], lambda: self.decide(ALLOW))
        self.block_btn = _FlatButton(actions, "Block", c["block"], c["block_hover"], lambda: self.decide(BLOCK))
        self.allow_btn.widget.pack(side="right")
        self.block_btn.widget.pack(side="right", padx=(0, 10))

        # Placement: centred, cascaded per concurrent popup ----------------------
        win.update_idletasks()
        w, h = max(win.winfo_reqwidth(), 600), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        offset = (slot % 8) * 32
        x = max(0, (sw - w) // 2 + offset)
        y = max(0, (sh - h) // 3 + offset)
        win.geometry(f"{w}x{h}+{x}+{y}")

        self._raise()
        win.after(ARM_DELAY_MS, self._arm)
        self._tick()

    def _raise(self) -> None:
        win = self.win
        win.attributes("-topmost", True)
        win.deiconify()
        win.lift()
        _activate_app()
        win.focus_force()
        try:
            win.bell()
        except Exception:
            pass

    def _arm(self) -> None:
        if not self.closed:
            self.allow_btn.arm()
            self.block_btn.arm()

    def _tick(self) -> None:
        if self.closed:
            return
        remaining = max(0, int(self.deadline - time.monotonic() + 0.999))
        self.countdown.configure(text=f"Auto-block in {remaining}s",
                                 fg=COLORS["danger"] if remaining <= 10 else COLORS["muted"])
        self.win.after(250, self._tick)

    def set_queue_info(self, index: int, total: int) -> None:
        if not self.closed:
            self.queue_label.configure(text=f"{index} of {total} pending" if total > 1 else "")

    def decide(self, action: str) -> None:
        if self.closed:
            return
        choice = UserChoice(action=action, remember=bool(self.remember.get()))
        self.ui._resolve(self.fut, choice)
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except Exception:
            pass
        self.ui._forget(self.req.request_id)


class TkUI(UIBackend):
    name = "tk"

    def __init__(self) -> None:
        super().__init__()
        import tkinter as tk  # ImportError -> caller falls back to another backend

        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._popups: Dict[str, _Popup] = {}
        self._slot = 0
        # Creating the root here (main thread) also validates that a display
        # is available; TclError propagates to the backend selector.
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("MCP Guard")
        if sys.platform == "darwin":
            try:
                ns_appearance = "NSAppearanceNameDarkAqua" if _is_dark_mode() else "NSAppearanceNameAqua"
                self.root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", self.root._w, ns_appearance)
            except Exception:
                pass

    # asyncio thread -------------------------------------------------------
    def ask(self, req: ApprovalRequest, timeout: float) -> "asyncio.Future[UserChoice]":
        assert self.loop is not None
        fut: "asyncio.Future[UserChoice]" = self.loop.create_future()
        self._queue.put(("ask", req, fut, timeout))
        return fut

    def cancel(self, request_id: str) -> None:
        self._queue.put(("cancel", request_id))

    def stop(self) -> None:
        self._queue.put(("stop",))

    def dispose(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

    # main thread ------------------------------------------------------------
    def run_main(self, stop_event: threading.Event) -> None:
        def poll() -> None:
            try:
                while True:
                    cmd = self._queue.get_nowait()
                    if cmd[0] == "ask":
                        _, req, fut, timeout = cmd
                        self._open(req, fut, timeout)
                    elif cmd[0] == "cancel":
                        popup = self._popups.get(cmd[1])
                        if popup:
                            popup.close()
                    elif cmd[0] == "stop":
                        stop_event.set()
            except queue.Empty:
                pass
            except Exception:  # keep the UI loop alive whatever happens
                log.exception("UI loop error")
            if stop_event.is_set():
                for popup in list(self._popups.values()):
                    popup.close()
                self.root.quit()
                return
            self.root.after(POLL_MS, poll)

        self.root.after(POLL_MS, poll)
        self.root.mainloop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _open(self, req: ApprovalRequest, fut: "asyncio.Future[UserChoice]", timeout: float) -> None:
        if fut.done():  # cancelled before we got to it
            return
        try:
            slot = len(self._popups)
            self._popups[req.request_id] = _Popup(self, req, fut, timeout, slot)
        except Exception:  # never leave a request hanging on a UI bug: fail closed
            log.exception("Could not open popup for %s - blocking", req.target)
            self._resolve(fut, UserChoice(BLOCK))
            return
        self._refresh_queue_labels()

    def _forget(self, request_id: str) -> None:
        self._popups.pop(request_id, None)
        self._refresh_queue_labels()

    def _refresh_queue_labels(self) -> None:
        total = len(self._popups)
        for i, popup in enumerate(self._popups.values(), start=1):
            popup.set_queue_info(i, total)
