"""FileMcpSource — read MCP server configs from JSON files (the governed case).

Used by: the runtime's `file` mcp source. get('acme-prod') reads
<dir>/acme-prod.json — a per-environment file that can hold endpoints + auth,
kept in a governed location rather than the pipeline config.
Where: local files / a mounted secrets dir.
Why: the fetchable "agentMcpGet" — change which servers an agent gets by editing
a file, not the pipeline. mtime-cached (like FilePromptSource): no re-read until
the file changes, hot-reload preserved.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Tuple

from ...mcp import normalize_servers
from ...safepath import safe_join
from ...mcp import McpSource


class FileMcpSource(McpSource):
    def __init__(self, base_dir: str, ext: str = ".json") -> None:
        self._base = base_dir
        self._ext = ext
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}  # path -> (mtime, servers)

    async def get(self, key: str, **opts: Any) -> Dict[str, Any]:
        # safe_join (assessment cluster 5 security #3): a relative `key` like
        # `../../../etc/secrets` previously resolved straight through. Absolute
        # keys still pass through (operator's explicit intent).
        path = safe_join(self._base, key if os.path.isabs(key) else key + self._ext)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        cached = self._cache.get(path)
        if cached is not None and mtime is not None and cached[0] == mtime:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            servers = normalize_servers(json.load(f))
        if mtime is not None:
            self._cache[path] = (mtime, servers)
        return servers
