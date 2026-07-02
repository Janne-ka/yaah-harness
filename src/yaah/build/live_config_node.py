"""LiveConfigNode — per-call leaf-config refresh wrapper around a built node.

Used by: `build()` / `serve_from_config()` — the OUTERMOST wrapper around each
registered node when live config is on (root `live_config: true`).
Where: the dispatch seam; the comms still holds the frozen-at-build NodeConfig,
this wrapper swaps in the refreshed one before the node sees it.
Why: keeps the live-vars mechanism out of every node implementation — nodes
already read their NodeConfig per invocation (model, timeout, extras), so
refreshing the config object at the seam makes every node type live-capable
with zero node changes. The mutable surface lives in `LiveLeafConfig`.

Targets Python 3.9+.
"""
from __future__ import annotations

from ..core import Node, Envelope, NodeConfig
from .live_leaf_config import LiveLeafConfig


class LiveConfigNode(Node):
    def __init__(self, inner: Node, role: str, live: LiveLeafConfig) -> None:
        self._inner = inner
        self._role = role
        self._live = live

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return await self._inner.invoke(input, self._live.refresh(self._role, config))
