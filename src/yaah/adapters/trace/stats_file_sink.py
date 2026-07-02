"""StatsFileSink — keep a live statistics rollup in a separate file.

Used by: the runtime when `trace.sink: {type: stats_file, path: ..., price_map: ...}`.
Where: a swap-in TraceSink adapter (binds to the filesystem); reuses the pure
`trace.aggregate` reducer so the rollup logic lives in ONE place.
Why: the counterpart to ProgressFileSink — progress is the live tail, this is the
NUMBERS (per-run cost/tokens/duration, per-stage latency percentiles, model mix,
retry signal). It accumulates the spans it sees and REWRITES the file with the
current aggregate on each one, so the stats file always reflects the run so far
(cat it any time; no end-of-run step). Cost is opt-in via the config `price_map`
(token->$), exactly as the report's metrics block and the aggregate CLI.

Separate from the JSONL FileTraceSink: that's the append-only raw record; this is
the reduced snapshot. Use both — raw for replay/debug, this for "how much/how long".

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ...core import Envelope
from ...trace import TraceSink
from ...trace.aggregate import aggregate


class StatsFileSink(TraceSink):
    def __init__(self, path: str, *, price_map: Optional[Dict[str, Any]] = None) -> None:
        self._path = path
        self._price_map = price_map
        self._records: List[Dict[str, Any]] = []  # spans seen so far (in-memory, this run)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def handle(self, env: Envelope) -> None:
        # accumulate then rewrite the snapshot — the file always holds the current
        # rollup, so it's readable mid-run without an end-of-run finalize step.
        self._records.append(dict(env.payload))
        snapshot = aggregate(self._records, price_map=self._price_map)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, self._path)  # atomic: a concurrent reader never sees a half-write
