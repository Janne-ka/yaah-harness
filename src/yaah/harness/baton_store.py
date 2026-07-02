"""BatonStore — durable run-state (the resume cursor), over a StoreBackend.

Used by: Harness (replaces the in-memory `_batons` dict). The default is
BatonStore(MemoryBackend()) = today's behavior; a durable StoreBackend extender makes a
parked human gate survive a restart and lets resume() run in ANOTHER process.
Where: the harness's persistence seam — a typed facade over the yaah.store StoreBackend
substrate, namespace 'baton:'.
Why: keep the harness ignorant of WHERE state lives. It calls save/load/delete/
sweep/list; this serializes the Baton to bytes and back. Needs the +SCAN tier
(sweep_expired, list_suspended iterate the namespace).

Level 1 (now): the harness saves a baton only when it SUSPENDS and deletes it on
any terminal outcome — so the store holds exactly the parked runs, the same bound
the dict had. (Level 2 per-stage checkpointing is later; see docs/durable-state.md.)

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import List, Optional

from ..store import ScannableStore, StoreBackedFacade
from .baton import Baton


class BatonStore(StoreBackedFacade[ScannableStore]):  # +SCAN: sweep/list need scan
    PREFIX = "baton:"

    async def save(self, baton: Baton) -> None:
        await self._store.put(self.PREFIX + baton.id, json.dumps(baton.to_dict()).encode())

    async def load(self, baton_id: str) -> Optional[Baton]:
        raw = await self._store.get(self.PREFIX + baton_id)
        return Baton.from_dict(json.loads(raw.decode())) if raw is not None else None

    async def delete(self, baton_id: str) -> None:
        await self._store.delete(self.PREFIX + baton_id)

    async def sweep_expired(self, now: float) -> List[str]:
        """Delete every parked baton past its own ttl as of `now`; return their ids.

        Assessment cluster 2 LOW: deleting WHILE iterating the scan is a
        narrow race (the underlying store may invalidate iterator state). We
        snapshot first, delete second — same async cost, no in-flight
        mutation."""
        candidates: List[tuple] = []
        async for key, raw in self._store.scan(self.PREFIX):
            try:
                baton = Baton.from_dict(json.loads(raw.decode()))
            except (json.JSONDecodeError, ValueError, KeyError):
                continue                                          # corrupt entry: skip, sweep can't fix it
            if baton.is_expired(now):
                candidates.append((key, baton.id))
        dead: List[str] = []
        for key, baton_id in candidates:
            await self._store.delete(key)
            dead.append(baton_id)
        return dead

    async def list_suspended(self) -> List[Baton]:
        """Every currently-suspended baton — the mailbox view (open human gates)."""
        out: List[Baton] = []
        async for _key, raw in self._store.scan(self.PREFIX):
            baton = Baton.from_dict(json.loads(raw.decode()))
            if baton.status == "suspended":
                out.append(baton)
        return out
