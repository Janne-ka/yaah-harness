"""StoreBackedFacade — the shared base for the typed access-layer facades that
wrap a StoreBackend substrate (EnvelopeStore, IdempotencyStore, BatonStore).

Marker-only by design. It holds ONLY the wrap-pattern the three facades have in
common — a backend handle plus a namespace `PREFIX` — and deliberately declares
NO save/load/delete signature. Their verbs genuinely differ (EnvelopeStore is
explicit-key `save(key, env)`, BatonStore is self-keying `save(baton)`,
IdempotencyStore is a `lookup`/`finalize` cache), so forcing a shared signature
would be an LSP violation, not an abstraction. This base makes the "these are
facades over a StoreBackend, not backends themselves" relationship VISIBLE (in the
class header) without over-promising a common API.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from .store import StoreBackend

B = TypeVar("B", bound=StoreBackend)  # the backend tier THIS facade needs


class StoreBackedFacade(Generic[B]):
    PREFIX = ""  # namespace each facade's keys carry into the shared backend

    def __init__(self, backend: B) -> None:
        # `_store` holds the StoreBackend substrate (typed to the tier the facade
        # declares via StoreBackedFacade[...]); each facade layers its own typed
        # verbs (save/load/lookup/…) over it under `PREFIX`.
        self._store: B = backend
