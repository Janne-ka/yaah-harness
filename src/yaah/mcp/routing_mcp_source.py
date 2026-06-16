"""RoutingMcpSource — dispatch by a 'source:' prefix on the mcp key.

Used by: the runtime (built from the root config's `mcp_sources`) and given to
Agents as their single MCP source.
Where: the seam where an agent's `mcp` ref selects a source.
Why: 'registry:acme-prod' -> registry source get('acme-prod'). The
prefix-dispatch lives in PrefixRouter (shared with the prompt/data/backend
routers); this class only forwards the `get` verb (returning a servers map).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from ..prefix_router import PrefixRouter
from .mcp_source import McpSource


class RoutingMcpSource(PrefixRouter[McpSource]):
    label = "mcp source"
    prefix = "source"

    async def get(self, key: str, **opts: Any) -> Dict[str, Any]:
        source, rest = self._select(key)
        return await source.get(rest, **opts)
