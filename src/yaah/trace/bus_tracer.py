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

from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from ..comms import Comms
from ..core import Envelope, Kind
from .contributor import TraceContributor
from .tracer import Tracer
from .record import project
from .span import Span


class BusTracer(Tracer):
    is_carriage = False  # delivery is via the bus, not via the envelope

    # Per-call ingest cap (assessment #6): ingested records come from REMOTE
    # reply headers — re-publishing an unbounded batch would let one runaway
    # worker flood every subscribed sink.
    INGEST_MAX = 1000

    # ADR-0003: cap on the per-corr "last model_call span" dict — bounded by
    # concurrent corrs, not pipeline depth. The dict holds exactly one record
    # per corr (the most recent model_call); when the count exceeds the cap,
    # the oldest-inserted entry is dropped. Most pipelines run sequentially
    # so 256 entries is generous; a runaway concurrent fan-out would still be
    # bounded.
    ATTACH_BUFFER_MAX = 256

    def __init__(self, comms: Comms, *, topic: str = "trace",
                 contributors: Sequence[TraceContributor] = ()) -> None:
        self._comms = comms
        self._topic = topic
        self._contributors = list(contributors)
        self.captures: FrozenSet[str] = frozenset(c.name for c in self._contributors)
        # ADR-0003: one slot per corr for the most recent model_call record.
        # Populated in emit (in addition to publishing). Read by attachers via
        # last_model_call_span. Insertion-ordered (Python 3.7+ dict), so the
        # oldest entry is evicted when the cap is hit.
        self._last_model_call: Dict[str, Dict[str, Any]] = {}

    async def emit(self, span: Span) -> None:
        record = project(span, self._contributors)
        await self._comms.publish(self._topic, Envelope(Kind.EVENT, record))
        # ADR-0003: remember model_call records per corr for attachers
        if span.name == "model_call":
            corr = record.get("corr") or ""
            # if updating existing entry, preserve insertion order by removing
            # the old key first; otherwise the cap-eviction would treat this as
            # the freshest entry and evict a different (older) corr.
            if corr in self._last_model_call:
                del self._last_model_call[corr]
            self._last_model_call[corr] = record
            if len(self._last_model_call) > self.ATTACH_BUFFER_MAX:
                # drop oldest-inserted entry (FIFO)
                oldest = next(iter(self._last_model_call))
                del self._last_model_call[oldest]

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

    def last_model_call_span(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        """ADR-0003: return the most recent model_call record for this corr from
        the small per-corr buffer maintained by emit(). The buffer is bounded by
        ATTACH_BUFFER_MAX (one slot per active corr)."""
        return self._last_model_call.get(correlation_id)
