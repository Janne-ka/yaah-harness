"""FileBackend — a durable StoreBackend extender backed by one file per key.

Used by: the runtime when root `state: {type: file, dir: ...}` is set. Makes a
parked human gate (and execute-once results) survive process exit, so a run
suspended by one process can be resumed by ANOTHER process sharing the directory
(the cross-process human-gate story; see docs/durable-state.md). Tests use it for
a deterministic cross-process proof (no broker needed).
Where: a concrete extender of the yaah.store base contract (core + scan + cas).
Why: the file-based-state default — matches the project's "state is files"
philosophy with zero dependencies.

Each key is one JSON file `<urlencoded-key>.json` = {"rev": int, "value": b64};
writes are atomic (temp + os.replace), and `put`/`cas` serialize their
read-modify-write under a per-key flock on POSIX (released by the OS on process
exit, so a crash can't deadlock the next writer). On non-POSIX hosts (no fcntl)
both fall back to the unlocked RMW — use nats_kv there for strict cross-process
guarantees. `ttl` is accepted but not auto-expired (higher layers sweep).

Targets Python 3.9+.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, AsyncGenerator, Dict, Optional, Tuple
from urllib.parse import quote, unquote

from ...store import CompareAndSet, Scannable, StoreBackend


class FileBackend(StoreBackend, Scannable, CompareAndSet):
    def __init__(self, base_dir: str) -> None:
        self._dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self._dir, quote(key, safe="") + ".json")

    def _read(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _write(self, path: str, value: bytes, rev: int) -> None:
        record = {"rev": rev, "value": base64.b64encode(value).decode("ascii")}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, path)  # atomic

    def _rev(self, key: str) -> Optional[int]:
        rec = self._read(self._path(key))
        return rec["rev"] if rec else None

    async def get(self, key: str) -> Optional[bytes]:
        rec = self._read(self._path(key))
        return base64.b64decode(rec["value"]) if rec else None

    async def put(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        # Same per-key flock as `cas` (assessment #13): the rev-bump here is a
        # read-modify-write too — unlocked, a put racing anything lost a rev and
        # broke a concurrent CAS's conflict detection.
        await _to_thread(self._put_locked, key, value)

    def _put_locked(self, key: str, value: bytes) -> None:
        def rmw() -> None:
            self._write(self._path(key), value, (self._rev(key) or 0) + 1)
        self._with_key_lock(key, rmw)

    async def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    async def scan(self, prefix: str) -> AsyncGenerator[Tuple[str, bytes], None]:
        for name in list(os.listdir(self._dir)):  # snapshot: callers delete while iterating
            if not name.endswith(".json"):
                continue
            key = unquote(name[:-len(".json")])
            if key.startswith(prefix):
                rec = self._read(os.path.join(self._dir, name))
                if rec:
                    yield key, base64.b64decode(rec["value"])

    async def get_rev(self, key: str) -> Tuple[Optional[bytes], Optional[int]]:
        rec = self._read(self._path(key))
        return (base64.b64decode(rec["value"]), rec["rev"]) if rec else (None, None)

    async def cas(self, key: str, value: bytes, *, expected: Optional[int],
                  ttl: Optional[float] = None) -> Optional[int]:
        """Atomic compare-and-set across processes (assessment cluster 2 B3).
        Previously a non-atomic read-modify-write: two processes both reading
        rev=N then both writing rev=N+1 silently lost one update and defeated
        CAS conflict detection. We now serialize the RMW under a per-key
        exclusive flock — POSIX OS releases the lock automatically on process
        exit, so a crash mid-operation can't deadlock the next writer.

        Windows / non-POSIX: fcntl is unavailable; falls back to the original
        non-atomic RMW with a docstring warning. Use nats_kv for strict
        cross-process CAS on non-POSIX deployments."""
        return await _to_thread(self._cas_locked, key, value, expected)

    def _cas_locked(self, key: str, value: bytes, expected: Optional[int]) -> Optional[int]:
        def rmw() -> Optional[int]:
            current = self._rev(key)
            if current != expected:
                return None
            new = (current or 0) + 1
            self._write(self._path(key), value, new)
            return new
        return self._with_key_lock(key, rmw)

    def _with_key_lock(self, key: str, fn: Any) -> Any:
        """Run `fn` under an exclusive per-key flock — the one serialization
        point for every read-modify-write (`put` and `cas`). The lock is a
        sidecar file (a rendezvous, so we don't depend on the data file
        existing) and is released by the OS on process exit, so a crash
        mid-operation can't deadlock the next writer. Non-POSIX (no fcntl):
        runs `fn` unlocked — the original best-effort behaviour; use nats_kv
        for strict cross-process guarantees there."""
        try:
            import fcntl
        except ImportError:                                  # non-POSIX fallback (Windows)
            return fn()
        with open(self._path(key) + ".lock", "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


async def _to_thread(fn, *args, **kwargs):
    """asyncio.to_thread wrapper — file IO + flock blocks; running it on the
    event loop would serialize the harness. Inlined to keep this file
    self-contained."""
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)
