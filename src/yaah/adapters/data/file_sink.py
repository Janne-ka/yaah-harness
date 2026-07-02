"""FileSink — write a value to a file (the simplest 'post' target).

Used by: the 'post' node (PostNode) and the runtime's `file` data sink.
Where: local files (a report artifact, a scratchpad, a result dropped for
another process).
Why: the write counterpart to FileDataSource. store('out/report.html', text)
writes the file and returns its path as the handle. Non-str values are
JSON-encoded so structured results round-trip.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ...safepath import safe_join
from ...data import DataSink


class FileSink(DataSink):
    def __init__(self, base_dir: str = "") -> None:
        self._base = base_dir

    async def store(self, key: str, value: Any, *, cwd: Optional[str] = None, **_: Any) -> str:
        base = cwd or self._base
        # safe_join: of the four file adapters this is the MOST DANGEROUS to leave
        # untainted — store() both writes AND auto-creates parents, so a relative
        # `key` like `../../etc/something` previously made a directory and wrote
        # to it (assessment cluster 5 security #3).
        path = safe_join(base, key)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path  # the handle is the path written
