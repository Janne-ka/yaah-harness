"""BatonStore — durable run-state (the resume cursor), over a StoreBackend.

Used by: Harness (replaces the in-memory `_batons` dict). The default is
BatonStore(MemoryBackend()) = today's behavior; a durable StoreBackend extender makes a
parked human gate survive a restart and lets resume() run in ANOTHER process.
Where: the harness's persistence seam — a typed facade over the yaah.store StoreBackend
substrate, namespace 'baton:'.
Why: keep the harness ignorant of WHERE state lives. It calls save/load/delete/
sweep/list; this serializes the Baton to bytes and back. Needs the +SCAN tier
(sweep_expired, list_suspended iterate the namespace).

What the store holds (docs/durable-state.md §5):
  Level 1 — the harness saves a baton when it SUSPENDS at a human gate and deletes
  it on any terminal outcome. These are the `suspended` records (list_suspended).
  Level 2 — the harness ALSO writes a `running` checkpoint after each completed
  stage (Harness._checkpoint), carrying the cursor + the envelope that feeds the
  next stage, so a run killed mid-flight is re-drivable (list_running). A park
  overwrites the running checkpoint with the suspended record; a terminal outcome
  deletes it.
So the store holds the parked runs PLUS the in-flight/crashed ones. Both are
bounded by the same TTL sweep (Baton.is_expired covers each kind's own clock).

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import List, Optional, Tuple

from ..store import ScannableBackend, StoreBackedFacade
from .baton import Baton


class BatonStore(StoreBackedFacade[ScannableBackend]):  # +SCAN: sweep/list need scan
    PREFIX = "baton:"
    REQUIRES = ScannableBackend  # checked at construction (fail fast)

    async def save(self, baton: Baton) -> None:
        await self._store.put(self.PREFIX + baton.id, json.dumps(baton.to_dict()).encode())

    async def load(self, baton_id: str) -> Optional[Baton]:
        raw = await self._store.get(self.PREFIX + baton_id)
        return Baton.from_dict(json.loads(raw.decode())) if raw is not None else None

    async def load_rev(self, baton_id: str) -> "Tuple[Optional[Baton], Optional[int]]":
        """`load` plus the backend REVISION the record was read at — the first half
        of the read-decide-claim sequence in `Harness.resume_running`. On a backend
        without the +CAS tier the revision is None, which `claim` treats as "no
        conflict detection available" (see there)."""
        get_rev = getattr(self._store, "get_rev", None)
        if get_rev is None:
            return await self.load(baton_id), None
        raw, rev = await get_rev(self.PREFIX + baton_id)
        return (Baton.from_dict(json.loads(raw.decode())) if raw is not None else None), rev

    async def claim(self, baton: Baton, expected_rev: Optional[int]) -> bool:
        """CLAIM the record: write it back only if nobody else wrote it since
        `expected_rev` (docs/durable-state.md §10, "single-owner baton"). Returns
        False when the claim was LOST — another process got there first, and the
        caller must refuse rather than double-drive the run.

        The +CAS tier is OPTIONAL, probed by `getattr` — the same stance the
        facades take toward `close()`: a backend that has it gets the real
        guarantee, one that does not still works. On a non-CAS backend this falls
        back to a plain `put` and returns True, so the claim is ADVISORY there;
        the refusal message says so, because "your store cannot prove this" is an
        operator fact, not an engine detail to hide. FileBackend's CAS is
        flock-serialized, which is itself advisory over NFS."""
        cas = getattr(self._store, "cas", None)
        if cas is None:
            await self.save(baton)
            return True
        rev = await cas(self.PREFIX + baton.id,
                        json.dumps(baton.to_dict()).encode(), expected=expected_rev)
        return rev is not None

    def has_cas(self) -> bool:
        """Whether this store can actually PROVE a claim (the +CAS tier). Read by
        the refusal/warning messages so an operator on a memory/blob backend is
        told the single-owner check was advisory."""
        return callable(getattr(self._store, "cas", None))

    async def delete(self, baton_id: str) -> None:
        await self._store.delete(self.PREFIX + baton_id)

    async def sweep_expired(self, now: float) -> List[str]:
        """Delete every baton past its own ttl as of `now`; return their ids.

        Covers BOTH stored kinds (Baton.is_expired decides): a `suspended` gate
        nobody answered, measured from `parked_at`, and a Level 2 `running`
        checkpoint nobody recovered, measured from `checkpointed_at`. So a killed
        mid-run leaves nothing behind forever — but note the second clock also
        bounds a LIVE stage: a stage in flight longer than the ttl has its recovery
        record swept (see Baton.is_expired's dual-meaning note).

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

    async def list_running(self) -> List[Baton]:
        """Every RUNNING checkpoint (Level 2) — a baton persisted mid-run with a
        `cursor_input`. On a shared durable store these are either LIVE runs (a
        process still driving them) or CRASHED ones a `resume_running` can recover.
        Which one is answered by the LIVENESS LEASE each record carries
        (`owner`/`leased_at` → `LeaseState`, docs/durable-state.md §10), so the
        inspection surface labels them `live` / `stale` / `foreign` / `none` rather
        than making the operator assert death; `yaah list` prints the `resume-run`
        hint only when the lease is not live. Since the first-stage checkpoint this
        includes runs that have not yet completed a single stage. Empty on the
        default memory backend after the process that ran them exits (the dict died
        with it)."""
        out: List[Baton] = []
        async for _key, raw in self._store.scan(self.PREFIX):
            baton = Baton.from_dict(json.loads(raw.decode()))
            if baton.status == "running" and baton.cursor_input is not None:
                out.append(baton)
        return out
