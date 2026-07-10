"""PostgresExperimentStore — INSERT-only rows table over Postgres (AB-T3).

Used by: the `yaah ab` campaign stack when root config sets store.type = "postgres" —
  production campaigns wanting cross-host durability and no per-host filesystem.
Where: yaah.adapters.experiment_stores; implements yaah.experiment.ExperimentStore.
Why a dedicated adapter (not re-using PostgresBackend): rows are an INSERT-only
  append stream shaped as (experiment_id, seq, json_blob), not a K/V store.
  Folding this into a generic K/V backend would require awkward key naming and
  full-table scans; a dedicated INSERT-only table is the right shape and the
  JSONL trace precedent ("append-only … crash-safe") confirms the pattern.

Two tables live under the configured name:
  {t}:     (experiment_id TEXT, seq BIGINT, row_data TEXT NOT NULL,
            PRIMARY KEY (experiment_id, seq))
  {t}_seq: (experiment_id TEXT PRIMARY KEY, next_seq BIGINT NOT NULL)
  — the _seq table provides atomic sequence allocation in a SINGLE SQL statement:
    INSERT ... VALUES (exp_id, 2)
    ON CONFLICT (experiment_id) DO UPDATE SET next_seq = {t}_seq.next_seq + 1
    RETURNING next_seq - 1
  PostgreSQL's ON CONFLICT DO UPDATE acquires a row lock before the update, so
  two concurrent callers (any number of processes) serialize at the database and
  each receives a distinct seq — both land, no collision.

The psycopg import is lazy: constructing the runtime without this store type never
touches psycopg. Tests inject `connection=` (a fake) so the deterministic suite runs
without psycopg or a server — the same pattern as PostgresBackend.

One asyncio.Lock serializes statements on the single owned connection; cross-process
races are settled at the database, not the lock.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from ...experiment import ExperimentStore

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class PostgresExperimentStore(ExperimentStore):
    """Rows for all experiments live in table `{t}`; seq allocation in `{t}_seq`."""

    def __init__(self, dsn: str, table: str = "yaah_experiment_rows", *,
                 connection: Any = None) -> None:
        # `connection` is the external dependency seam: any object with
        #   `await execute(sql, params) -> cursor` (psycopg AsyncConnection shape).
        # Defaults to a lazily-built real connection. Tests pass a fake so this
        # store runs without psycopg / a server.
        if not dsn and connection is None:
            raise ValueError(
                'PostgresExperimentStore requires "dsn" '
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
        self._op_lock: Optional[asyncio.Lock] = None  # created lazily inside the loop

        t = table
        ts = table + "_seq"
        self._sql_ddl_rows = (
            "CREATE TABLE IF NOT EXISTS {t} ("
            "experiment_id TEXT NOT NULL, "
            "seq BIGINT NOT NULL, "
            "row_data TEXT NOT NULL, "
            "PRIMARY KEY (experiment_id, seq))").format(t=t)
        self._sql_ddl_seq = (
            "CREATE TABLE IF NOT EXISTS {ts} ("
            "experiment_id TEXT PRIMARY KEY, "
            "next_seq BIGINT NOT NULL)").format(ts=ts)
        # Atomic seq allocation — see module docstring for the race analysis.
        # First append: INSERT (exp, 2), no conflict → RETURNING 2-1=1 (seq=1).
        # Nth concurrent append: UPDATE next_seq = prev+1 → RETURNING prev (seq=prev).
        self._sql_alloc_seq = (
            "INSERT INTO {ts} (experiment_id, next_seq) VALUES (%s, 2) "
            "ON CONFLICT (experiment_id) DO UPDATE "
            "SET next_seq = {ts}.next_seq + 1 "
            "RETURNING next_seq - 1").format(ts=ts)
        # Values always travel as bind parameters — experiment_id and row_data are
        # NEVER interpolated into the SQL string.
        self._sql_insert = (
            "INSERT INTO {t} (experiment_id, seq, row_data) "
            "VALUES (%s, %s, %s)").format(t=t)
        self._sql_select = (
            "SELECT seq, row_data FROM {t} "
            "WHERE experiment_id = %s "
            "ORDER BY seq").format(t=t)

    # --- plumbing ------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        # Created on first async use, not in __init__: a 3.9 asyncio.Lock binds
        # its loop at construction, and the store is built before the loop runs.
        if self._op_lock is None:
            self._op_lock = asyncio.Lock()
        return self._op_lock

    async def _conn(self) -> Any:
        """The (lazily opened) connection, with both tables ensured once."""
        if self._connection is None:
            try:
                import psycopg  # type: ignore[import-not-found]  # lazy optional dep; only needed if this store is used
            except ImportError as e:
                raise ImportError(
                    'store type "postgres" needs the optional psycopg package: '
                    'pip install "psycopg[binary]" — the core engine stays '
                    "dependency-free; install it only on hosts using this store."
                ) from e
            self._connection = await psycopg.AsyncConnection.connect(
                self._dsn, autocommit=True)
        if not self._ready:
            await self._connection.execute(self._sql_ddl_rows)
            await self._connection.execute(self._sql_ddl_seq)
            self._ready = True
        return self._connection

    async def close(self) -> None:
        """Close the owned connection. No-op for an injected one — the caller
        owns its lifetime. Safe to call twice; the store reopens lazily."""
        conn, owned = self._connection, self._owns_connection
        self._ready = False
        if owned:
            self._connection = None
            if conn is not None:
                await conn.close()

    # --- ExperimentStore port ------------------------------------------------

    async def append_row(self, experiment_id: str, row: Dict[str, Any]) -> None:
        """Durably append one row. Seq is allocated atomically (single SQL
        statement) so concurrent callers from any number of processes each land
        a row with a distinct seq — no read-modify-write race."""
        row_text = json.dumps(row, sort_keys=True)
        async with self._lock():
            conn = await self._conn()
            cur = await conn.execute(self._sql_alloc_seq, (experiment_id,))
            seq_row = await cur.fetchone()
            seq = seq_row[0]
            await conn.execute(self._sql_insert, (experiment_id, seq, row_text))

    async def rows(self, experiment_id: str) -> List[Dict[str, Any]]:
        """Every row appended to the experiment, in seq (append) order.
        An unknown experiment id returns [] (not yet started, not an error).
        A row whose stored text fails json.loads raises ValueError naming table +
        experiment_id + seq — silently skipping a corrupt row would undercount a
        campaign's population, the exact reliability failure this store prevents."""
        async with self._lock():
            conn = await self._conn()
            cur = await conn.execute(self._sql_select, (experiment_id,))
            db_rows = await cur.fetchall()
        out: List[Dict[str, Any]] = []
        for seq, row_text in db_rows:
            try:
                out.append(json.loads(row_text))
            except json.JSONDecodeError as e:
                raise ValueError(
                    "corrupt experiment row in table {!r} for experiment {!r} "
                    "at seq {} — json decode failed: {} — "
                    "inspect and repair or DELETE the row; "
                    "rows before it are intact".format(
                        self._table, experiment_id, seq, e)) from e
        return out
