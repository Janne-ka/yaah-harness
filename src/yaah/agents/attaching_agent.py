"""AttachingAgent — wraps an Agent; merges attacher outputs onto its reply.

Used by: `build/builders.py::_build_agent` when the agent spec carries
`attach: [...]`. Transparent to the harness — invoke() returns an Envelope
just like Agent does.
Where: the harness layer's wrapper-as-capability slot, parallel to
OnceNode (idempotency wrap) and CarriageBoundaryNode (trace wrap).
Why: keep base Agent naive about cost/usage/observability data; isolate
the "post-invoke attach" concern to one wrapper class. See ADR-0003.

Tracer dependency: the wrapper reads the tracer's most recent model_call
span for THIS correlation (not the global last span — concurrent runs
share the buffer; nested-agent / broker setups would race a parameter-less
lookup). The Tracer protocol's `last_model_call_span(corr)` accessor is
the seam. NullTracer cannot serve attachers; the builder rejects at load
time when `attach` is set without the required captures on the tracer.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..core import Envelope, NodeConfig
from .attacher import Attacher


class AttachingAgent:
    def __init__(self, inner: Any, attachers: List[Attacher], tracer: Any) -> None:
        # `inner` is an Agent; typed Any to avoid an import cycle (Agent is in
        # the same package but the wrapper doesn't need its full surface, only
        # `invoke()`).
        self._inner = inner
        self._attachers = list(attachers)
        self._tracer = tracer

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        result = await self._inner.invoke(input, config)
        # per-corr lookup: in concurrent runs (R12 broker, nested agents) the
        # tracer's flat span buffer holds spans from many correlations. A
        # parameter-less last_* would return whichever ran last globally.
        span = self._tracer.last_model_call_span(result.correlation_id)
        attached: Dict[str, Any] = {}
        for a in self._attachers:
            attached.update(a.attach(result, span))
        if not attached:
            return result
        # merge over the agent's output payload, attacher keys winning a
        # collision (the attacher's whole point is to add named data)
        return result.reply(result.kind, **{**result.payload, **attached})
