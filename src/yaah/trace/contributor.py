"""TraceContributor — a capture module that projects a Span into record fields.

Used by: a Tracer (Recording/Bus) runs every enabled contributor over each
emitted Span and merges their fields into the carried record.
Where: the engine tracing core defines the PORT (R2a); the bundled modules
(phase / cost / tools) live beside it in yaah.trace.contributors — they're pure
projection (no external system), so they stay in the engine like the Static/
Routing prompt sources; only I/O sinks are adapters. (`reasoning`, R8a, is parked.)
Why: WHAT a trace carries is a COMPOSABLE SET of orthogonal modules, not a
linear verbosity level — `[phase]` (the default-on minimum), `[phase, cost,
tools]`, or `[phase, reasoning]` (compliance, deliberately without cost/tools)
are all valid. Each module owns one fragment of the record and any logic inside.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Dict, Protocol, runtime_checkable

from .span import Span


@runtime_checkable
class TraceContributor(Protocol):
    name: str  # the capture name this module provides (e.g. "phase", "cost")

    @abstractmethod
    def contribute(self, span: Span) -> Dict[str, Any]:
        """Return the fields this capture adds to the record for `span`
        (possibly empty — e.g. the tools capture contributes nothing to a
        stage span)."""
        ...
