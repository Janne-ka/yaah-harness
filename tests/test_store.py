"""Durable state Phase 1: the StoreBackend substrate + BatonStore + IdempotencyStore.

Proves the memory extender's tiers (core/scan/cas), baton (de)serialization +
sweep + the mailbox view, execute-once via OnceNode, and the headline payoff —
suspend a run on one Harness and resume it on a DIFFERENT Harness sharing the
store (the cross-process resume primitive, here cross-instance in one process).

Run: cd yaah && PYTHONPATH=src python3 tests/test_store.py
"""
from __future__ import annotations

import asyncio
import tempfile

from yaah import (
    Done,
    Envelope,
    Failure,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    Suspended,
    Verdict,
)
from yaah.harness import BatonStore
from yaah.harness.baton import Baton
from yaah.nodes import OnceNode
from yaah.store import (
    CompareAndSet,
    EnvelopeStore,
    IdempotencyStore,
    MemoryBackend,
    Scannable,
    StoreBackedFacade,
    StoreBackend,
)
from yaah.adapters.stores import FileBackend


def test_backends_declare_tiers_and_facades_declare_the_base() -> None:
    # Declaration is checked via __mro__ (real inheritance), NOT isinstance —
    # these are @runtime_checkable Protocols, so isinstance is structural and
    # would pass for a class that only conforms by shape (vacuous for "declares").
    for be in (MemoryBackend, FileBackend):
        for tier in (StoreBackend, Scannable, CompareAndSet):
            assert tier in be.__mro__, "{} must declare {}".format(be.__name__, tier.__name__)
    for facade in (EnvelopeStore, IdempotencyStore, BatonStore):
        assert StoreBackedFacade in facade.__mro__, facade.__name__
    # Enforcement: a declared-but-incomplete backend can't instantiate.
    try:
        class HalfBackend(StoreBackend):  # missing put/delete
            async def get(self, key): ...
        HalfBackend()
    except TypeError as e:
        assert "put" in str(e) or "delete" in str(e), e
    else:
        raise AssertionError("an incomplete StoreBackend subclass must not instantiate")


def test_facade_rejects_backend_missing_its_tier_at_construction() -> None:
    # "Validated up front" (store.py docstring): a facade whose verbs need +SCAN
    # must reject a core-only backend AT CONSTRUCTION — not AttributeError deep
    # in a baton sweep. This is the extender path the tiers exist for (e.g. a
    # blob store with no scan).
    class CoreOnly:  # structural core tier: get/put/delete, no scan
        async def get(self, key): return None
        async def put(self, key, value, *, ttl=None): pass
        async def delete(self, key): pass

    try:
        EnvelopeStore(CoreOnly())
    except TypeError as e:
        assert "ScannableStore" in str(e) and "CoreOnly" in str(e), e
    else:
        raise AssertionError("EnvelopeStore must reject a scan-less backend up front")
    try:
        BatonStore(CoreOnly())
    except TypeError:
        pass
    else:
        raise AssertionError("BatonStore must reject a scan-less backend up front")
    # IdempotencyStore needs only the core tier — a core-only backend is FINE there.
    IdempotencyStore(CoreOnly())


async def scenario_memory_store() -> None:
    s = MemoryBackend()
    assert await s.get("a") is None
    await s.put("a", b"1")
    assert await s.get("a") == b"1"

    await s.put("p:x", b"x")
    await s.put("p:y", b"y")
    found = {k: v async for k, v in s.scan("p:")}
    assert found == {"p:x": b"x", "p:y": b"y"}, found

    await s.delete("a")
    assert await s.get("a") is None

    # compare-and-set: create-if-absent, then revisioned updates
    rev1 = await s.cas("c", b"v1", expected=None)
    assert rev1 is not None
    assert await s.cas("c", b"v2", expected=None) is None        # already exists -> conflict
    assert await s.cas("c", b"v2", expected=rev1 + 99) is None   # wrong rev -> conflict
    rev2 = await s.cas("c", b"v2", expected=rev1)                # right rev -> ok
    assert rev2 is not None and await s.get("c") == b"v2"


