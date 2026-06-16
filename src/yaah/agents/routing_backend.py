"""RoutingBackend — a ModelBackend that dispatches by the model's provider prefix.

Used by: the runtime (built from the root config's `providers`) and apps; given
to every Agent as its single backend.
Where: the seam where `NodeConfig.model` selects a provider.
Why: a model string 'provider:rest' routes to the backend registered for
'provider' (called with model='rest'), so choosing fake/claude/litellm for a
node is pure config. The prefix-dispatch lives in PrefixRouter (shared with the
prompt/data/mcp routers); this class forwards two verbs — `complete` and the
tool-loop `turn` — and maps an empty rest back to None (provider default model).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, List, Optional

from ..prefix_router import PrefixRouter
from .model_backend import ModelBackend


class RoutingBackend(PrefixRouter[ModelBackend]):
    label = "backend"
    prefix = "provider"

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        backend, rest = self._select(model)
        return await backend.complete(prompt, model=(rest or None), **opts)

    async def turn(self, messages: List[dict], tools: List[dict], *,
                   model: Optional[str] = None, **opts: Any) -> dict:
        """Forward the tool-loop `turn` to the selected provider. Only providers
        that implement turn (litellm, scripted-tool) support tools."""
        backend, rest = self._select(model)
        if not hasattr(backend, "turn"):
            raise TypeError(
                "provider for model {!r} does not support tool-use (no `turn`)".format(model))
        return await backend.turn(messages, tools, model=(rest or None), **opts)

    def supports_turn(self, model: Optional[str] = None) -> bool:
        """Does the provider SELECTED by `model` implement the tool-loop `turn`?
        The router itself defines a `turn` method, so a structural ToolBackend
        isinstance on the ROUTER is true no matter which leaf the model routes
        to (assessment H4) — capability must be answered AFTER routing. The
        Agent asks this before choosing tool-loop vs manifest fallback (R11),
        so a claude_cli-routed call renders the manifest instead of crashing
        mid-loop on the TypeError above."""
        backend, rest = self._select(model)
        if hasattr(backend, "supports_turn"):  # a nested router answers for ITS leaf
            return bool(backend.supports_turn(rest or None))
        return callable(getattr(backend, "turn", None))
