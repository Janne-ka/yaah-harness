"""LiveContributor — project `llm_progress` pulse spans into trace records.

Used by: a Tracer's contributor set; enabled by `capture: [..., "live"]`. The flag
does double duty: it gates the Agent BUILDING the StreamEvent bridge at all (the
emit-site convention — a disabled capture skips gathering raw material), and it
projects the pulses here so the configured sinks (console / file / NATS) render
them. Off (the default) → no bridge, no spans, zero overhead.
Where: the engine's bundled contributors (pure projection, no external system).
Why: the monitoring answer to "what is the model doing NOW — alive or hung?".
Stage spans are post-hoc; these are live pulses: turn start, a throttled
chars-so-far heartbeat, each assembled tool call, done/error. Sizes and names
only — never model text (content capture is a separate, security-reviewed
concern; see agents/live_events.py).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from ..contributor import TraceContributor
from ..span import Span


class LiveContributor(TraceContributor):
    name = "live"

    def contribute(self, span: Span) -> Dict[str, Any]:
        if span.name != "llm_progress":
            return {}  # contributes nothing to stage/model_call/tool_call spans
        out: Dict[str, Any] = {}
        for k in ("event", "stage", "chars", "tool", "stop_reason", "message"):
            if k in span.attrs:
                out[k] = span.attrs[k]
        return out
