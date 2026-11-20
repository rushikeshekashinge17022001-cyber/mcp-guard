"""Transport-independent interception logic.

A :class:`SessionContext` passively observes the JSON-RPC traffic of one MCP
session in both directions to learn

* who the client is (``initialize`` -> ``clientInfo``),
* what the server is (``initialize`` result -> ``serverInfo``),
* the tool catalogue (``tools/list`` results: descriptions + annotations),

so the approval popup can show *what* a tool does, not just its name.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional, Set, Tuple

from mcp_guard import jsonrpc
from mcp_guard.models import ApprovalRequest

log = logging.getLogger(__name__)

_MAX_TRACKED_IDS = 1000


class SessionContext:
    def __init__(self, server: str, origin: str, transport: str, gated_methods: Iterable[str]) -> None:
        self.server = server
        self.origin = origin
        self.transport = transport
        self.gated_methods: Set[str] = set(gated_methods)
        self.client = "unknown"
        self.server_info: Optional[str] = None
        self.tools: Dict[str, Tuple[Optional[str], Dict[str, Any]]] = {}
        self._watch_ids: Dict[Any, str] = {}   # request id -> method we want the result of

    # ------------------------------------------------------------ observing
    def observe_client(self, msg: Any) -> None:
        for m in msg if isinstance(msg, list) else [msg]:
            if not jsonrpc.is_request(m):
                continue
            method = m.get("method")
            if method == "initialize":
                info = (m.get("params") or {}).get("clientInfo") or {}
                name, version = info.get("name"), info.get("version")
                if name:
                    self.client = f"{name} {version}".strip() if version else str(name)
            if method in ("initialize", "tools/list"):
                if len(self._watch_ids) > _MAX_TRACKED_IDS:
                    self._watch_ids.clear()
                self._watch_ids[_key(m["id"])] = method

    def observe_server(self, msg: Any) -> None:
        for m in msg if isinstance(msg, list) else [msg]:
            if not jsonrpc.is_response(m):
                continue
            method = self._watch_ids.pop(_key(m.get("id")), None)
            result = m.get("result")
            if method is None or not isinstance(result, dict):
                continue
            if method == "initialize":
                info = result.get("serverInfo") or {}
                if info.get("name"):
                    self.server_info = f"{info.get('name')} {info.get('version', '')}".strip()
            elif method == "tools/list":
                for tool in result.get("tools") or []:
                    if isinstance(tool, dict) and tool.get("name"):
                        ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
                        self.tools[str(tool["name"])] = (tool.get("description"), ann)

    # ------------------------------------------------------------- gating
    def is_gated(self, msg: Any) -> bool:
        return jsonrpc.is_request(msg) and msg.get("method") in self.gated_methods

    def build_request(self, msg: Dict[str, Any]) -> ApprovalRequest:
        target = jsonrpc.request_target(msg)
        description, annotations = self.tools.get(target or "", (None, {}))
        return ApprovalRequest(
            server=self.server,
            method=str(msg.get("method")),
            tool=target,
            arguments=jsonrpc.request_arguments(msg),
            client=self.client,
            origin=self.origin,
            transport=self.transport,
            tool_description=description,
            annotations=dict(annotations or {}),
            jsonrpc_id=msg.get("id"),
        )


def _key(req_id: Any) -> Any:
    # JSON-RPC ids may be str or int; normalise so lookups are hashable/stable.
    return req_id if isinstance(req_id, (str, int)) else repr(req_id)

