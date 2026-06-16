"""Tracer — the port emit sites call to record a measurement.

Used by: the harness, the Agent, and run_tool_loop (emit). Injected via
BuildContext like the model backend.
Where: the engine tracing core defines the PORT (R1); the zero-/no-external-
system defaults (NullTracer, RecordingTracer, BusTracer, EnvelopeTracer) live
beside it. Every sink (file / Langfuse / OTLP) is a swap-in adapter consuming
the carried records.
Why: one tiny interface — `emit` + `drain` + `captures` — so emit sites stay
clean and the CARRIAGE is config-selected: `none` (NullTracer) or `tracer` (Bus,
the v1 default) or `envelope` (R6, no shared bus — spans accrete in an in-memory
per-corr buffer; the carrier process drains and attaches them to its outgoing
envelope's `headers["trace"]` so the orchestrator sees them on reply).
`captures` lets an emit site skip gathering raw material a disabled capture
would never use (e.g. claude --output-format json for cost).

R6 carriage rule: `drain(corr)` returns AND CLEARS the carriage-side buffer —
the spans are about to travel on an envelope; we must not also re-deliver them
locally. The non-carriage tracers (Null/Bus) return [] — they deliver via their
own channel and have nothing to hand over. RecordingTracer's `drain` is part of
the same port (clears); tests inspect `tracer.records` (the unconditional list)
instead of drain.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Protocol, runtime_checkable

from .span import Span


@runtime_checkable
class Tracer(Protocol):
    captures: FrozenSet[str]  # enabled capture names — gate raw-material gathering
    is_carriage: bool          # True for envelope carriage; False for bus/null/recording —
                                # tells the Agent whether to drain on reply (R6)

    async def emit(self, span: Span) -> None:
        """Record one measurement (carry it: bus / list / drop / envelope-buffer)."""
        ...

    async def drain(self, corr: str) -> List[Dict[str, Any]]:
        """Return AND CLEAR any buffered records for `corr` so the caller can attach
        them to an outgoing envelope (R6 envelope carriage). Non-carriage tracers
        return [] — they deliver through their own channel."""
        ...

    async def ingest(self, records: List[Dict[str, Any]]) -> None:
        """Incorporate already-projected records that arrived from a remote node
        (R6 merge-on-receive). The harness calls this on `reply.headers["trace"]`
        so remote spans flow into the orchestrator's local tracer for unified
        delivery — Bus republishes, Envelope appends to its own buffer, Null drops.
        Records were projected through contributors at emit time; ingest skips
        re-projection."""
        ...
