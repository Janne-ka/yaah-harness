"""Span — one trace measurement (a stage / model_call / tool_call).

Used by: emit sites (the harness, the Agent, run_tool_loop) build one per
measurement; a Tracer carries it; the capture contributors project it.
Where: the engine tracing core (the OTel-aligned record contract, R2).
Why: a single rich measurement shape so timing/cost/tool detail is captured
uniformly; what actually gets RECORDED is then chosen by the capture
contributors (R2a), so the Span holds everything and the contributors decide
which fields survive.

Targets Python 3.9+.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Span:
    # structural stitch keys (always carried, used to assemble a run's trace)
    id: str                              # this span's id
    corr: str                            # correlation_id — the whole run
    name: str                            # span kind: "stage" | "model_call" | "tool_call"
    parent: Optional[str] = None         # causation_id — the span that caused this one
    # timing skeleton (monotonic from the emit site's clock; durations are exact,
    # absolute wall-time is the sink's concern — see FileTraceSink/LangfuseTraceSink)
    t_start: float = 0.0
    t_end: float = 0.0
    duration_ms: float = 0.0
    # cost (filled only when the `cost` capture is enabled — see R4 on_usage)
    tokens_in: int = 0
    tokens_out: int = 0
    model: Optional[str] = None
    # tool (filled for tool_call spans)
    tool: Optional[str] = None
    # outcome + free attributes (stage/attempt/route/verdict)
    status: Optional[str] = None         # "ok" | "error" | a route/verdict label
    attrs: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def timed(cls, name: str, *, corr: str, t0: float, t1: float,
              parent: Optional[str] = None, **fields: Any) -> "Span":
        """Build a span from a start/end clock reading: mints the id and computes
        duration_ms, so every emit site (harness/agent/tool-loop) shares the same
        boilerplate and only passes its own fields (status, tokens, tool, attrs)."""
        return cls(id=uuid.uuid4().hex, corr=corr, name=name, parent=parent,
                   t_start=t0, t_end=t1, duration_ms=(t1 - t0) * 1000.0, **fields)
