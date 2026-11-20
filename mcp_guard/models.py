"""Data types shared by the proxies, the broker and the UI."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

ALLOW = "allow"
BLOCK = "block"
ASK = "ask"
ACTIONS = (ALLOW, BLOCK, ASK)


@dataclass
class ApprovalRequest:
    """A single intercepted MCP request awaiting a decision."""

    server: str                      # logical MCP server name (config key)
    method: str                      # JSON-RPC method, e.g. "tools/call"
    tool: Optional[str] = None       # tool / resource / prompt name
    arguments: Any = None            # request parameters
    client: str = "unknown"          # clientInfo reported in `initialize`
    origin: str = "unknown"          # "claude-desktop" | "claude-web" | ...
    transport: str = "stdio"         # "stdio" | "http"
    tool_description: Optional[str] = None
    annotations: Dict[str, Any] = field(default_factory=dict)
    jsonrpc_id: Any = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRequest":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def target(self) -> str:
        return f"{self.server}/{self.tool or self.method}"


@dataclass
class Decision:
    """Outcome for an :class:`ApprovalRequest`."""

    request_id: str
    action: str                      # ALLOW | BLOCK
    reason: str = ""
    decided_by: str = "user"         # user | policy | remembered | timeout | error | cancelled
    latency_ms: int = 0

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Decision":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class UserChoice:
    """What the user picked in the popup."""

    action: str                      # ALLOW | BLOCK
    remember: bool = False
