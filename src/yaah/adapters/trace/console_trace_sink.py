"""ConsoleTraceSink — render the phase capture as live progress on stderr.

Used by: the runtime ships this by default so the default-on `[phase]` tracing is
VISIBLE out of the box (the basic UX win), not merely recorded. `trace.sink:
{type: console}` selects it explicitly.
Where: a swap-in TraceSink adapter (binds to stderr).
Why: a zero-config run should SHOW which stage it's in and how long each took —
the cheapest, highest-value observability. Renders `stage` spans (the phase
signal) and — when the `live` capture is on — `llm_progress` pulses, the
mid-call "alive or hung?" answer. Richer records go to the file/Langfuse sinks.

Targets Python 3.9+.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Optional, TextIO

from ...core import Envelope
from ...trace import TraceSink


def _live_line(r: Dict[str, Any]) -> str:
    """One compact line per llm_progress pulse. No `live` capture → no such
    records arrive → zero console noise by default."""
    stage = r.get("stage", "?")
    event = r.get("event", "?")
    if event == "progress":
        return "[trace] live {}: generating ({} chars)".format(stage, r.get("chars", "?"))
    if event == "tool_call":
        return "[trace] live {}: tool {}".format(stage, r.get("tool") or "?")
    if event == "tool_result":
        return "[trace] live {}: tool {} returned".format(stage, r.get("tool") or "?")
    if event == "done":
        return "[trace] live {}: turn done ({})".format(stage, r.get("stop_reason", "?"))
    if event == "error":
        return "[trace] live {}: ERROR {}".format(stage, r.get("message", ""))
    return "[trace] live {}: turn started".format(stage)   # "start" (and any future pulse)


class ConsoleTraceSink(TraceSink):
    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    async def handle(self, env: Envelope) -> None:
        r = env.payload
        name = r.get("name")
        if name == "llm_progress":
            print(_live_line(r), file=self._stream, flush=True)
            return
        if name != "stage":
            return  # progress = stage completions; other spans go to richer sinks
        dur = r.get("duration_ms", 0.0)
        print("[trace] stage {} {} ({:.0f}ms)".format(
            r.get("stage", "?"), r.get("status", "?"), dur),
            file=self._stream, flush=True)
