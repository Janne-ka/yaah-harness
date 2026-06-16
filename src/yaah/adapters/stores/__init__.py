"""Durable state stores (adapters). External-system implementations of the Store
port (which, with the in-memory MemoryStore default, stays in yaah.store).
Deferred: nats_kv, blob, sqlite — see docs/durable-state.md.
"""
from .file_store import FileStore

__all__ = ["FileStore"]
