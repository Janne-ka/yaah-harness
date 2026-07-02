"""RoutingBackend — an ApiProvider that dispatches by the model's provider prefix.

Used by: the runtime (built from the root config's `providers`) and apps; given
to every Agent / AgentLoopNode as its single backend.
Where: the seam where `NodeConfig.model` selects a provider.
Why: a model string 'provider:rest' routes to the backend registered for
'provider' (called with model='rest'), so choosing fake/claude/litellm for a
node is pure config. The prefix-dispatch lives in PrefixRouter (shared with the
prompt/data/mcp routers); this class forwards two verbs — `stream` (the one
model seam) and the tool-loop `turn` — and maps an empty rest back to None
(provider default model).

`stream()` is the single routing verb: every leaf backend implements it
natively, so stream-based consumers (operator UI, trace recorders, hedge logic)
never need a capability check. `supports_turn()` stays a real signal because
claude_cli has no native turn() — that capability gap is by design (claude
handles its own tool loop), so the check remains until (if ever) a turn() shim
is added for claude_cli.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional

from ..prefix_router import PrefixRouter
from . import api_provider as _ap


# Generic parameter is Any: leaf backends are structural ApiProviders,
# duck-typed on `complete` / `turn` / `stream` at dispatch time. That runtime
# dispatch is the real contract; a static Protocol annotation would only be a
# type-only ornament over it.
class RoutingBackend(PrefixRouter[Any]):
    label = "backend"
    prefix = "provider"

    def stream(self, context: _ap.Context, **opts: Any) -> AsyncIterator[_ap.StreamEvent]:
        """Forward an ApiProvider.stream call to the selected provider. Every leaf
        backend implements stream() natively; no capability check needed. The
        context's `model` is rewritten to the post-prefix rest so the leaf sees
        the canonical model name, not 'provider:model'."""
        model = context.get("model")
        backend, rest = self._select(model)
        # Rebuild the context with the resolved leaf-side model. Use dict() so
        # we don't mutate the caller's context.
        new_ctx: _ap.Context = dict(context)  # type: ignore[assignment]
        new_ctx["model"] = (rest or None)
        # stream_of adapts a collected-only leaf (no native stream(), e.g. an
        # external legacy backend) into a one-shot stream, so routing to it works.
        return _ap.stream_of(backend, new_ctx, **opts)

    async def turn(self, messages: List[dict], tools: List[dict], *,
                   model: Optional[str] = None, **opts: Any) -> dict:
        """Forward the tool-loop `turn` to the selected provider. Only providers
        that implement turn (litellm, scripted-tool, fake-tool) support tools."""
        backend, rest = self._select(model)
        if not hasattr(backend, "turn"):
            raise TypeError(
                "provider for model {!r} does not support tool-use (no `turn`)".format(model))
        return await backend.turn(messages, tools, model=(rest or None), **opts)

    def supports_turn(self, model: Optional[str] = None) -> bool:
        """Does the provider SELECTED by `model` implement the tool-loop `turn`?
        The router itself defines a `turn` method, so a structural isinstance
        on the ROUTER is true no matter which leaf the model routes
        to (assessment H4) — capability must be answered AFTER routing. The
        Agent asks this before choosing tool-loop vs manifest fallback (R11),
        so a claude_cli-routed call renders the manifest instead of crashing
        mid-loop on the TypeError above."""
        backend, rest = self._select(model)
        if hasattr(backend, "supports_turn"):  # a nested router answers for ITS leaf
            return bool(backend.supports_turn(rest or None))
        return callable(getattr(backend, "turn", None))
