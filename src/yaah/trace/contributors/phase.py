"""PhaseContributor — the default-on minimum capture (progress UX).

Used by: a Tracer's contributor set; enabled by default (`capture: [phase]`).
Where: the engine's bundled contributors (pure projection, no external system —
like the Static/Routing prompt sources that ship in the engine).
Why: the cheapest useful trace — which stage ran, did it pass, how long — so a
zero-config run gets live progress out of the box. No tokens, no tool detail.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from ..span import Span


class PhaseContributor:
    name = "phase"

    def contribute(self, span: Span) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": span.status, "duration_ms": span.duration_ms}
        if "stage" in span.attrs:
            out["stage"] = span.attrs["stage"]
        return out
