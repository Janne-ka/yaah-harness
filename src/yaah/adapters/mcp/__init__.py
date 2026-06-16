"""MCP sources (adapters). I/O-bound implementations of the McpSource port
(which, with the Static/Routing references, stays in yaah.mcp).
"""
from .file_mcp_source import FileMcpSource

__all__ = ["FileMcpSource"]
