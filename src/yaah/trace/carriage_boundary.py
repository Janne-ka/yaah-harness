"""CarriageBoundaryNode — drain the R6 envelope carriage at the SERVE boundary.

Used by: `build._wrap_node`, which wraps EVERY built node in one of these when
the worker's tracer is a carriage (`is_carriage` True, i.e. EnvelopeTracer).
Where: the outermost node wrapper — the last code that touches a reply before
it goes back over Comms to whoever requested it.
Why: the drain used to live inside `Agent.invoke` (assessment H6/#6), which was
wrong twice over: (a) a NESTED agent sharing the tracer and correlation id —
exactly the R12 broker node — drained the corr MID-stage, and its caller (a tool
reading only `reply.payload`) discarded the headers: spans permanently lost;
(b) non-agent nodes (shell / transform / gate) never drained at all, so in a
remote-worker topology their spans never travelled. At the serve boundary the
drain happens exactly once per request, for every node type, when the reply is
genuinely leaving for the requester. The requester is responsible for the other
half of the contract: a terminal consumer (the harness) `ingest`s the records;
a NON-terminal requester (the context_broker tool, any nested `node:` call)
must re-`ingest` them into its own tracer so they ride out with ITS reply.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any

from ..core import Envelope, NodeConfig


class CarriageBoundaryNode:
    def __init__(self, inner: Any, tracer: Any) -> None:
        self._inner = inner
        self._tracer = tracer

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        out = await self._inner.invoke(input, config)
        records = await self._tracer.drain(input.correlation_id)
        if records:
            # merge, don't overwrite: the inner node may itself have received
            # (and chosen to forward) carriage records from ITS nested calls.
            existing = out.headers.get("trace") or []
            out.headers["trace"] = list(existing) + records
        return out
