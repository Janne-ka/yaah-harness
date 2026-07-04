"""PostgresBackend — a durable StoreBackend extender over ONE Postgres table.

Used by: the runtime when root `state: {type: postgres, dsn: ...}` is set — the
shared-database durable-state story: batons, gate parking, and execute-once
survive process exit AND host boundaries (any process reaching the same
database can resume a run another process parked).
Where: hosts with `pip install psycopg` + a reachable Postgres. The import is
lazy (inside the first operation), so the zero-dependency core stays intact —
constructing the runtime without this state type never touches psycopg.
Why: one adapter of many behind the store port (file / memory / postgres are
peers — nothing may special-case this one); Postgres brings multi-host
durability the file backend's per-key flock can't.

One table (default `yaah_state`, override via `table`):
    key TEXT PRIMARY KEY, value BYTEA NOT NULL, rev BIGINT NOT NULL
created on first use (CREATE TABLE IF NOT EXISTS). Every mutation is ONE SQL
statement, so atomicity comes from Postgres itself — the analog of
FileBackend's per-key flock:
  - `put`   = upsert that bumps rev (INSERT .. ON CONFLICT DO UPDATE rev+1);
  - `cas`   = expected None  -> INSERT .. ON CONFLICT DO NOTHING RETURNING rev
              expected int   -> UPDATE .. WHERE key AND rev=expected RETURNING rev
              — the returned row's presence IS the conflict verdict, exactly
              MemoryBackend's semantics (create-if-absent / revisioned update);
  - `delete` drops the row, so the key's rev history resets (create-if-absent
    succeeds again) — same as MemoryBackend, which pops the rev with the value.
`ttl` is accepted but not auto-expired (higher layers sweep — see store.py).
`scan` snapshots matching rows before yielding, so callers may delete while
iterating (the sweep pattern); the prefix is LIKE-escaped so a literal '%'/'_'
in a key never acts as a wildcard.

One asyncio.Lock serializes statements on the single owned connection (a
psycopg AsyncConnection is not safe for concurrent operations); cross-process
races are settled by the database, not the lock. Tests inject `connection=`
(a fake) so the deterministic suite runs without psycopg or a server —
the litellm/langfuse injected-dependency pattern.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, AsyncGenerator, Optional, Tuple

from ...store import CompareAndSet, Scannable, StoreBackend

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _like_prefix(prefix: str) -> str:
    """Escape a key prefix for LIKE ... ESCAPE '\\' and append the wildcard —
    a literal %, _ or \\ in a key must match itself, not act as a pattern."""
    return (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")) + "%"


class PostgresBackend(StoreBackend, Scannable, CompareAndSet):
    def __init__(self, dsn: str, *, table: str = "yaah_state",
                 connection: Any = None) -> None:
        # `connection` is the external dependency, injected for testability: any
        # object with `await execute(sql, params) -> cursor` (psycopg's
        # AsyncConnection shape). Defaults to a lazily-built real connection
        # (only when used). Tests pass a fake so this backend runs without the
        # psycopg package / a server.
        if not dsn and connection is None:
            raise ValueError(
                'PostgresBackend requires "dsn" '
                '(e.g. "postgresql://user:pass@host:5432/dbname")')
        if not _IDENTIFIER.match(table):
            raise ValueError(
                "table {!r} is not a plain SQL identifier "
                "([A-Za-z_][A-Za-z0-9_]*) — it is interpolated into statements, "
                "so anything else is rejected up front".format(table))
        self._dsn = dsn
        self._table = table
        self._connection = connection
        self._owns_connection = connection is None   # close() only what we opened
        self._ready = False                          # DDL ran on this connection
        self._op_lock: Optional[asyncio.Lock] = None  # created lazily IN the loop

        # Statements, formatted once (table validated above; values/keys always
        # travel as bind parameters — never interpolated).
        t = table
        self._sql_ddl = ("CREATE TABLE IF NOT EXISTS {t} ("
                         "key TEXT PRIMARY KEY, "
                         "value BYTEA NOT NULL, "
                         "rev BIGINT NOT NULL)").format(t=t)
        self._sql_get = "SELECT value FROM {t} WHERE key = %s".format(t=t)
        self._sql_get_rev = "SELECT value, rev FROM {t} WHERE key = %s".format(t=t)
        self._sql_put = ("INSERT INTO {t} (key, value, rev) VALUES (%s, %s, 1) "
                         "ON CONFLICT (key) DO UPDATE "
                         "SET value = EXCLUDED.value, rev = {t}.rev + 1").format(t=t)
        self._sql_delete = "DELETE FROM {t} WHERE key = %s".format(t=t)
        self._sql_scan = ("SELECT key, value FROM {t} "
                          "WHERE key LIKE %s ESCAPE '\\' ORDER BY key").format(t=t)
        self._sql_cas_insert = ("INSERT INTO {t} (key, value, rev) VALUES (%s, %s, 1) "
                                "ON CONFLICT (key) DO NOTHING RETURNING rev").format(t=t)
        self._sql_cas_update = ("UPDATE {t} SET value = %s, rev = rev + 1 "
                                "WHERE key = %s AND rev = %s RETURNING rev").format(t=t)

    # --- plumbing ------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        # Created on first async use, not in __init__: a 3.9 asyncio.Lock binds
        # its loop at construction, and the backend is built before the loop runs.
        if self._op_lock is None:
            self._op_lock = asyncio.Lock()
        return self._op_lock

    async def _conn(self) -> Any:
        """The (lazily opened) connection, with the table ensured once."""
        if self._connection is None:
            try:
                import psycopg  # lazy: only needed if this backend is used
            except ImportError as e:
                raise ImportError(
                    'state type "postgres" needs the optional psycopg package: '
                    'pip install "psycopg[binary]" — the core engine stays '
                    "dependency-free; install it only on hosts using this backend."
                ) from e
            # autocommit: every statement is its own transaction, which is what
            # the single-statement atomicity above relies on.
            self._connection = await psycopg.AsyncConnection.connect(
                self._dsn, autocommit=True)
        if not self._ready:
            await self._connection.execute(self._sql_ddl)
            self._ready = True
        return self._connection

    async def close(self) -> None:
        """Close the owned connection (no-op for an injected one — the caller
        owns its lifetime). Safe to call twice; the backend reopens lazily."""
        conn, owned = self._connection, self._owns_connection
        self._ready = False
        if owned:
            self._connection = None
            if conn is not None:
                await conn.close()

    # --- core tier -----------------------------------------------------------

    async def get(self, key: str) -> Optional[bytes]:
        async with self._lock():
            cur = await (await self._conn()).execute(self._sql_get, (key,))
            row = await cur.fetchone()
        return bytes(row[0]) if row else None

    async def put(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        # ttl accepted, not auto-expired (module docstring); the upsert bumps
        # rev in the SAME statement so concurrent puts can't lose a bump.
        async with self._lock():
            await (await self._conn()).execute(self._sql_put, (key, value))

    async def delete(self, key: str) -> None:
        async with self._lock():
            await (await self._conn()).execute(self._sql_delete, (key,))

    # --- +scan tier ----------------------------------------------------------

    async def scan(self, prefix: str) -> AsyncGenerator[Tuple[str, bytes], None]:
        async with self._lock():
            cur = await (await self._conn()).execute(
                self._sql_scan, (_like_prefix(prefix),))
            rows = await cur.fetchall()
        # snapshot fetched, lock released: callers delete while iterating (sweep)
        for key, value in rows:
            yield key, bytes(value)

    # --- +cas tier -----------------------------------------------------------

    async def get_rev(self, key: str) -> Tuple[Optional[bytes], Optional[int]]:
        async with self._lock():
            cur = await (await self._conn()).execute(self._sql_get_rev, (key,))
            row = await cur.fetchone()
        return (bytes(row[0]), row[1]) if row else (None, None)

    async def cas(self, key: str, value: bytes, *, expected: Optional[int],
                  ttl: Optional[float] = None) -> Optional[int]:
        """Atomic compare-and-set: `expected` None = create-if-absent, int = the
        rev the caller last saw. Returns the new rev, or None on conflict —
        decided by ONE statement, so racing writers (any process) settle in the
        database: the insert's ON CONFLICT or the update's rev match admits
        exactly one winner per round."""
        async with self._lock():
            conn = await self._conn()
            if expected is None:
                cur = await conn.execute(self._sql_cas_insert, (key, value))
            else:
                cur = await conn.execute(self._sql_cas_update, (value, key, expected))
            row = await cur.fetchone()
        return row[0] if row else None
