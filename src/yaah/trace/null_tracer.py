"""NullTracer — the explicit "tracing off" carriage.

Used by: the build/runtime when `trace.mode: none`; also the default a harness/
agent falls back to when no tracer is injected, so emit sites can call
self._tracer.emit(...) unconditionally.
Where: the engine tracing core (a zero-cost default, no external system).
Why: make "off" a real object (not None checks scattered at every emit site) —
emit drops the span, captures is empty so emit sites skip gathering any raw
material.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional

from .span import Span
from .tracer import Tracer


class NullTracer(Tracer):
    is_carriage = False
    captures: FrozenSet[str] = frozenset()

    async def emit(self, span: Span) -> None:
        return None

    async def drain(self, corr: str) -> List[Dict[str, Any]]:
        return []  # not a carriage tracer — nothing to hand over

    async def ingest(self, records: List[Dict[str, Any]]) -> None:
        return None  # tracing off — drop remote records too (R6)

    def last_model_call_span(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        return None  # ADR-0003: tracing off — no span to surface to attachers
