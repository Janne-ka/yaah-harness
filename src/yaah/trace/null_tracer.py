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

from typing import Any, Dict, FrozenSet, List

from .span import Span


class NullTracer:
    is_carriage = False
    captures: FrozenSet[str] = frozenset()

    async def emit(self, span: Span) -> None:
        return None

    async def drain(self, corr: str) -> List[Dict[str, Any]]:
        return []  # not a carriage tracer — nothing to hand over

    async def ingest(self, records: List[Dict[str, Any]]) -> None:
        return None  # tracing off — drop remote records too (R6)
