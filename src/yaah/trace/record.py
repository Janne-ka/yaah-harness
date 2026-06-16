"""project — turn a Span + the enabled contributors into the carried record.

Used by: RecordingTracer and BusTracer (the projecting tracers) build each
record this way; NullTracer skips it.
Where: the engine tracing core, shared so the two tracers can't drift.
Why: the carried record is the STRUCTURAL stitch keys (always present, so any
sink can assemble/order a run's spans regardless of capture config) PLUS the
union of the enabled contributors' fields (R2a). One place defines that shape.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

from .contributor import TraceContributor
from .span import Span


def project(span: Span, contributors: Sequence[TraceContributor]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "id": span.id,
        "parent": span.parent,
        "corr": span.corr,
        "name": span.name,
        "t_start": span.t_start,
        "t_end": span.t_end,
    }
    for c in contributors:
        record.update(c.contribute(span))
    return record
