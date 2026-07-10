"""PostgresExperimentStore — INSERT-only rows adapter + store_from_config factory.

Deterministic part (no database, no psycopg): construction guards, SQL shape,
lazy-import error message, port conformance, and full port contract (append/
order/isolation/corruption-loud/hostile-ids) against a fake connection that
emulates exactly the statements the store issues — plus factory dispatch covering
all known/unknown types and keys.

Atomic-seq claim: two stores with DISTINCT shared-state fake connections both
land a row for the same experiment, with different seq values (no row clobbered,
no collision) — the DB-side ON CONFLICT DO UPDATE that the real backend relies on.

Integration part (ONE path, self-gated): set YAAH_TEST_POSTGRES_DSN to a real
postgres DSN and the full port contract + concurrent appender proof + corruption
loud run against a real, throwaway table (created and dropped by the test).
Absent env → clear skip, so the normal suite stays zero-dep.

Run: cd yaah && PYTHONPATH=src python3 tests/test_postgres_experiment_store.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Tuple

from yaah.adapters.experiment_stores import JsonlExperimentStore, PostgresExperimentStore
from yaah.experiment import ExperimentStore


# ---------------------------------------------------------------------------
# Shared fake: two connections over the same dict simulate cross-process races
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, rows: List[Tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)


class FakeExperimentConnection:
    """Emulates postgres for exactly the statements PostgresExperimentStore issues.

    Pass `shared=` (a plain dict) to share counter + row state across multiple
    fake instances — this is the concurrent-appender test harness: two stores
    each holding their own fake but sharing the same underlying storage lets the
    seq allocation race play out in-process without a real database.
    """

    def __init__(self, shared: Optional[Dict[str, Any]] = None) -> None:
        self._shared: Dict[str, Any] = {} if shared is None else shared

    async def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> FakeCursor:
        s = " ".join(sql.split())
        # DDL — both rows table and seq table
        if s.startswith("CREATE TABLE"):
            return FakeCursor([])
        # seq allocation: INSERT...ON CONFLICT DO UPDATE...RETURNING next_seq - 1
        if "RETURNING next_seq - 1" in s:
            experiment_id = params[0]
            counters: Dict[str, int] = self._shared.setdefault("counters", {})
            seq = counters.get(experiment_id, 1)
            counters[experiment_id] = seq + 1
            return FakeCursor([(seq,)])
        # row insert
        if s.startswith("INSERT INTO") and "experiment_id, seq, row_data" in s:
            experiment_id, seq, row_text = params[0], params[1], params[2]
            rows_store: Dict[str, List[Tuple[int, str]]] = self._shared.setdefault("rows", {})
            rows_store.setdefault(experiment_id, []).append((seq, row_text))
            return FakeCursor([])
        # row query
        if s.startswith("SELECT seq, row_data"):
            experiment_id = params[0]
            rows_store = self._shared.get("rows", {})
            exp_rows = sorted(rows_store.get(experiment_id, []))  # sort by seq asc
            return FakeCursor(list(exp_rows))
        raise AssertionError("fake got an unrecognized statement: {!r}".format(s))


# ---------------------------------------------------------------------------
# Construction guards + SQL shape
# ---------------------------------------------------------------------------

def test_construction_guards() -> None:
    # no dsn AND no injected connection → actionable ValueError
    try:
        PostgresExperimentStore("")
    except ValueError as e:
        assert "dsn" in str(e), e
    else:
        raise AssertionError("empty dsn must be rejected up front")

    # bad table name → actionable ValueError naming the identifier rule
    for bad in ("rows; DROP TABLE x", 'a"b', "1abc", "sp ace", ""):
        try:
            PostgresExperimentStore("postgresql://u@h/db", bad)
        except ValueError as e:
            assert "identifier" in str(e), e
        else:
            raise AssertionError("table {!r} must be rejected".format(bad))

    # injected connection with empty dsn is allowed (test path)
    store = PostgresExperimentStore("", connection=FakeExperimentConnection())
    assert store._table == "yaah_experiment_rows"

    # table override propagates
    store2 = PostgresExperimentStore("dsn://x", "my_rows", connection=FakeExperimentConnection())
    assert store2._table == "my_rows"


def test_sql_construction() -> None:
    s = PostgresExperimentStore("postgresql://u@h/db", "exp_rows",
                                connection=FakeExperimentConnection())
    # row table DDL
    assert "exp_rows" in s._sql_ddl_rows
    assert "experiment_id TEXT" in s._sql_ddl_rows
    assert "seq BIGINT" in s._sql_ddl_rows
    assert "row_data TEXT" in s._sql_ddl_rows
    assert "PRIMARY KEY (experiment_id, seq)" in s._sql_ddl_rows
    # seq counter table DDL
    assert "exp_rows_seq" in s._sql_ddl_seq
    assert "experiment_id TEXT PRIMARY KEY" in s._sql_ddl_seq
    assert "next_seq BIGINT" in s._sql_ddl_seq
    # seq allocation: ON CONFLICT update + RETURNING
    assert "ON CONFLICT" in s._sql_alloc_seq and "RETURNING next_seq - 1" in s._sql_alloc_seq
    # insert: all three columns bound as params, never interpolated
    assert "experiment_id, seq, row_data" in s._sql_insert
    assert "%s, %s, %s" in s._sql_insert
    # select: ordered by seq
    assert "ORDER BY seq" in s._sql_select
    # every statement targets the configured table
    for sql in (s._sql_ddl_rows, s._sql_insert, s._sql_select):
        assert "exp_rows" in sql, sql


def test_lazy_import_error_is_actionable() -> None:
    """Even with a real psycopg installed, force the import to fail so the
    error message content is tested in every environment."""
    prior = sys.modules.get("psycopg", "absent")
    sys.modules["psycopg"] = None  # type: ignore[assignment]
    try:
        st = PostgresExperimentStore("postgresql://u@h/db")
        try:
            asyncio.run(st.append_row("exp", {}))
        except ImportError as e:
            assert "psycopg" in str(e) and "pip install" in str(e), e
        else:
            raise AssertionError("missing psycopg must raise ImportError")
    finally:
        if prior == "absent":
            del sys.modules["psycopg"]
        else:
            sys.modules["psycopg"] = prior  # type: ignore[assignment]


def test_port_conformance() -> None:
    st = PostgresExperimentStore("postgresql://u@h/db", connection=FakeExperimentConnection())
    assert isinstance(st, ExperimentStore)


# ---------------------------------------------------------------------------
# Port contract against the fake
# ---------------------------------------------------------------------------

async def scenario_fake_port_contract() -> None:
    """append/order/isolation/empty-id — same contracts as JsonlExperimentStore."""
    fake = FakeExperimentConnection()
    st = PostgresExperimentStore("postgresql://ignored", connection=fake)

    # rows come back in append order
    await st.append_row("exp-a", {"variant": "A", "rep": 0})
    await st.append_row("exp-a", {"variant": "B", "rep": 0})
    await st.append_row("exp-b", {"variant": "X", "rep": 0})
    rows_a = await st.rows("exp-a")
    assert [r["variant"] for r in rows_a] == ["A", "B"], rows_a

    # isolation: exp-b is independent
    rows_b = await st.rows("exp-b")
    assert len(rows_b) == 1 and rows_b[0]["variant"] == "X"

    # unknown experiment id = empty list, not an error
    assert await st.rows("no-such-exp") == []

    # row dicts survive the round-trip intact (sort_keys is an implementation
    # detail; the VALUES must be equal)
    await st.append_row("rt", {"z": 3, "a": 1, "m": True, "n": None})
    roundtrip = (await st.rows("rt"))[0]
    assert roundtrip == {"z": 3, "a": 1, "m": True, "n": None}, roundtrip


async def scenario_fake_corruption_loud() -> None:
    """A stored row that fails json.loads must raise ValueError naming
    table + experiment_id + seq — never a silent skip."""
    shared: Dict[str, Any] = {}
    fake = FakeExperimentConnection(shared)
    st = PostgresExperimentStore("postgresql://ignored", "my_tbl", connection=fake)

    await st.append_row("exp-c", {"ok": 1})
    # inject a corrupt row directly into the shared store
    rows_store: Dict[str, List[Tuple[int, str]]] = shared.setdefault("rows", {})
    rows_store["exp-c"].append((2, "{torn write"))

    try:
        await st.rows("exp-c")
        raise AssertionError("corrupt row must raise ValueError")
    except ValueError as e:
        msg = str(e)
        assert "my_tbl" in msg, "table name must appear in error: {}".format(msg)
        assert "exp-c" in msg, "experiment id must appear in error: {}".format(msg)
        assert "2" in msg, "seq must appear in error: {}".format(msg)


async def scenario_fake_hostile_experiment_ids() -> None:
    """Quotes, path chars, unicode in experiment_id must work (they travel as
    SQL bind parameters, never interpolated) and must never traverse paths."""
    hostile_ids = [
        "../evil/exp",
        "exp'; DROP TABLE x--",
        "ex\x00p",
        "exp/foo/../bar",
        "exp with spaces",
        "expé中文",   # non-ASCII unicode
    ]
    for exp_id in hostile_ids:
        shared: Dict[str, Any] = {}
        fake = FakeExperimentConnection(shared)
        st = PostgresExperimentStore("postgresql://ignored", connection=fake)
        await st.append_row(exp_id, {"ok": 1})
        got = await st.rows(exp_id)
        assert got == [{"ok": 1}], "id {!r}: got {!r}".format(exp_id, got)


async def scenario_fake_concurrent_appenders() -> None:
    """Two stores with DISTINCT connections over the same shared state —
    both appenders must land a row with distinct seq values (no clobber,
    no UniqueConstraint collision). The DB-side ON CONFLICT DO UPDATE
    in the seq allocation statement is what makes this safe in production;
    the fake's shared dict emulates the atomic counter semantics."""
    shared: Dict[str, Any] = {}
    fake1 = FakeExperimentConnection(shared)
    fake2 = FakeExperimentConnection(shared)
    st1 = PostgresExperimentStore("postgresql://ignored", connection=fake1)
    st2 = PostgresExperimentStore("postgresql://ignored", connection=fake2)

    await asyncio.gather(
        st1.append_row("exp-race", {"writer": 1}),
        st2.append_row("exp-race", {"writer": 2}),
    )

    landed = sorted(shared["rows"]["exp-race"])   # [(seq, row_text), ...]
    assert len(landed) == 2, "both appenders must land: {}".format(landed)
    seqs = [seq for seq, _ in landed]
    assert len(set(seqs)) == 2, "seqs must be distinct: {}".format(seqs)

    # rows() returns them in seq order (both land, both readable)
    # use a fresh read connection to verify
    read_fake = FakeExperimentConnection(shared)
    read_st = PostgresExperimentStore("postgresql://ignored", connection=read_fake)
    all_rows = await read_st.rows("exp-race")
    assert len(all_rows) == 2
    writers = {r["writer"] for r in all_rows}
    assert writers == {1, 2}, writers


