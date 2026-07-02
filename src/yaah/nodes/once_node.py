"""OnceNode — wrap a side-effecting node so it runs ONCE per idempotency key.

Used by: yaah.build, which wraps any node whose config says `idempotent: true`
(given an IdempotencyStore in the BuildContext). Transparent: same Node interface.
Where: around a `post` / `transform` / `shell` / `git` node that has an external
effect which must not repeat on a retry or a (Level 2) replay.
Why: the harness retries and may re-run a stage; without a guard a retried
`git commit` / external POST fires twice (early_review #14). This checks the
IdempotencyStore for the envelope's idempotency_key: a cached result short-circuits
the inner node; otherwise it runs the inner node once and records the result.

No key (no idempotency_key on the envelope or NodeConfig) -> not guarded, runs as
normal. Phase A (sequential) only; concurrent-replica claiming is deferred
(docs/durable-state.md §6).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any

from ..core import Node, Envelope, Kind, NodeConfig
from ..store import IdempotencyStore


def _is_committed_success(env: Envelope) -> bool:
    """A returned envelope represents a successful committed side effect when
    its kind isn't ERROR and (if it carries a verdict) its status isn't fail.
    Used by OnceNode (assessment cluster 2 B2): a FAILURE return must NOT be
    cached — the side effect didn't actually commit, and freezing the failure
    forever turns a transient error into a permanent one. A raised exception
    propagates without reaching finalize; the asymmetry was: raise -> retries,
    return-failure -> cached forever. Symmetric now: neither is cached."""
    if env.kind == Kind.ERROR:
        return False
    if env.kind == Kind.VERDICT and env.payload.get("status") == "fail":
        return False
    return True


class OnceNode(Node):
    def __init__(self, inner: Node, store: IdempotencyStore) -> None:
        self._inner = inner
        self._store = store

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        key = config.idempotency_key or input.headers.get("idempotency_key")
        if not key:  # unkeyed -> not idempotent, run normally
            return await self._inner.invoke(input, config)
        hit = await self._store.lookup(key)
        if hit is not None:  # already ran for this key -> return the cached output
            return Envelope.from_dict(hit)
        out = await self._inner.invoke(input, config)
        # B2: only cache COMMITTED SUCCESS — a returned failure means the side
        # effect didn't commit (or partially did and bailed). Caching it would
        # prevent any retry from succeeding. A raised exception also escapes
        # without caching; both failure shapes now behave the same way.
        if _is_committed_success(out):
            await self._store.finalize(key, out.to_dict())
        return out
