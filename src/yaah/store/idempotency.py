"""IdempotencyStore — execute-once result cache over a Store (Phase A).

Used by: OnceNode (yaah.nodes.once_node), which wraps a side-effecting node when
its config says `idempotent: true`. Built by the runtime from root `state:` and
handed to builders via BuildContext.
Where: a typed facade over the Store substrate, namespace 'idem:'.
Why: a retried/replayed side-effecting node (e.g. a git commit, an external POST)
must run ONCE (early_review #14). This stores the first run's result keyed by the
envelope's idempotency_key; a later attempt with the same key returns the cached
result instead of re-running.

Phase A (here): sequential dedup — lookup, then finalize. Enough for the retry
loop within one run (attempts are sequential). Phase B (concurrent replicas, via
the +CAS tier's claim) is deferred (see docs/durable-state.md §6).

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


class IdempotencyStore:
    PREFIX = "idem:"

    def __init__(self, store: Any) -> None:  # store: yaah.store.Store (core tier)
        self._store = store

    async def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        """The cached result for `key`, or None if this key hasn't run yet."""
        raw = await self._store.get(self.PREFIX + key)
        return json.loads(raw.decode()) if raw is not None else None

    async def finalize(self, key: str, result: Dict[str, Any]) -> None:
        """Record the result of the first (and only) run for `key`. Uses CAS
        with expected=None (create-if-absent) when the backing store supports
        it (assessment cluster 2 B4): two concurrent first-runs both called
        `put` previously, so both wrote and both executed the side effect.
        With CAS, only the first writer wins; the second `finalize` is a
        no-op (the cached result is whatever the first writer recorded —
        callers must lookup() to read it). Stores without `cas` fall back to
        `put` (Phase A sequential-only guarantee per docs/durable-state.md)."""
        encoded = json.dumps(result).encode()
        cas = getattr(self._store, "cas", None)
        if cas is not None:
            await cas(self.PREFIX + key, encoded, expected=None)
        else:
            await self._store.put(self.PREFIX + key, encoded)