# ---------------------------------------------------------------------------
# Factory dispatch
# ---------------------------------------------------------------------------

def test_factory_dispatch() -> None:
    from yaah.experiment.store_factory import store_from_config

    with tempfile.TemporaryDirectory() as d:
        # no `store` key → JsonlExperimentStore
        st = store_from_config({"store": {}}, d)
        assert isinstance(st, JsonlExperimentStore)

        # explicit type=jsonl
        st = store_from_config({"store": {"type": "jsonl"}}, d)
        assert isinstance(st, JsonlExperimentStore)

        # explicit dir for jsonl
        st = store_from_config({"store": {"type": "jsonl", "dir": ".ab"}}, d)
        assert isinstance(st, JsonlExperimentStore)

        # type=postgres with dsn
        st = store_from_config({"store": {"type": "postgres",
                                          "dsn": "postgresql://u@h/db"}}, d)
        assert isinstance(st, PostgresExperimentStore)
        assert st._table == "yaah_experiment_rows"   # default table

        # type=postgres with table override
        st = store_from_config({"store": {"type": "postgres",
                                          "dsn": "postgresql://u@h/db",
                                          "table": "my_rows"}}, d)
        assert isinstance(st, PostgresExperimentStore) and st._table == "my_rows"

        # type=postgres missing dsn → loud, names "dsn"
        try:
            store_from_config({"store": {"type": "postgres"}}, d)
        except ValueError as e:
            assert "dsn" in str(e), e
        else:
            raise AssertionError("postgres without dsn must fail loud")

        # unknown type → loud, names the known types
        try:
            store_from_config({"store": {"type": "duckdb"}}, d)
        except ValueError as e:
            assert "duckdb" in str(e) and ("jsonl" in str(e) or "postgres" in str(e)), e
        else:
            raise AssertionError("unknown store type must fail loud")

        # unknown key for jsonl → loud, names the key
        try:
            store_from_config({"store": {"type": "jsonl", "badkey": "x"}}, d)
        except ValueError as e:
            assert "badkey" in str(e), e
        else:
            raise AssertionError("unknown jsonl key must fail loud")

        # unknown key for postgres → loud, names the key
        try:
            store_from_config({"store": {"type": "postgres",
                                         "dsn": "postgresql://u@h/db",
                                         "tabel": "t"}}, d)
        except ValueError as e:
            assert "tabel" in str(e), e
        else:
            raise AssertionError("unknown postgres key must fail loud")

        # jsonl path unbroken (existing test_experiment_store.py contract path)
        jsonl = store_from_config({"store": {}}, d)
        asyncio.run(jsonl.append_row("t-exp", {"v": "A"}))
        assert asyncio.run(jsonl.rows("t-exp")) == [{"v": "A"}]