async def scenario_baton_store() -> None:
    bs = BatonStore(MemoryBackend())
    b = Baton(id="x", stage="s", status="suspended", parked_at=0.0, ttl=100.0,
              concerns=[{"code": "c"}], pending=Envelope("result", {"k": 1}))
    await bs.save(b)

    got = await bs.load("x")
    assert got is not None and got.stage == "s" and got.concerns == [{"code": "c"}]
    assert got.pending is not None and got.pending.payload == {"k": 1}, "pending must round-trip"

    assert [x.id for x in await bs.list_suspended()] == ["x"]

    assert await bs.sweep_expired(50.0) == []          # parked_at 0 + ttl 100, now 50 -> alive
    assert await bs.sweep_expired(200.0) == ["x"]      # now 200 > 100 -> swept
    assert await bs.load("x") is None

    # a running baton is not part of the suspended (mailbox) view
    await bs.save(Baton(id="r", stage="s", status="running"))
    assert await bs.list_suspended() == []


async def scenario_file_store() -> None:
    """The durable FileBackend extender: same tiers as memory, but persisted to disk
    so a SECOND store over the same dir (a fresh process) sees what the first wrote."""
    with tempfile.TemporaryDirectory() as d:
        s = FileBackend(d)
        await s.put("k", b"v")
        assert await s.get("k") == b"v"
        # keys with ':' and '/' survive the on-disk encoding
        await s.put("kv:a/b", b"x")
        found = {k: v async for k, v in s.scan("kv:")}
        assert found == {"kv:a/b": b"x"}, found
        rev = await s.cas("c", b"1", expected=None)
        assert rev is not None and await s.cas("c", b"2", expected=None) is None

        # assessment #13: put is a rev-bumping read-modify-write under the same
        # per-key flock as cas — N concurrent puts (each on its own thread via
        # to_thread) must yield rev == N + prior, no lost bump.
        import asyncio
        before = (await s.get_rev("k"))[1]
        await asyncio.gather(*(s.put("k", b"p%d" % i) for i in range(20)))
        after = (await s.get_rev("k"))[1]
        assert after == before + 20, (before, after)

        # durability: a baton saved via one BatonStore is visible via another over
        # the SAME dir (stand-in for a different process)
        await BatonStore(s).save(Baton(id="z", stage="g", status="suspended",
                                       parked_at=0.0, ttl=99.0, awaiting="human:g"))
        reopened = BatonStore(FileBackend(d))
        got = await reopened.load("z")
        assert got is not None and got.awaiting == "human:g", got
        assert [b.id for b in await reopened.list_suspended()] == ["z"]


class Counter:
    """Counts how many times its effect actually ran."""
    def __init__(self) -> None:
        self.n = 0

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.n += 1
        return input.reply("result", n=self.n)


async def scenario_idempotency_once() -> None:
    inner = Counter()
    once = OnceNode(inner, IdempotencyStore(MemoryBackend()))
    env = Envelope("task", {})

    # same key twice -> inner runs ONCE, second call returns the cached output
    o1 = await once.invoke(env, NodeConfig(idempotency_key="k1"))
    o2 = await once.invoke(env, NodeConfig(idempotency_key="k1"))
    assert inner.n == 1, inner.n
    assert o1.payload["n"] == 1 and o2.payload["n"] == 1, (o1.payload, o2.payload)

    # a different key runs again
    await once.invoke(env, NodeConfig(idempotency_key="k2"))
    assert inner.n == 2, inner.n

    # the key can also come from the envelope header
    keyed = Envelope("task", {}, headers={"idempotency_key": "k1"})
    o3 = await once.invoke(keyed, NodeConfig())
    assert inner.n == 2 and o3.payload["n"] == 1, "header key hits the same cache"

    # no key at all -> not guarded, runs every time
    await once.invoke(env, NodeConfig())
    await once.invoke(env, NodeConfig())
    assert inner.n == 4, inner.n


class Stubborn:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text="nope", ok=False)


class OkValidator:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("ok"):
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(Failure("not_ok", "needs ok", "set ok")).to_envelope(input)


