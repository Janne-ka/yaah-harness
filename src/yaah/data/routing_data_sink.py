"""RoutingDataSink — dispatch a write by a 'sink:' prefix on the key.

Used by: the runtime (built from the root config's `data_sinks`) and given to
PostNodes as their single sink.
Where: the seam where a 'post' node's key selects a sink.
Why: 'file:out/report.html' -> file sink store('out/report.html', value). The
prefix-dispatch lives in PrefixRouter (shared with the prompt/source/mcp/backend
routers); this class only forwards the `store` verb (key + value).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any

from ..prefix_router import PrefixRouter
from .data_sink import DataSink


class RoutingDataSink(PrefixRouter[DataSink], DataSink):
    label = "data sink"
    prefix = "sink"

    async def store(self, key: str, value: Any, **opts: Any) -> str:
        sink, rest = self._select(key)
        return await sink.store(rest, value, **opts)