async def scenario_opened_store_ownership() -> None:
    """opened_store closes stores it BUILT (even when the body raises) and
    never closes an injected, caller-owned store — the connection-leak fix
    from the adversarial eval."""
    import yaah.experiment.store_factory as sf

    closed: List[str] = []

    class TrackedStore:
        async def append_row(self, experiment_id, row):  # pragma: no cover - unused
            pass

        async def rows(self, experiment_id):
            return []

        async def close(self):
            closed.append("closed")

    # built store → closed on normal exit
    orig = sf.store_from_config
    sf.store_from_config = lambda cfg, base: TrackedStore()  # type: ignore[assignment]
    try:
        async with sf.opened_store({}, ".") as st:
            await st.rows("x")
        assert closed == ["closed"], closed

        # built store → closed even when the body raises
        closed.clear()
        try:
            async with sf.opened_store({}, ".") as st:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert closed == ["closed"], "close must run on the exception path"
    finally:
        sf.store_from_config = orig

    # injected store → caller-owned, NEVER closed
    closed.clear()
    injected = TrackedStore()
    async with sf.opened_store({}, ".", injected) as st:
        assert st is injected
    assert closed == [], "an injected store must not be closed"

    # built jsonl store (no close attr) → exits without error
    with tempfile.TemporaryDirectory() as d:
        async with sf.opened_store({"store": {"dir": d}}, d) as st:
            assert isinstance(st, JsonlExperimentStore)


