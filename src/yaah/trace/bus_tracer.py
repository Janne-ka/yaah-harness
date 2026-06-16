"""BusTracer — the v1 carriage: publish each record to the `trace` topic.

Used by: the build/runtime when `trace.mode: tracer` (the default); injected
into the harness and agents.
Where: the engine tracing core (uses ONLY the Comms port — no external system,
so it stays in the engine; the sinks that persist the stream are adapters).
Why: emit-as-it-happens for near-real-time observability. Any sink/UX consumer
(file, Langfuse, a live progress printer) just subscribes to the topic; with no
subscriber, publish is a no-op, so default-on tracing is genuinely zero-cost.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Sequence

from ..comms import Comms
from ..core import Envelope, Kind
from .contributor import TraceContributor
from .record import project
from .span import Span


class BusTracer:
    is_carriage = False  # delivery is via the bus, not via the envelope

    # Per-call ingest cap (assessment #6): ingested records come from REMOTE
    # reply headers — re-publishing an unbounded batch would let one runaway
    # worker flood every subscribed sink.
    INGEST_MAX = 1000

    def __init__(self, comms: Comms, *, topic: str = "trace",
                 contributors: Sequence[TraceContributor] = ()) -> None:
        self._comms = comms
        self._topic = topic
        self._contributors = list(contributors)
        self.captures: FrozenSet[str] = frozenset(c.name for c in self._contributors)

    async def emit(self, span: Span) -> None:
        record = project(span, self._contributors)
        await self._comms.publish(self._topic, Envelope(Kind.EVENT, record))

    async def drain(self, corr: str) -> List[Dict[str, Any]]:
        return []  # delivery is via the bus, not via envelope headers

    async def ingest(self, records: List[Dict[str, Any]]) -> None:
        """Re-publish remote records to the local trace topic so subscribed sinks
        see them just like local emits. Records are already projected — no
        contributor pass. Non-dict records are dropped and the batch is capped
        at INGEST_MAX (a single truncation marker is published for the rest) —
        the input is remote-controlled data (assessment #6)."""
        clean = [r for r in (records or ()) if isinstance(r, dict)]
        if len(clean) > self.INGEST_MAX:
            kept = self.INGEST_MAX - 1  # leave room for the marker: published total == INGEST_MAX
            dropped = len(clean) - kept
            clean = clean[:kept] + [{"name": "trace_truncated", "dropped": dropped}]
        for record in clean:
            await self._comms.publish(self._topic, Envelope(Kind.EVENT, record))
