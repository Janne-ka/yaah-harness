"""FileTraceSink — persist trace records as append-only JSONL.

Used by: the runtime when `trace.sink: {type: file, ...}`; subscribed to the
`trace` topic so every span a BusTracer publishes is written.
Where: a swap-in TraceSink adapter (binds to the filesystem).
Why: the simplest durable sink — one JSON record per line, appended as it
happens. No per-run buffering or lifecycle to get wrong (crash-safe: a record on
disk stays), and the `corr` in each line lets the aggregator (R8) group/sort by
(corr, t_start) at read time. The new `agent-bus.jsonl`.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ...core import Envelope


class FileTraceSink:
    def __init__(self, path: str) -> None:
        self._path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def handle(self, env: Envelope) -> None:
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(env.payload) + "\n")