async def scenario_cross_instance_resume() -> None:
    """Suspend on harness A, resume on harness B — they share only the store."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    graph = Graph.of(Stage("g", node="role:stubborn", validators=["role:check"],
                           max_attempts=1, escalate="human"))
    shared = BatonStore(MemoryBackend())

    a = Harness(comms, graph, baton_store=shared)
    susp = await a.run(Envelope("task", {}))
    assert isinstance(susp, Suspended), susp

    # a brand-new harness instance (no shared memory but the store) resumes it
    b = Harness(comms, graph, baton_store=shared)
    final = await b.resume(susp.baton_id, Envelope("result", {"text": "approved", "ok": True}))
    assert isinstance(final, Done), final
    assert final.output.payload["text"] == "approved", final.output
    assert await shared.list_suspended() == [], "resumed run is evicted from the shared store"


async def scenario_envelope_store() -> None:
    """The gate-parking utility: save/load/delete/list Envelopes over a StoreBackend. The
    SAME facade works over MemoryBackend (here) or FileBackend/db (swap the backend)."""
    es = EnvelopeStore(MemoryBackend())
    assert await es.load("g:1") is None                       # nothing parked yet
    await es.save("g:1", Envelope("task", {"n": 1}, {"correlation_id": "C"}))
    await es.save("g:2", Envelope("result", {"n": 2}))
    back = await es.load("g:1")
    assert back is not None and back.kind == "task" and back.payload == {"n": 1}
    assert back.correlation_id == "C"                         # headers round-trip
    listed = dict(await es.list("g:"))                        # scan/mailbox view
    assert set(listed) == {"g:1", "g:2"}, listed
    await es.delete("g:1")
    assert await es.load("g:1") is None and len(await es.list("g:")) == 1
    # flush the parked set (the parked-side of a flush clear / error recovery)
    await es.save("g:3", Envelope("task", {"n": 3}))
    n = await es.flush("g:")
    assert n == 2 and await es.list("g:") == []

    # durable backend, same facade: park on one FileBackend, reload on another
    with tempfile.TemporaryDirectory() as d:
        await EnvelopeStore(FileBackend(d)).save("p", Envelope("task", {"x": 9}))
        again = await EnvelopeStore(FileBackend(d)).load("p")
        assert again is not None and again.payload == {"x": 9}, again


class FailingInner:
    """Returns a failure-verdict envelope (the side-effect failed in-band)."""
    def __init__(self) -> None:
        self.n = 0

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        self.n += 1
        return Verdict.failed(Failure("inner_fail", "boom", "retry")).to_envelope(input)


async def scenario_idempotency_does_not_cache_failures() -> None:
    # assessment cluster 2 B2: a returned failure used to be CACHED FOREVER,
    # so a transient error became permanent. Now: only committed successes are
    # cached; a failure return lets the next attempt retry.
    inner = FailingInner()
    once = OnceNode(inner, IdempotencyStore(MemoryBackend()))
    env = Envelope("task", {})
    await once.invoke(env, NodeConfig(idempotency_key="kf"))
    await once.invoke(env, NodeConfig(idempotency_key="kf"))
    assert inner.n == 2, "failure must NOT be cached — both attempts run"


async def scenario_filestore_cas_under_concurrent_writers() -> None:
    # assessment cluster 2 B3: the old read-then-write was non-atomic —
    # two concurrent writers reading rev=N both produced rev=N+1 and lost
    # one update. Under flock, exactly ONE writer wins per round.
    import tempfile

    from yaah.adapters.stores import FileBackend

    with tempfile.TemporaryDirectory() as d:
        store = FileBackend(d)
        await store.put("k", b"v0")                                    # rev=1 established

        async def writer(i: int) -> int:
            r = await store.cas("k", "v{}".format(i).encode(), expected=1)
            return -1 if r is None else r

        # 8 racing CAS attempts vs expected=1: exactly ONE may win.
        wins = await asyncio.gather(*[writer(i) for i in range(8)])
        winners = [w for w in wins if w != -1]
        assert len(winners) == 1, wins                                 # exactly one
        assert winners[0] == 2                                          # rev advances by 1


async def scenario_idempotency_finalize_uses_cas_when_available() -> None:
    # assessment cluster 2 B4: finalize used `put` so concurrent first-runs
    # both wrote and both executed the side effect. With cas, only the first
    # winner commits.
    import tempfile

    from yaah.adapters.stores import FileBackend

    with tempfile.TemporaryDirectory() as d:
        store = FileBackend(d)
        idem = IdempotencyStore(store)
        await idem.finalize("k", {"first": True})                      # first writer
        await idem.finalize("k", {"first": False})                     # second is a no-op
        hit = await idem.lookup("k")
        assert hit == {"first": True}, hit


async def main() -> None:
    test_backends_declare_tiers_and_facades_declare_the_base()
    await scenario_memory_store()
    await scenario_file_store()
    await scenario_baton_store()
    await scenario_idempotency_once()
    await scenario_idempotency_does_not_cache_failures()
    await scenario_idempotency_finalize_uses_cas_when_available()
    await scenario_filestore_cas_under_concurrent_writers()
    await scenario_cross_instance_resume()
    await scenario_envelope_store()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