# ---------------------------------------------------------------------------
# _check_experiment store-block validation
# ---------------------------------------------------------------------------

def test_check_experiment_store_validation() -> None:
    from yaah.experiment.runner import _check_experiment

    # valid: no store block
    _check_experiment({"id": "e", "variants": {"a": "a.json"},
                       "inputs": [{}], "price_map": {"m": {"input": 1, "output": 1}}})
    # valid: jsonl explicit
    _check_experiment({"id": "e", "variants": {"a": "a.json"},
                       "inputs": [{}], "price_map": {},
                       "store": {"type": "jsonl", "dir": ".ab"}})
    # valid: postgres
    _check_experiment({"id": "e", "variants": {"a": "a.json"},
                       "inputs": [{}], "price_map": {},
                       "store": {"type": "postgres", "dsn": "postgresql://u@h/db"}})

    # unknown type
    try:
        _check_experiment({"id": "e", "variants": {"a": "a.json"},
                           "inputs": [{}], "price_map": {},
                           "store": {"type": "redis"}})
    except ValueError as e:
        assert "redis" in str(e), e
    else:
        raise AssertionError("unknown store type must fail _check_experiment")

    # unknown key in jsonl block
    try:
        _check_experiment({"id": "e", "variants": {"a": "a.json"},
                           "inputs": [{}], "price_map": {},
                           "store": {"type": "jsonl", "host": "x"}})
    except ValueError as e:
        assert "host" in str(e), e
    else:
        raise AssertionError("unknown store key must fail _check_experiment")

    # missing dsn for postgres
    try:
        _check_experiment({"id": "e", "variants": {"a": "a.json"},
                           "inputs": [{}], "price_map": {},
                           "store": {"type": "postgres"}})
    except ValueError as e:
        assert "dsn" in str(e), e
    else:
        raise AssertionError("postgres without dsn must fail _check_experiment")


