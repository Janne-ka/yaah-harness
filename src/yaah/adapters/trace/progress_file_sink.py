"""ProgressFileSink — append live stage progress to a file you can `tail -f`.

Used by: the runtime when `trace.sink: {type: progress_file, path: ...}`.
Where: a swap-in TraceSink adapter (binds to the filesystem). The file twin of
ConsoleTraceSink (which writes the same lines to stderr).
Why: the cheapest "where is the run now?" UX that survives a detached/background
run — one human-readable line per stage completion, appended as it happens, so
`tail -f progress.log` shows the pipeline advancing. Deliberately SEPARATE from
the statistics rollup (StatsFileSink) and the machine JSONL (FileTraceSink): a
progress file you watch, a stats file you read, a JSONL you aggregate.

Renders only `stage` spans (the phase signal); richer records go to the other
sinks. Each line: `<HH:MM:SS> <stage> <status> (<dur>ms)`.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from ...core import Envelope


class ProgressFileSink:
    def __init__(self, path: str, *, clock: Optional[Callable[[], float]] = None) -> None:
        self._path = path
        self._clock = clock or time.time  # injectable for deterministic tests
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def handle(self, env: Envelope) -> None:
        r = env.payload
        if r.get("name") != "stage":
            return  # progress = stage completions; other spans go to richer sinks
        ts = time.strftime("%H:%M:%S", time.localtime(self._clock()))
        line = "{} {:<18} {} ({:.0f}ms)\n".format(
            ts, r.get("stage", "?"), r.get("status", "?"), r.get("duration_ms", 0.0))
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line)
