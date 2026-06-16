"""MemoryStore — the in-process Store extender (the default; = today's behavior).

Used by: the runtime when no durable `state:` backend is configured, and the test
suite. Backs BatonStore / IdempotencyStore over a plain dict.
Where: the default substrate; single process, lost on exit.
Why: a zero-dependency Store that implements all three tiers (core + scan + cas)
so the facades and their tests run with nothing installed. A durable extender
(file / nats_kv / ...) is dropped in per-deployment with the same interface.

Note: `ttl` is accepted but NOT auto-expired here — higher layers sweep on their
own clock (e.g. BatonStore.sweep_expired via Baton.is_expired); a backend's native
TTL is only a backstop. Single-process, so no locking is needed.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import AsyncIterator, Dict, Optional, Tuple


class MemoryStore:
    def __init__(self) -> None:
        self._data: Dict[str, bytes] = {}
        self._rev: Dict[str, int] = {}  # per-key revision, for compare-and-set

    async def get(self, key: str) -> Optional[bytes]:
        return self._data.get(key)

    async def put(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        self._data[key] = value
        self._rev[key] = self._rev.get(key, 0) + 1

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._rev.pop(key, None)

    async def scan(self, prefix: str) -> AsyncIterator[Tuple[str, bytes]]:
        # snapshot first: callers (sweep) delete while iterating
        for key, value in list(self._data.items()):
            if key.startswith(prefix):
                yield key, value

    async def get_rev(self, key: str) -> Tuple[Optional[bytes], Optional[int]]:
        return self._data.get(key), self._rev.get(key)

    async def cas(self, key: str, value: bytes, *, expected: Optional[int],
                  ttl: Optional[float] = None) -> Optional[int]:
        current = self._rev.get(key)  # None when the key is absent
        if current != expected:       # expected None + absent -> match (create-if-absent)
            return None
        self._data[key] = value
        self._rev[key] = (current or 0) + 1
        return self._rev[key]