# ---------------------------------------------------------------------------
# Integration (real Postgres, env-gated)
# ---------------------------------------------------------------------------

async def scenario_real_postgres(dsn: str) -> None:
    table = "yaah_exp_it_" + uuid.uuid4().hex[:12]
    st = PostgresExperimentStore(dsn, table)
    try:
        # port contract
        await st.append_row("exp-a", {"variant": "A", "rep": 0})
        await st.append_row("exp-a", {"variant": "B", "rep": 1})
        await st.append_row("exp-b", {"variant": "X", "rep": 0})
        rows_a = await st.rows("exp-a")
        assert [r["variant"] for r in rows_a] == ["A", "B"], rows_a
        assert (await st.rows("exp-b"))[0]["variant"] == "X"
        assert await st.rows("no-such") == []

        # hostile experiment ids round-trip via bind params (never path-injected)
        await st.append_row("exp'; DROP TABLE t--", {"ok": True})
        got = await st.rows("exp'; DROP TABLE t--")
        assert got == [{"ok": True}], got

        # corruption is loud
        conn = await st._conn()
        await conn.execute(
            "INSERT INTO {} (experiment_id, seq, row_data) VALUES (%s, %s, %s)".format(table),
            ("exp-corrupt", 1, "{torn"))
        try:
            await st.rows("exp-corrupt")
            raise AssertionError("corrupt row must raise ValueError on real DB")
        except ValueError as e:
            assert table in str(e) and "exp-corrupt" in str(e), e

        # concurrent appenders via DISTINCT connections — the DB must settle the race
        await st.append_row("race", {"first": True})   # ensure table + counter exist
        racers = [PostgresExperimentStore(dsn, table) for _ in range(6)]
        try:
            await asyncio.gather(*[
                r.append_row("race", {"racer": i}) for i, r in enumerate(racers)
            ])
        finally:
            for r in racers:
                await r.close()
        all_race_rows = await st.rows("race")
        assert len(all_race_rows) == 7, "1 seed + 6 racers: {}".format(len(all_race_rows))
        # Verify no seq collision by checking all rows are distinct
        # (all 7 must land; a collision would drop one, leaving only 6)
        assert len({json.dumps(r, sort_keys=True) for r in all_race_rows}) == 7, (
            "all 7 rows must be distinct — seq collision would drop one")

    finally:
        conn = await st._conn()
        await conn.execute("DROP TABLE IF EXISTS {}".format(table))
        await conn.execute("DROP TABLE IF EXISTS {}_seq".format(table))
        await st.close()
    print("ok (integration against {})".format(dsn.split("@")[-1] if "@" in dsn else dsn))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    test_construction_guards()
    test_sql_construction()
    test_lazy_import_error_is_actionable()
    test_port_conformance()
    asyncio.run(scenario_fake_port_contract())
    asyncio.run(scenario_fake_corruption_loud())
    asyncio.run(scenario_fake_hostile_experiment_ids())
    asyncio.run(scenario_fake_concurrent_appenders())
    test_factory_dispatch()
    asyncio.run(scenario_opened_store_ownership())
    test_check_experiment_store_validation()

    dsn = os.environ.get("YAAH_TEST_POSTGRES_DSN")
    if dsn:
        asyncio.run(scenario_real_postgres(dsn))
    else:
        print("skip: YAAH_TEST_POSTGRES_DSN not set — postgres integration path skipped")
    print("PASS")


if __name__ == "__main__":
    main()
