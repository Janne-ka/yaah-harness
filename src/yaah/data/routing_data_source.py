"""RoutingDataSource — dispatch by a 'source:' prefix on the data key.

Used by: the runtime (built from the root config's `data_sources`) and given to
GetNodes as their single data source.
Where: the seam where a 'get' node's key selects a source.
Why: 'git:HEAD' -> git source fetch('HEAD'); 'file:app/x.rb' -> file source
fetch('app/x.rb'). The prefix-dispatch lives in PrefixRouter (shared with the
prompt/sink/mcp/backend routers); this class only forwards the `fetch` verb.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any

from ..prefix_router import PrefixRouter
from .data_source import DataSource


class RoutingDataSource(PrefixRouter[DataSource]):
    label = "data source"
    prefix = "source"

    async def fetch(self, key: str, **opts: Any) -> str:
        source, rest = self._select(key)
        return await source.fetch(rest, **opts)
