"""DataSink — the interface for WRITING data by key (the 'post' seam).

Used by: PostNode (the 'post' node type) and the runtime (builds one from the
root config's `data_sinks`). Implemented by: File / Routing sinks (a memory/
substrate sink lands here later — memory-write is a `post`).
Where: the mirror of DataSource — where a stage's output is persisted/emitted
(a file, a substrate store, an external system) instead of riding the chain.
Why: writes are transforms too. The harness knows only `Node`; "memory" is not a
type, it's a `get` (read) plus a `post` (write). Same shape as DataSource.fetch
so the read/write pair feels symmetric.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataSink(Protocol):
    async def store(self, key: str, value: Any, **opts: Any) -> str:
        """Persist `value` under `key`; return a handle/locator for it."""
        ...
