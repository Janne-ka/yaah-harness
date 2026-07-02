"""RoutingPromptSource — dispatch by a 'source:' prefix on the prompt key.

Used by: the runtime (built from the root config's `prompt_sources`) and given
to Agents as their single prompt source.
Where: the seam where a node's `prompt` string selects a source.
Why: 'file:eval' -> file source get('eval'). The prefix-dispatch itself lives in
PrefixRouter (shared with the data/mcp/backend routers); this class only forwards
the `get` verb, so a node's prompt is one config string and switching source is
config only.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any

from ..prefix_router import PrefixRouter
from .prompt_source import PromptSource


class RoutingPromptSource(PrefixRouter[PromptSource], PromptSource):
    label = "prompt source"
    prefix = "source"

    async def get(self, key: str, **opts: Any) -> str:
        source, rest = self._select(key)
        return await source.get(rest, **opts)
