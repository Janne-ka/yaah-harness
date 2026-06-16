"""yaah.mcp — the McpSource PORT + its zero-config references (Static, Routing).
The I/O-bound source (file) is a swap-in adapter in yaah.adapters.mcp.

Mirrors yaah.prompts / yaah.data: a source fetches the MCP servers offered to a
model, by key. MCP is model-initiated agent config (not a pipeline node).
Optional layer, not the kernel.
"""
from .mcp_source import McpSource, normalize_servers
from .routing_mcp_source import RoutingMcpSource
from .static_mcp_source import StaticMcpSource

__all__ = ["McpSource", "StaticMcpSource", "RoutingMcpSource", "normalize_servers"]
