"""ConsoleTraceSink — render the phase capture as live progress on stderr.

Used by: the runtime ships this by default so the default-on `[phase]` tracing is
VISIBLE out of the box (the basic UX win), not merely recorded. `trace.sink:
{type: console}` selects it explicitly.
Where: a swap-in TraceSink adapter (binds to stderr).
Why: a zero-config run should SHOW which stage it's in and how long each took —
the cheapest, highest-value observability. Only renders `stage` spans (the phase
signal); richer records go to the file/Langfuse sinks.

Targets Python 3.9+.
"""
from __future__ import annotations

import sys
from typing import Any, Optional, TextIO

from ...core import Envelope
from ...trace import TraceSink


class ConsoleTraceSink(TraceSink):
    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    async def handle(self, env: Envelope) -> None:
        r = env.payload
        if r.get("name") != "stage":
            return  # progress = stage completions; other spans go to richer sinks
        dur = r.get("duration_ms", 0.0)
        print("[trace] stage {} {} ({:.0f}ms)".format(
            r.get("stage", "?"), r.get("status", "?"), dur),
            file=self._stream, flush=True)
