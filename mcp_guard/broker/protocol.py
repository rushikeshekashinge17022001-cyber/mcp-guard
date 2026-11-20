"""Wire protocol between proxies and the approval daemon.

Transport: TCP on the loopback interface, newline-delimited JSON.

Every connection starts with ``{"op": "hello", "token": ...}``; the token is
read from ``~/.mcp-guard/daemon.json`` (mode 0600), so only processes of the
same OS user can talk to the daemon.

One connection carries exactly one operation. For ``decide`` the connection
*is* the lifetime of the pending request: if the proxy closes it (because the
client cancelled the request or the proxy died) the daemon dismisses the popup.

Operations::

    -> {"op": "decide", "request": <ApprovalRequest>}
    <- {"op": "decision", "decision": <Decision>}

    -> {"op": "ping"}            <- {"op": "pong", "version": "...", "pid": 123}
    -> {"op": "status"}          <- {"op": "status", "pending": [<ApprovalRequest>...]}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1
# Tool arguments may legitimately be large (file contents, etc.).
STREAM_LIMIT = 64 * 1024 * 1024


class ProtocolError(Exception):
    pass


async def read_message(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    """Read one message; ``None`` on clean EOF."""
    line = await reader.readline()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("message must be an object")
    return msg


async def write_message(writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
    writer.write(json.dumps(msg, default=str).encode("utf-8") + b"\n")
    await writer.drain()
