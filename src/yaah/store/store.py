"""Store — the durable key->bytes substrate, in capability tiers.

Used by: the typed facades (BatonStore, IdempotencyStore, and a KV-backed
DataSource/Sink later) — they layer meaning on top of these raw bytes ops.
Implemented by: MemoryStore now; file / nats_kv / sqlite / blob / ... are
deferred drop-in EXTENDERS chosen per-deployment (see docs/durable-state.md).
Where: the bundled-stdlib substrate behind durable run state, execute-once, and
working memory — NOT the kernel (the kernel is still only Node/Envelope/Comms).
Why: define ONE contract, in tiers, so a backend implements only what it can —
a blob store has no compare-and-set, a KV store does — and each facade requires
just the tier it needs (working memory -> core; baton store -> +scan;
cross-process single-owner resume -> +cas), validated up front.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import AsyncIterator, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """CORE tier — every extender provides this (enough for working-memory get/post)."""
    async def get(self, key: str) -> Optional[bytes]: ...
    async def put(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None: ...
    async def delete(self, key: str) -> None: ...


@runtime_checkable
class Scannable(Protocol):
    """+SCAN tier — list by key prefix; needed for the baton sweep and the mailbox view."""
    def scan(self, prefix: str) -> AsyncIterator[Tuple[str, bytes]]: ...


@runtime_checkable
class CompareAndSet(Protocol):
    """+CAS tier — atomic write-if-unchanged; needed only for distributed single-owner
    resume and concurrent-replica idempotency. `expected` is the revision the caller
    last saw (None = create-if-absent); returns the new revision, or None on conflict."""
    async def get_rev(self, key: str) -> Tuple[Optional[bytes], Optional[int]]: ...
    async def cas(self, key: str, value: bytes, *, expected: Optional[int],
                  ttl: Optional[float] = None) -> Optional[int]: ...
