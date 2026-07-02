"""yaah.store — the durable-state PORT (StoreBackend, in capability tiers) + the in-memory
default (MemoryBackend) and the typed facades over it: IdempotencyStore (execute-once)
and EnvelopeStore (the gate-parking utility — save/load an Envelope by key; used by
gates like human_gate / fanin / future proxygate / hold-until, memory now and
file/db later by swapping the backend). External-system stores (file now; nats_kv /
blob / sqlite deferred) are swap-in adapters in yaah.adapters.stores. BatonStore lives
in yaah.harness (the resume-cursor persistence). See docs/durable-state.md.
"""
from .envelope_store import EnvelopeStore
from .facade import StoreBackedFacade
from .idempotency import IdempotencyStore
from .memory_backend import MemoryBackend
from .store import CompareAndSet, Scannable, ScannableStore, StoreBackend

__all__ = ["StoreBackend", "Scannable", "CompareAndSet", "ScannableStore",
           "StoreBackedFacade", "MemoryBackend", "IdempotencyStore", "EnvelopeStore"]
