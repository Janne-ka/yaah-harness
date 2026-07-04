"""PostgresBackend — the shared-DB StoreBackend extender (core + scan + cas).

Deterministic part (no database, no psycopg): SQL construction + identifier
guards, the actionable lazy-import error, config-type registration
(`state: {type: postgres, dsn: ...}`) + validate acceptance, and full tier
parity (get/put/delete/scan/get_rev/cas) against an in-memory fake connection
that emulates exactly the statements the backend issues — the SAME scenario
MemoryBackend/FileBackend pass in tests/test_store.py, so CAS semantics can't
silently diverge.

Integration part (ONE path, self-gated): set YAAH_TEST_POSTGRES_DSN to a real
postgres DSN and the same tier-parity scenario + a concurrent-CAS race + the
BatonStore save/load/list_suspended round-trip run against a real, throwaway
table (created and dropped by the test). Absent env -> clear skip, so the
normal suite stays zero-dep.

Run: cd yaah && PYTHONPATH=src python3 tests/test_postgres_backend.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from yaah import Envelope
from yaah.adapters.stores import PostgresBackend
from yaah.adapters.stores.postgres_backend import _like_prefix
from yaah.harness import BatonStore
from yaah.harness.baton import Baton
from yaah.store import CompareAndSet, Scannable, StoreBackend


# --- the fake connection/cursor double (deterministic, no DB) ---------------------

def _like_to_regex(pattern: str) -> "re.Pattern":
    """LIKE ... ESCAPE '\\' semantics, so the fake honors the backend's escaping
    (a literal '%' in a key must not act as a wildcard)."""
    out: List[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "%":
            out.append(".*")
            i += 1
        elif c == "_":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


class FakeCursor:
    def __init__(self, rows: List[Tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> List[Tuple[Any, ...]]:
        return list(self._rows)


class FakeConnection:
    """Emulates postgres for exactly the statements PostgresBackend issues,
    over {key: (value, rev)} — enough to prove the tier semantics without a DB.
    Dispatches on statement shape, so a changed statement that no longer says
    what the backend means FAILS here instead of passing vacuously."""

    def __init__(self) -> None:
        self.rows: Dict[str, Tuple[bytes, int]] = {}
        self.executed: List[Tuple[str, Tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> FakeCursor:
        s = " ".join(sql.split())
        self.executed.append((s, tuple(params)))
        if s.startswith("CREATE TABLE"):
            return FakeCursor([])
        if s.startswith("SELECT value, rev"):
            hit = self.rows.get(params[0])
            return FakeCursor([hit] if hit else [])
        if s.startswith("SELECT value"):
            hit = self.rows.get(params[0])
            return FakeCursor([(hit[0],)] if hit else [])
        if s.startswith("SELECT key, value"):
            rx = _like_to_regex(params[0])
            return FakeCursor(sorted((k, v[0]) for k, v in self.rows.items()
                                     if rx.match(k)))
        if s.startswith("INSERT") and "DO NOTHING" in s:
            key, value = params
            if key in self.rows:
                return FakeCursor([])                       # conflict -> no row
            self.rows[key] = (bytes(value), 1)
            return FakeCursor([(1,)])
        if s.startswith("INSERT") and "DO UPDATE" in s:
            key, value = params
            rev = (self.rows[key][1] + 1) if key in self.rows else 1
            self.rows[key] = (bytes(value), rev)
            return FakeCursor([])
        if s.startswith("UPDATE"):
            value, key, expected = params
            hit = self.rows.get(key)
            if hit is None or hit[1] != expected:
                return FakeCursor([])                       # conflict -> no row
            self.rows[key] = (bytes(value), expected + 1)
            return FakeCursor([(expected + 1,)])
        if s.startswith("DELETE"):
            self.rows.pop(params[0], None)
            return FakeCursor([])
        raise AssertionError("fake got an unrecognized statement: {!r}".format(s))


# --- construction guards + SQL shape ----------------------------------------------

def test_construction_guards() -> None:
    # no dsn and no injected connection -> actionable ValueError at build time
    try:
        PostgresBackend("")
    except ValueError as e:
        assert "dsn" in str(e), e
    else:
        raise AssertionError("empty dsn must be rejected up front")
    # a table name is interpolated into SQL -> must be a plain identifier
    for bad in ("state; DROP TABLE x", 'a"b', "1abc", "sp ace", ""):
        try:
            PostgresBackend("postgresql://u@h/db", table=bad)
        except ValueError as e:
            assert "identifier" in str(e), e
        else:
            raise AssertionError("table {!r} must be rejected".format(bad))


def test_sql_construction() -> None:
    b = PostgresBackend("postgresql://u@h/db", table="custom_tbl")
    ddl = b._sql_ddl
    assert "custom_tbl" in ddl and "key TEXT PRIMARY KEY" in ddl, ddl
    assert "value BYTEA" in ddl and "rev BIGINT" in ddl, ddl
    # cas create-if-absent: insert-or-nothing, row presence IS the verdict
    assert "ON CONFLICT (key) DO NOTHING RETURNING rev" in b._sql_cas_insert
    # cas revisioned update: match on (key, rev), bump atomically
    assert "rev = rev + 1 WHERE key = %s AND rev = %s RETURNING rev" in b._sql_cas_update
    # put: single-statement upsert that bumps rev (DB-side atomic RMW)
    assert "DO UPDATE" in b._sql_put and "rev + 1" in b._sql_put
    # scan: prefix via escaped LIKE
    assert "LIKE" in b._sql_scan and "ESCAPE" in b._sql_scan
    # every statement targets the configured table
    for sql in (b._sql_get, b._sql_get_rev, b._sql_put, b._sql_delete,
                b._sql_scan, b._sql_cas_insert, b._sql_cas_update):
        assert "custom_tbl" in sql, sql


def test_like_prefix_escaping() -> None:
    assert _like_prefix("a%b_c\\d") == "a\\%b\\_c\\\\d%"
    assert _like_prefix("") == "%"
    # round-trip through the fake's LIKE semantics: literal % must not wildcard
    rx = _like_to_regex(_like_prefix("a%b"))
    assert rx.match("a%b:x") and not rx.match("axb:y")


def test_lazy_import_error_is_actionable() -> None:
    # force `import psycopg` to fail even on hosts that have it installed
    prior = sys.modules.get("psycopg", "absent")
    sys.modules["psycopg"] = None  # type: ignore[assignment]
    try:
        b = PostgresBackend("postgresql://u@h/db")
        try:
            asyncio.run(b.get("k"))
        except ImportError as e:
            assert "psycopg" in str(e) and "pip install" in str(e), e
        else:
            raise AssertionError("missing psycopg must raise ImportError")
    finally:
        if prior == "absent":
            del sys.modules["psycopg"]
        else:
            sys.modules["psycopg"] = prior  # type: ignore[assignment]


# --- registration: ports, config type, validate ------------------------------------

def test_declares_its_ports() -> None:
    for port in (StoreBackend, Scannable, CompareAndSet):
        assert port in PostgresBackend.__mro__, port


def test_config_type_registration() -> None:
    from yaah import runtime_factories as rf
    from yaah.validate import validate_root

    factory, keys = rf._STATE_TYPES["postgres"]
    assert keys == frozenset({"dsn", "table"}), keys

    built = rf._build_store({"type": "postgres", "dsn": "postgresql://u@h/db",
                             "table": "t1"}, ".")
    assert isinstance(built, PostgresBackend) and built._table == "t1"
    # table optional -> default
    assert rf._build_store({"type": "postgres", "dsn": "postgresql://u@h/db"},
                           ".")._table == "yaah_state"
    # missing dsn fails at build time with the key named
    try:
        rf._build_store({"type": "postgres"}, ".")
    except ValueError as e:
        assert "dsn" in str(e), e
    else:
        raise AssertionError("postgres state without dsn must fail loud")

    # validate accepts the block; an unknown key is rejected with a suggestion
    validate_root({"state": {"type": "postgres", "dsn": "postgresql://u@h/db",
                             "table": "t"}})
    try:
        validate_root({"state": {"type": "postgres", "dsn": "x", "tabel": "t"}})
    except ValueError as e:
        assert "tabel" in str(e) and "table" in str(e), e
    else:
        raise AssertionError("validate must reject unknown state keys")


# --- tier parity (the spec = tests/test_store.py's memory/file scenarios) ----------

async def assert_tier_parity(s: Any) -> None:
    """The exact contract MemoryBackend/FileBackend satisfy, run against any
    backend. Used with the fake (deterministic) AND the real DSN (integration)."""
    # core: get/put/delete
    assert await s.get("a") is None
    await s.put("a", b"1")
    assert await s.get("a") == b"1"
    await s.delete("a")
    assert await s.get("a") is None
    await s.delete("a")                                     # delete absent: no-op
    # hostile bytes must round-trip untouched: NULs, high bytes, quote/escape chars
    evil = b"\x00\xff'\"\\%_\n" + bytes(range(32))
    await s.put("bin", evil)
    assert await s.get("bin") == evil
    await s.delete("bin")

    # scan: prefix view; literal '%' and '_' in keys must not wildcard
    await s.put("p:x", b"x")
    await s.put("p:y", b"y")
    await s.put("q:z", b"z")
    found = {k: v async for k, v in s.scan("p:")}
    assert found == {"p:x": b"x", "p:y": b"y"}, found
    await s.put("a%b:1", b"pc")
    await s.put("axb:2", b"no")
    only = {k async for k, _ in s.scan("a%b")}
    assert only == {"a%b:1"}, only
    everything = {k async for k, _ in s.scan("")}
    assert {"p:x", "p:y", "q:z", "a%b:1", "axb:2"} <= everything, everything
    # snapshot semantics: callers delete while iterating (the sweep pattern)
    async for k, _ in s.scan("p:"):
        await s.delete(k)
    assert {k async for k, _ in s.scan("p:")} == set()

    # get_rev: absent -> (None, None); put bumps rev by exactly 1
    assert await s.get_rev("nope") == (None, None)
    await s.put("r", b"v1")
    v, r1 = await s.get_rev("r")
    assert v == b"v1" and r1 is not None
    await s.put("r", b"v2")
    v, r2 = await s.get_rev("r")
    assert v == b"v2" and r2 == r1 + 1, (r1, r2)

    # cas: create-if-absent, then revisioned updates (memory-scenario parity)
    rev1 = await s.cas("c", b"v1", expected=None)
    assert rev1 is not None
    assert await s.cas("c", b"v2", expected=None) is None        # exists -> conflict
    assert await s.cas("c", b"v2", expected=rev1 + 99) is None   # wrong rev -> conflict
    rev2 = await s.cas("c", b"v2", expected=rev1)                # right rev -> ok
    assert rev2 is not None and await s.get("c") == b"v2"
    assert rev2 == rev1 + 1, (rev1, rev2)
    # cas against an ABSENT key with a concrete expected rev -> conflict
    assert await s.cas("ghost", b"x", expected=3) is None
    # delete resets the key's rev history: create-if-absent works again
    await s.delete("c")
    assert await s.cas("c", b"v3", expected=None) is not None
    # cas after plain puts uses the put-established rev
    await s.put("mix", b"m1")
    _, mrev = await s.get_rev("mix")
    assert await s.cas("mix", b"m2", expected=mrev) == mrev + 1


async def scenario_fake_tier_parity() -> None:
    fake = FakeConnection()
    s = PostgresBackend("postgresql://ignored", connection=fake)
    await assert_tier_parity(s)
    # the DDL ran exactly once, before the first data statement
    creates = [q for q, _ in fake.executed if q.startswith("CREATE TABLE")]
    assert len(creates) == 1 and fake.executed[0][0].startswith("CREATE TABLE")


async def scenario_fake_baton_store() -> None:
    """The facade over the fake-backed postgres substrate — proves the tier
    declarations satisfy BatonStore's up-front backend check and that a baton
    round-trips through BYTEA-shaped values."""
    bs = BatonStore(PostgresBackend("postgresql://ignored", connection=FakeConnection()))
    b = Baton(id="x", stage="s", status="suspended", parked_at=0.0, ttl=100.0,
              concerns=[{"code": "c"}], pending=Envelope("result", {"k": 1}))
    await bs.save(b)
    got = await bs.load("x")
    assert got is not None and got.stage == "s" and got.concerns == [{"code": "c"}]
    assert got.pending is not None and got.pending.payload == {"k": 1}
    assert [x.id for x in await bs.list_suspended()] == ["x"]
    assert await bs.sweep_expired(200.0) == ["x"]
    assert await bs.load("x") is None


async def scenario_close_semantics() -> None:
    # an injected connection is the CALLER's to close: our close() must not
    # touch it (FakeConnection has no close(), so a wrong call would explode),
    # and the backend stays usable afterwards.
    fake = FakeConnection()
    s = PostgresBackend("postgresql://ignored", connection=fake)
    await s.put("k", b"v")
    await s.close()
    assert await s.get("k") == b"v"
    # an owned-but-never-opened backend closes as a safe no-op
    await PostgresBackend("postgresql://u@h/db").close()


# --- the ONE integration path (real DSN, env-gated) --------------------------------

async def scenario_real_postgres(dsn: str) -> None:
    table = "yaah_it_" + uuid.uuid4().hex[:12]
    s = PostgresBackend(dsn, table=table)
    try:
        await assert_tier_parity(s)

        # concurrent CAS race: 8 writers vs expected=rev-of-first-put — exactly
        # one may win per round (the FileBackend flock scenario, DB-enforced).
        # Each racer gets its OWN backend instance (= its own connection): a
        # shared instance would serialize on its asyncio.Lock, and the race
        # would pass even against a database with broken CAS atomicity
        # (adversarial-eval finding) — the DATABASE must settle this, not the lock.
        await s.put("race", b"v0")
        _, base = await s.get_rev("race")
        racers = [PostgresBackend(dsn, table=table) for _ in range(8)]
        try:
            wins = [w for w in await asyncio.gather(
                        *[r.cas("race", "v{}".format(i).encode(), expected=base)
                          for i, r in enumerate(racers)])
                    if w is not None]
        finally:
            for r in racers:
                await r.close()
        assert len(wins) == 1 and wins[0] == base + 1, wins

        # BatonStore round-trip; a SECOND backend over the same table sees it
        # (the cross-process durable-state stand-in)
        await BatonStore(s).save(Baton(id="z", stage="g", status="suspended",
                                       parked_at=0.0, ttl=99.0, awaiting="human:g"))
        reopened = PostgresBackend(dsn, table=table)
        try:
            other = BatonStore(reopened)
            got = await other.load("z")
            assert got is not None and got.awaiting == "human:g", got
            assert [b.id for b in await other.list_suspended()] == ["z"]
        finally:
            await reopened.close()
    finally:
        conn = await s._conn()
        await conn.execute("DROP TABLE IF EXISTS {}".format(table))
        await s.close()
    print("ok (integration against {})".format(dsn.split("@")[-1]))


def main() -> None:
    test_construction_guards()
    test_sql_construction()
    test_like_prefix_escaping()
    test_lazy_import_error_is_actionable()
    test_declares_its_ports()
    test_config_type_registration()
    asyncio.run(scenario_fake_tier_parity())
    asyncio.run(scenario_fake_baton_store())
    asyncio.run(scenario_close_semantics())

    dsn = os.environ.get("YAAH_TEST_POSTGRES_DSN")
    if dsn:
        asyncio.run(scenario_real_postgres(dsn))
    else:
        print("skip: YAAH_TEST_POSTGRES_DSN not set — postgres integration path skipped")
    print("PASS")


if __name__ == "__main__":
    main()
