"""MCP Guard - interactive authorization for Model Context Protocol traffic.

MCP Guard sits between Claude (Desktop or Web) and the MCP servers it talks
to. Every tool invocation is held until the user explicitly allows or blocks
it from an always-on-top confirmation popup (or a policy rule decides).

Components
----------
* ``mcp_guard.proxy.stdio_proxy``  - transparent stdio wrapper for local
  servers launched by Claude Desktop.
* ``mcp_guard.proxy.http_gateway`` - Streamable-HTTP reverse proxy for remote
  connectors used by Claude Web (and Claude Desktop "custom connectors").
* ``mcp_guard.daemon``             - background approval broker + popup UI.
"""

__version__ = "1.0.0"
