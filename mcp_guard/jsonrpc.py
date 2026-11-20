"""Minimal JSON-RPC 2.0 helpers for MCP messages."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

# Server-defined error code used for requests denied by MCP Guard.
BLOCKED_ERROR_CODE = -32001
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def try_parse(raw: bytes) -> Any:
    """Parse one JSON-RPC message; return ``None`` if it is not valid JSON."""
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def dumps(msg: Any) -> bytes:
    """Serialize a message as a single newline-terminated line (stdio framing)."""
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def is_request(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" in msg and msg["id"] is not None


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" in msg and "id" not in msg


def is_response(msg: Any) -> bool:
    return isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg)


def error_response(req_id: Any, code: int, message: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def block_response(request: Dict[str, Any], reason: str, style: str = "tool_error") -> Dict[str, Any]:
    """Build the reply sent to the client when ``request`` is blocked.

    For ``tools/call`` the default is an MCP *tool error* result: the spec
    recommends reporting tool failures inside the result so the model can see
    the denial and adapt (e.g. tell the user), instead of the host treating
    it as a protocol failure. Other methods always get a JSON-RPC error.
    """
    req_id = request.get("id")
    text = f"Request blocked by MCP Guard: {reason}"
    if request.get("method") == "tools/call" and style == "tool_error":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    return error_response(req_id, BLOCKED_ERROR_CODE, text, {"blockedBy": "mcp-guard", "reason": reason})


def request_target(msg: Dict[str, Any]) -> Optional[str]:
    """Name of the tool / prompt / resource a request addresses, if any."""
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return None
    method = msg.get("method")
    if method in ("tools/call", "prompts/get"):
        name = params.get("name")
        return str(name) if name is not None else None
    if method in ("resources/read", "resources/subscribe"):
        uri = params.get("uri")
        return str(uri) if uri is not None else None
    return None


def request_arguments(msg: Dict[str, Any]) -> Any:
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return params
    if msg.get("method") in ("tools/call", "prompts/get"):
        return params.get("arguments", {})
    return {k: v for k, v in params.items() if k != "_meta"}
