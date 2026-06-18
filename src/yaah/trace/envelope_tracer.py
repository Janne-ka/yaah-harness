"""EnvelopeTracer — the R6 carriage: records ride on the envelope.

Used by: a runtime whose `trace.mode: envelope`. Picked when there is NO shared
bus (no broker subscriber that can collect the stream out-of-band) — typically a
remote worker / serverless function that ALWAYS has an envelope it's about to
reply on, but does NOT have a sink it can publish to. The worker emits while it
works; `drain(corr)` returns and clears the buffered records; the caller (the
agent / harness) attaches them to the outgoing envelope's `headers["trace"]` and
the orchestrator merges them on receive.
Where: the engine tracing core (a no-external-system default — pure in-memory
buffer, no Comms, no disk).
Why: bus carriage assumes a subscriber is listening; envelope carriage doesn't
— spans accrete on the artifact that is GOING TO BE DELIVERED anyway. Pairs
with BusTracer (the with-bus case) under the same Tracer port; same emit-site
code on both sides.

Size cap: a per-corr buffer is bounded by `buffer_max` records. When the cap is
hit on emit, the OLDEST record for that corr is dropped and a single
`{name: "trace_truncated", corr, dropped}` marker is appended on drain. This
keeps the envelope bounded (otherwise a long-running stage with chatty
contributors could balloon a reply envelope past any transport's frame size).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from .contributor import TraceContributor
from .record import project
from .span import Span


class EnvelopeTracer:
    is_carriage = True  # R6 — only carriage tracer; the Agent drains on reply

    def __init__(self, *, contributors: Sequence[TraceContributor] = (),
                 buffer_max: int = 256) -> None:
        self._contributors = list(contributors)
        self.captures: FrozenSet[str] = frozenset(c.name for c in self._contributors)
        self._buffer_max = max(1, int(buffer_max))
        # per-corr FIFO; we never need cross-corr ordering for the envelope carriage.
        self._by_corr: Dict[str, List[Dict[str, Any]]] = {}
        self._dropped: Dict[str, int] = {}

    async def emit(self, span: Span) -> None:
        record = project(span, self._contributors)
        corr = record.get("corr") or ""
        buf = self._by_corr.setdefault(corr, [])
        buf.append(record)
        if len(buf) > self._buffer_max:
            # drop oldest (FIFO); count it so drain can append a single marker
            buf.pop(0)
            self._dropped[corr] = self._dropped.get(corr, 0) + 1

    async def drain(self, corr: str) -> List[Dict[str, Any]]:
        out = self._by_corr.pop(corr, [])
        dropped = self._dropped.pop(corr, 0)
        if dropped:
            out.append({"name": "trace_truncated", "corr": corr, "dropped": dropped})
        return out

    async def ingest(self, records: List[Dict[str, Any]]) -> None:
        """Merge remote records into this tracer's per-corr buffer (R6) so they
        ride the next outgoing envelope's drain. Same cap policy as `emit`: when
        the buffer is full, the OLDEST record is evicted and a single
        `trace_truncated` marker is appended on drain."""
        for record in records or ():
            corr = record.get("corr") or ""
            buf = self._by_corr.setdefault(corr, [])
            buf.append(record)
            if len(buf) > self._buffer_max:
                buf.pop(0)
                self._dropped[corr] = self._dropped.get(corr, 0) + 1

    def last_model_call_span(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        """ADR-0003: scan this corr's per-corr buffer in reverse for the most
        recent model_call span. The buffer is not cleared by this read — only
        drain() clears, since attachers run BEFORE the agent's reply carriage
        drain (the order: agent emits span → attacher reads span → agent
        returns reply → carriage wrapper drains for the outgoing envelope)."""
        buf = self._by_corr.get(correlation_id)
        if not buf:
            return None
        for record in reversed(buf):
            if record.get("name") == "model_call":
                return record
        return None
