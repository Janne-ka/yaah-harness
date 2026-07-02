"""RecordingTracer — an in-process tracer that keeps every record in a list.

Used by: tests (assert what was traced without a bus/sink) and any in-proc
consumer that wants the spans directly.
Where: the engine tracing core (a no-external-system default).
Why: the DI-testing seam for the whole tracing feature — inject this instead of
a BusTracer and inspect `.records`; mirrors how FakeBackend/RecordingScripted
stand in for real backends.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from .contributor import TraceContributor
from .tracer import Tracer
from .record import project
from .span import Span


class RecordingTracer(Tracer):
    is_carriage = False  # records stay in `.records` for inspection; not a carriage

    def __init__(self, contributors: Sequence[TraceContributor] = ()) -> None:
        self._contributors = list(contributors)
        self.captures: FrozenSet[str] = frozenset(c.name for c in self._contributors)
        self.records: List[Dict[str, Any]] = []
        self.spans: List[Span] = []

    async def emit(self, span: Span) -> None:
        self.spans.append(span)
        self.records.append(project(span, self._contributors))

    async def drain(self, corr: str) -> List[Dict[str, Any]]:
        # Tracer-port `drain` (R6): return AND CLEAR the buffered records for one
        # corr — same carriage semantic as EnvelopeTracer. Inspection from tests
        # uses `tracer.records` (the unconditional list) instead.
        out = [r for r in self.records if r.get("corr") == corr]
        self.records = [r for r in self.records if r.get("corr") != corr]
        self.spans = [s for s in self.spans if s.corr != corr]
        return out

    async def ingest(self, records: List[Dict[str, Any]]) -> None:
        """Tracer-port `ingest` (R6): append remote records to `records` so test
        inspection sees them. No re-projection (records arrive already projected)."""
        self.records.extend(records or ())

    def last_model_call_span(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        """ADR-0003: scan records in reverse for the most recent model_call span
        under this corr. records hold every emit (and ingest) since construction,
        so a nested-agent's span and the outer agent's span are both present —
        the corr filter is what disambiguates."""
        for record in reversed(self.records):
            if (record.get("corr") == correlation_id
                    and record.get("name") == "model_call"):
                return record
        return None
