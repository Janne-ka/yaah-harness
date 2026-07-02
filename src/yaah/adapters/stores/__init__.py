"""Durable state stores (adapters). External-system implementations of the StoreBackend
port (which, with the in-memory MemoryBackend default, stays in yaah.store).
Deferred: nats_kv, blob, sqlite — see docs/durable-state.md.
"""
from .file_backend import FileBackend

__all__ = ["FileBackend"]
