"""State-store backend lifecycle — the runtime RELEASES what it BUILDS.

The gap this pins: `runtime_factories._build_store` constructs a StoreBackend
from the root `state:` block, and a durable backend (PostgresBackend) holds a
real DB connection — but nothing on the runtime/CLI paths ever `close()`d the
backend it built, so a long-lived embedder leaked one connection per
`run_root`/`list_gates`/`resume_gate`. The experiment layer solved the identical
problem with an ownership-aware context manager (`experiment.store_factory.
opened_store`); this mirrors that stance for the runtime substrate:

  - a backend the runtime BUILT is closed on exit (normal, suspend, or error);
  - a backend INJECTED from outside is caller-owned and left untouched;
  - close() releases the CONNECTION only — durable state survives, so a parked
    baton is still resumable by a fresh backend instance (cross-process resume,
    docs/durable-state.md §5). close() is an OPTIONAL adapter capability probed
    with getattr, so MemoryBackend/FileBackend (no long-lived resource) need no
    no-op stub.

Run: cd yaah && PYTHONPATH=src python3 tests/test_store_lifecycle.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from yaah import runtime as r
from yaah import runtime_factories as rf
from yaah.harness import Done, Suspended
from yaah.store import CompareAndSet, MemoryBackend, Scannable, StoreBackend


# --- a durable-emulating probe backend -------------------------------------------
#
# Data lives in a shared MemoryBackend keyed by `store_id`, so it SURVIVES close()
# exactly the way Postgres rows survive a connection close — a fresh ProbeBackend
# over the same id sees the parked baton. close() records the call and drops the
# "connection" flag; the next op transparently "reconnects". Every built instance
# is recorded so a test can assert precisely who got closed.

_SHARED: Dict[str, MemoryBackend] = {}
_BUILT: List["ProbeBackend"] = []


class ProbeBackend(StoreBackend, Scannable, CompareAndSet):
    def __init__(self, store_id: str) -> None:
        self._id = store_id
        self._mem = _SHARED.setdefault(store_id, MemoryBackend())
        self.closed = 0
        self.connected = True
        _BUILT.append(self)

    def _use(self) -> MemoryBackend:
        self.connected = True   # lazy reconnect, like PostgresBackend._conn()
        return self._mem

    async def get(self, key: str) -> Optional[bytes]:
        return await self._use().get(key)

    async def put(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        await self._use().put(key, value, ttl=ttl)

    async def delete(self, key: str) -> None:
        await self._use().delete(key)

    async def scan(self, prefix: str) -> AsyncGenerator[Tuple[str, bytes], None]:
        async for item in self._use().scan(prefix):
            yield item

    async def get_rev(self, key: str) -> Tuple[Optional[bytes], Optional[int]]:
        return await self._use().get_rev(key)

    async def cas(self, key: str, value: bytes, *, expected: Optional[int],
                  ttl: Optional[float] = None) -> Optional[int]:
        return await self._use().cas(key, value, expected=expected, ttl=ttl)

    async def close(self) -> None:
        # release the "connection" only — the shared data (durable rows) stays.
        self.closed += 1
        self.connected = False


def _install_probe() -> None:
    rf._STATE_TYPES["probe"] = (
        lambda spec, base: ProbeBackend(spec.get("id", "default")),
        frozenset({"id"}))


def _uninstall_probe() -> None:
    rf._STATE_TYPES.pop("probe", None)
    _SHARED.clear()
    _BUILT.clear()


def _root(store_id: str, *, gated: bool, run: bool = True) -> dict:
    nodes = {"role:writer": {"type": "agent", "template": "spec for {{request}}",
                             "model": "fake:w", "stage": "writer", "parse": False}}
    stages = {"write": {"node": "role:writer"}}
    graph = {"start": "write", "stages": stages}
    if gated:
        nodes["role:gate"] = {"type": "human_gate", "ask": "ok?\n{{raw}}",
                              "awaiting": "spec:approve", "form": "approve_or_revise"}
        stages["write"] = {"node": "role:writer", "then": "gate"}
        stages["gate"] = {"node": "role:gate",
                          "branch": {"on": "decision", "routes": {"revise": "write"}}}
    return {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "a draft"}},
        "default_provider": "fake",
        "state": {"type": "probe", "id": store_id},
        "pipeline": {"nodes": nodes, "graph": graph},
        "input": {"request": "overdraft guard"},
        "run": run,
    }


# --- 1. run_root closes a backend it BUILT (normal + suspend + error exits) --------

def scenario_run_root_closes_built_backend() -> None:
    # normal completion -> Done, the one backend run_root built is closed once.
    before = len(_BUILT)
    out = asyncio.run(r.run_root(_root("done", gated=False), "."))
    assert isinstance(out, Done), out
    built = _BUILT[before:]
    assert len(built) == 1, built                    # exactly one backend per run
    assert built[0].closed == 1, ("normal exit must close", built[0].closed)

    # suspend exit (parked at a human gate) -> still closed.
    before = len(_BUILT)
    out = asyncio.run(r.run_root(_root("susp", gated=True), "."))
    assert isinstance(out, Suspended), out
    built = _BUILT[before:]
    assert len(built) == 1 and built[0].closed == 1, ("suspend exit must close", built)

    # error exit (assemble raises AFTER the store was built) -> still closed.
    before = len(_BUILT)
    boom = RuntimeError("assemble blew up")

    async def _raise(root, base, *, store=None):  # noqa: ANN001
        raise boom

    orig = r._assemble_harness
    r._assemble_harness = _raise
    try:
        asyncio.run(r.run_root(_root("err", gated=False), "."))
        raise AssertionError("run_root must propagate the assemble error")
    except RuntimeError as e:
        assert e is boom, e
    finally:
        r._assemble_harness = orig
    built = _BUILT[before:]
    assert len(built) == 1 and built[0].closed == 1, ("error exit must close", built)


# --- 2. an INJECTED backend is caller-owned (never closed) ------------------------

def scenario_injected_backend_not_closed() -> None:
    from yaah.runtime_factories import opened_store

    async def go() -> None:
        # injected -> yielded untouched, not closed (caller owns its lifetime).
        injected = ProbeBackend("injected")
        async with opened_store({"type": "probe", "id": "injected"}, ".",
                                backend=injected) as s:
            assert s is injected, s
        assert injected.closed == 0, ("injected must NOT be closed", injected.closed)

        # built -> closed on exit.
        before = len(_BUILT)
        async with opened_store({"type": "probe", "id": "built"}, ".") as s:
            assert isinstance(s, ProbeBackend)
        built = _BUILT[before:]
        assert len(built) == 1 and built[0].closed == 1, built

        # built + body raises -> STILL closed (finally fires on error).
        before = len(_BUILT)
        try:
            async with opened_store({"type": "probe", "id": "built2"}, ".") as s:
                raise ValueError("body boom")
        except ValueError:
            pass
        built = _BUILT[before:]
        assert len(built) == 1 and built[0].closed == 1, built

        # a backend WITHOUT close() (MemoryBackend) is tolerated by the getattr
        # probe — no crash, no stub required.
        async with opened_store({"type": "memory"}, ".") as s:
            assert isinstance(s, MemoryBackend)

    asyncio.run(go())


# --- 3. close() does NOT destroy durable state (park -> close -> reopen -> resume) --

def scenario_park_close_reopen_resume() -> None:
    root = _root("durable", gated=True)

    # run_root parks a baton, then closes the backend it built.
    out = asyncio.run(r.run_root(root, "."))
    assert isinstance(out, Suspended), out
    parker = _BUILT[-1]
    assert parker.closed == 1, ("parker must be closed", parker.closed)

    # list_gates builds a FRESH backend over the same durable store; the parked
    # baton is still visible (close dropped the connection, not the rows) — and
    # this backend is closed too.
    before = len(_BUILT)
    gates = asyncio.run(r.list_gates(root, "."))
    assert [b.id for b in gates] == [out.baton_id], gates
    lister = _BUILT[before]
    assert lister is not parker and lister.closed == 1, lister

    # baton_schema surfaces the form off the durable, reopened store.
    schema = asyncio.run(r.baton_schema(root, ".", out.baton_id))
    assert schema["form"] == "approve_or_revise", schema

    # resume_gate builds yet another fresh backend, loads the parked baton, and
    # drives to completion — proving the earlier close() left state intact.
    before = len(_BUILT)
    done = asyncio.run(r.resume_gate(root, ".", out.baton_id, {"decision": "approve"}))
    assert isinstance(done, Done), done
    resumer = _BUILT[before]
    assert resumer.closed == 1, ("resumer must be closed", resumer.closed)

    # the gate is gone from the durable store after completion.
    gates = asyncio.run(r.list_gates(root, "."))
    assert gates == [], gates


# --- 4. clear_state closes the backend it built -----------------------------------

def scenario_clear_state_closes() -> None:
    root = _root("clearme", gated=True)
    asyncio.run(r.run_root(root, "."))               # park one
    before = len(_BUILT)
    asyncio.run(r.clear_state(root, "."))
    assert _BUILT[before].closed == 1, ("clear_state must close", _BUILT[before].closed)


def main() -> None:
    _install_probe()
    try:
        scenario_run_root_closes_built_backend()
        scenario_injected_backend_not_closed()
        scenario_park_close_reopen_resume()
        scenario_clear_state_closes()
    finally:
        _uninstall_probe()
    print("PASS store lifecycle (build->close; injected untouched; durable survives close)")


if __name__ == "__main__":
    main()
