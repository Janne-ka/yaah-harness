"""IdempotencyStore — execute-once result cache over a StoreBackend (Phase A).

Used by: OnceNode (yaah.nodes.once_node), which wraps a side-effecting node when
its config says `idempotent: true`. Built by the runtime from root `state:` and
handed to builders via BuildContext.
Where: a typed facade over the StoreBackend substrate, namespace 'idem:'.
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

from .facade import StoreBackedFacade
from .store import StoreBackend


class IdempotencyStore(StoreBackedFacade[StoreBackend]):  # core tier only
    PREFIX = "idem:"

    async def lookup(self, key: str) -> Optional[Dict[str, Any]]:
        """The cached result for `key`, or None if this key hasn't run yet."""
        raw = await self._store.get(self.PREFIX + key)
        return json.loads(raw.decode()) if raw is not None else None

    async def finalize(self, key: str, result: Dict[str, Any]) -> None:
        """Record the first run's result. Uses CAS create-if-absent when the
        backend supports it, so concurrent first-runs commit exactly one winner
        (a losing finalize is a no-op; lookup() reads the winner). Core-tier
        backends fall back to put — sequential-only guarantee, see
        docs/durable-state.md."""
        encoded = json.dumps(result).encode()
        cas = getattr(self._store, "cas", None)
        if cas is not None:
            await cas(self.PREFIX + key, encoded, expected=None)
        else:
            await self._store.put(self.PREFIX + key, encoded)
