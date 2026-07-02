"""EnvelopeStore — park and reload Envelopes over a StoreBackend (the gate-parking utility).

Used by: GATES — any control point that holds an envelope until it can route or
release it: the human_gate (park until a decision), fanin (park each branch until the
join policy is met), and future gates (a proxygate routing by load, a hold-until gate).
They all need the same thing — save an envelope under a key now, load it back later —
so this is the ONE save/load utility instead of each gate reinventing parking.
Where: a typed facade over the StoreBackend substrate, namespace 'env:'. The BACKEND is the
swappable part — MemoryBackend (default, in-process), FileBackend, or a db extender — so a
gate parks in memory now and durably later WITHOUT the gate changing.
Why: an Envelope is the unit a gate holds; one utility, pluggable backend. Peer of
BatonStore (which already parks the pending envelope for a human gate) and
IdempotencyStore.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..core import Envelope
from .facade import StoreBackedFacade
from .store import ScannableBackend


class EnvelopeStore(StoreBackedFacade[ScannableBackend]):  # +SCAN: `list` needs scan
    PREFIX = "env:"
    REQUIRES = ScannableBackend  # checked at construction (fail fast)

    async def save(self, key: str, envelope: Envelope, *, ttl: Optional[float] = None) -> None:
        """Park `envelope` under `key` (overwrites). `ttl` (where the backend honors it)
        lets an abandoned parked envelope expire."""
        await self._store.put(self.PREFIX + key, envelope.to_json().encode(), ttl=ttl)

    async def load(self, key: str) -> Optional[Envelope]:
        """Reload the parked envelope for `key`, or None if nothing is parked there."""
        raw = await self._store.get(self.PREFIX + key)
        return Envelope.from_json(raw.decode()) if raw is not None else None

    async def delete(self, key: str) -> None:
        """Release (forget) the parked envelope for `key`. No-op if absent."""
        await self._store.delete(self.PREFIX + key)

    async def list(self, group: str = "") -> List[Tuple[str, Envelope]]:
        """All parked (key, envelope) pairs under an optional `group` key-prefix — the
        inspection / mailbox view. Requires the StoreBackend's +SCAN tier. Keys are returned
        without the internal namespace prefix."""
        out: List[Tuple[str, Envelope]] = []
        async for full_key, raw in self._store.scan(self.PREFIX + group):
            out.append((full_key[len(self.PREFIX):], Envelope.from_json(raw.decode())))
        return out

    async def flush(self, group: str = "") -> int:
        """Release ALL parked envelopes under `group` (default everything) and return
        the count. The parked-set side of a flush: error recovery, or cleaning up a
        finished gate's arrivals. Requires the StoreBackend's +SCAN tier."""
        keys = [self.PREFIX + k for k, _ in await self.list(group)]
        for full_key in keys:
            await self._store.delete(full_key)
        return len(keys)
