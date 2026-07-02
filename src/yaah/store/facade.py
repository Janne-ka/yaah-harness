"""StoreBackedFacade — shared base for the typed access-layer facades that wrap a
StoreBackend (EnvelopeStore, IdempotencyStore, BatonStore): a backend handle, a
key-namespace PREFIX, and a construction-time tier check. It deliberately declares
no common save/load verbs — the facades' APIs genuinely differ (explicit-key vs
self-keying vs lookup/finalize), so a shared signature would be a false abstraction.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Generic, TypeVar

from .store import StoreBackend

B = TypeVar("B", bound=StoreBackend)  # the backend tier THIS facade needs


class StoreBackedFacade(Generic[B]):
    PREFIX = ""  # namespace this facade's keys carry into the shared backend
    # The tier the facade requires, VALIDATED at construction (fail fast, not
    # mid-run): a facade whose list/sweep needs scan() sets REQUIRES = ScannableBackend,
    # so wiring it to a core-only backend raises here instead of AttributeError
    # deep in a baton sweep. isinstance works because the tiers are
    # @runtime_checkable Protocols.
    REQUIRES: type = StoreBackend

    def __init__(self, backend: B) -> None:
        if not isinstance(backend, self.REQUIRES):
            raise TypeError(
                "{} needs a {} backend; {} does not provide that tier "
                "(see docs/durable-state.md)".format(
                    type(self).__name__, self.REQUIRES.__name__,
                    type(backend).__name__))
        self._store: B = backend
