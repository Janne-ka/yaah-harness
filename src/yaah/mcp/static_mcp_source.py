"""StaticMcpSource — MCP server configs held in memory, keyed by name.

Used by: tests and small/inline deployments (the runtime's `static` mcp source).
Where: when the server set is fixed and non-secret.
Why: get('default') -> the servers map. The simplest McpSource.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from .mcp_source import McpSource, normalize_servers


class StaticMcpSource(McpSource):
    def __init__(self, configs: Dict[str, Dict[str, Any]]) -> None:
        self._configs = dict(configs)

    async def get(self, key: str, **opts: Any) -> Dict[str, Any]:
        if key not in self._configs:
            raise LookupError("no mcp config {!r}; have {}".format(key, sorted(self._configs)))
        return normalize_servers(self._configs[key])
