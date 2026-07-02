"""McpSource — the interface for fetching an agent's MCP server config ("get").

Used by: an Agent (resolves its `mcp` ref) and the runtime (builds one from the
root config's `mcp_sources`). Implemented by: Static/File/Routing sources.
Where: the seam where the MCP servers offered to a model come from — inline, a
file, or a governed registry/cloud. MCP is MODEL-initiated capability (agent
config), never a pipeline node — see docs/agent-tools.md.
Why: MCP servers are endpoints + auth that vary by environment; keeping them
fetchable (this is the "agentMcpGet") keeps secrets/endpoints out of the pipeline
file and per-environment swappable, exactly like prompts. Returns the servers map
({serverName: serverDef}); the backend wraps it (claude `--mcp-config`).

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class McpSource(Protocol):
    @abstractmethod
    async def get(self, key: str, **opts: Any) -> Dict[str, Any]:
        ...


def normalize_servers(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either the claude shape {"mcpServers": {...}} or a bare servers map,
    and always return the servers map ({serverName: serverDef})."""
    if isinstance(cfg, dict) and "mcpServers" in cfg and isinstance(cfg["mcpServers"], dict):
        return cfg["mcpServers"]
    return cfg
