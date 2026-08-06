"""Fork / fan-in tests — true parallel branches with an optional join.

Run: cd yaah && PYTHONPATH=src python3 tests/test_fork_join.py

fork = a stage that spreads to successor STAGES (each an independent branch);
fanin = a stage that waits for its declared inputs, reduces them, and continues.
Branches that don't reach a fanin just run to their own ends. (`fanout` is the
OTHER parallel primitive — the one-stage role barrier; explicit keys since the
2026-06-11 split, no more target-sniffing.)
"""
from __future__ import annotations

import asyncio
import tempfile

from yaah import Done, Envelope, Graph, Harness, InProcessComms, NodeConfig, Stage
from yaah.core import Kind
from yaah.build import build, validate_pipeline
from yaah.harness.reduce import default_reduce
from yaah.store import EnvelopeStore
from yaah.adapters.stores import FileBackend


class Emit:
    """Records that it ran (into `sink`) and emits findings tagged with `tag`."""
    def __init__(self, tag, sink):
        self.tag, self.sink = tag, sink

    async def invoke(self, env, config):
        self.sink.append(self.tag)
        return env.reply_with(Kind.RESULT, {"findings": [{"id": self.tag}]})


class Capture:
    """Records each input payload it sees (the evaluator / continuation)."""
    def __init__(self, sink):
        self.sink = sink

    async def invoke(self, env, config):
        self.sink.append(dict(env.payload))
        return env.reply_with(Kind.RESULT, dict(env.payload))


class Reducer:
    """A `node:` reduce override — receives the {branch_id: payload} map."""
    async def invoke(self, env, config):
        return env.reply_with(Kind.RESULT, {"custom": True, "n": len(env.payload)})


def _abcd(fanin, *, extra_eval=None):
    """Build the a/b/c/d graph: spread→[a,b,c,d]; a,b→join; c,d end; join→evaluator."""
    ran, seen = [], []
    comms = InProcessComms()
    for t in ("a", "b", "c", "d"):
        comms.register("role:" + t, Emit(t.upper(), ran))
    comms.register("role:eval", Capture(seen))
    if extra_eval:
        comms.register("role:reducer", Reducer())
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b", "c", "d"], then=None),
        Stage("a", node="role:a", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("c", node="role:c", then=None),
        Stage("d", node="role:d", then=None),
        Stage("join", node="", fanin=fanin, then="evaluator"),
        Stage("evaluator", node="role:eval", then=None),
    )
    return comms, graph, ran, seen


async def scenario_abcd_example() -> None:
    comms, graph, ran, seen = _abcd({"expect": ["a", "b"], "wait": "all"})
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"task": "go"}))
    assert isinstance(out, Done), out
    assert set(ran) == {"A", "B", "C", "D"}, ran          # all four branches ran
    assert len(seen) == 1, seen                            # evaluator ran exactly once
    ids = sorted(f["id"] for f in seen[0]["findings"])
    assert ids == ["A", "B"], ids                          # a,b combined; c,d did not


async def scenario_wait_any() -> None:
    comms, graph, ran, seen = _abcd({"expect": ["a", "b"], "wait": "any"})
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"task": "go"}))
    assert isinstance(out, Done) and len(seen) == 1, seen  # fires once on first arrival
    assert len(seen[0]["findings"]) >= 1


async def scenario_wait_n_of() -> None:
    # 3 branches into the join, continue on the 2nd
    ran, seen = [], []
    comms = InProcessComms()
    for t in ("a", "b", "e"):
        comms.register("role:" + t, Emit(t.upper(), ran))
    comms.register("role:eval", Capture(seen))
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b", "e"], then=None),
        Stage("a", node="role:a", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("e", node="role:e", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b", "e"], "wait": 2}, then="evaluator"),
        Stage("evaluator", node="role:eval", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done) and len(seen) == 1, seen
    assert 2 <= len(seen[0]["findings"]) <= 3  # at least the 2 that triggered


async def scenario_timeout_to_listener() -> None:
    # join expects a,b but only a is forked → never met → timeout → error published
    ran, seen, errs = [], [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:c", Emit("C", ran))
    comms.register("role:eval", Capture(seen))

    async def on_err(env):
        errs.append(env)
    await comms.subscribe("join.errors", on_err)

    graph = Graph.of(
        Stage("spread", node="", fork=["a", "c"], then=None),
        Stage("a", node="role:a", then="join"),
        Stage("c", node="role:c", then=None),
        Stage("join", node="", then="evaluator",
              fanin={"expect": ["a", "b"], "wait": "all",
                     "timeout": 0.05, "on_timeout": "join.errors"}),
        Stage("evaluator", node="role:eval", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done), out
    assert len(seen) == 0, seen                 # never continued past the join
    assert len(errs) == 1, errs                 # one error reached the listener
    assert errs[0].kind == Kind.ERROR and errs[0].payload["reason"] == "timeout", errs[0].payload


async def scenario_reduce_override_node() -> None:
    comms, graph, ran, seen = _abcd(
        {"expect": ["a", "b"], "wait": "all", "reduce": "node:role:reducer"},
        extra_eval=True)
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done) and len(seen) == 1, seen
    assert seen[0] == {"custom": True, "n": 2}, seen[0]  # override shaped the join output


async def scenario_branch_never_rejoins() -> None:
    # a lone branch that ends without any fanin — the run still completes
    ran = []
    comms = InProcessComms()
    comms.register("role:c", Emit("C", ran))
    graph = Graph.of(
        Stage("spread", node="", fork=["c"], then=None),
        Stage("c", node="role:c", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done) and ran == ["C"], ran


async def scenario_build_classifies_fanout_as_fork() -> None:
    # the EXPLICIT `fork` key (2026-06-11 split — no more target-sniffing):
    # `fork` targets stages; `fanout` pointed at stages is REJECTED with a hint.
    cfg = {
        "nodes": {"role:x": {"type": "agent", "template": "t", "model": "fake:x"}},
        "graph": {"start": "spread", "stages": {
            "spread": {"fork": ["p", "q"], "then": None},
            "p": {"node": "role:x", "then": None},
            "q": {"node": "role:x", "then": None},
        }},
    }
    validate_pipeline(cfg)  # no error: spread has no node but is a fork; p,q are stages
    from yaah.agents import FakeProvider
    h = build(cfg, backend=FakeProvider(default="{}"))
    assert h.graph.stages["spread"].fork == ["p", "q"]
    assert h.graph.stages["spread"].fanout is None

    # the old sniffed form is now a loud config error with a migration hint
    bad = {"nodes": cfg["nodes"],
           "graph": {"start": "spread", "stages": {
               "spread": {"fanout": ["p", "q"], "then": None},
               "p": {"node": "role:x", "then": None},
               "q": {"node": "role:x", "then": None}}}}
    try:
        validate_pipeline(bad)
        raise AssertionError("expected ValueError for fanout pointing at stages")
    except ValueError as e:
        assert 'did you mean "fork"' in str(e), e


def scenario_default_reduce_unit() -> None:
    out = default_reduce({"a": {"findings": [1], "x": 1},
                          "b": {"findings": [2], "x": 2, "y": 3}})
    assert out == {"findings": [1, 2], "x": 2, "y": 3}, out  # lists concat, last-wins scalar


async def scenario_fork_waits_for_clear() -> None:
    # synchronized scatter-gather: the FORK has a `then`, the fan-in just reduces
    # (no `then`); the fork waits for the clear, then the forking flow resumes at
    # `summary` carrying the joined result.
    ran, seen = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:b", Emit("B", ran))
    comms.register("role:summary", Capture(seen))
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b"], then="summary"),
        Stage("a", node="role:a", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done) and len(seen) == 1, seen      # the flow resumed once
    assert sorted(f["id"] for f in seen[0]["findings"]) == ["A", "B"], seen[0]
    assert out.output.payload.get("findings") is not None       # Done carries the joined result


async def scenario_clear_from_anyone() -> None:
    # the clear is keyed by msg-id (correlation_id), NOT by who sends it: a fork with
    # no fan-in still proceeds when an EXTERNAL party publishes clear(x).
    seen = []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))
    comms.register("role:summary", Capture(seen))
    graph = Graph.of(
        # explicit configurable node id "gateA" — the gate's addressable name
        Stage("spread", id="gateA", node="", fork=["a"], then="summary"),
        Stage("a", node="role:a", then=None),   # branch ends; NO fan-in publishes a clear
        Stage("summary", node="role:summary", then=None),
    )

    async def external_clear():  # some other party, not a fan-in
        await asyncio.sleep(0.01)
        # gate address = "<node-id>:<correlation_id>" — here node-id "gateA" (configured)
        # + run "RID". Anyone who knows the gate + run can target it.
        await comms.publish("clear", Envelope(
            Kind.RESULT, {"from": "outside", "findings": [{"id": "X"}]},
            {"correlation_id": "RID", "clear_id": "gateA:RID"}))
    task = asyncio.ensure_future(external_clear())
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    await task
    assert isinstance(out, Done) and len(seen) == 1, seen
    assert seen[0].get("from") == "outside", seen[0]   # the external clear drove the continuation


async def scenario_clear_scopes() -> None:
    # a clear addressed to the NODE (any run — error/blanket clear) or "*" (flush all
    # waiting) releases the fork, not just the exact "<node>:<corr>" instance address.
    for cid in ("gateA", "*"):
        seen = []
        comms = InProcessComms()
        comms.register("role:a", Emit("A", []))
        comms.register("role:summary", Capture(seen))
        graph = Graph.of(
            Stage("spread", id="gateA", node="", fork=["a"], then="summary"),
            Stage("a", node="role:a", then=None),
            Stage("summary", node="role:summary", then=None),
        )

        async def clearer(c=cid):
            await asyncio.sleep(0.01)
            await comms.publish("clear", Envelope(Kind.RESULT, {"scope": c}, {"clear_id": c}))
        task = asyncio.ensure_future(clearer())
        out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
        await task
        assert isinstance(out, Done) and len(seen) == 1 and seen[0].get("scope") == cid, (cid, seen)


async def scenario_node_clears_gate() -> None:
    # the reusable clear: a NORMAL stage (not a fan-in) names a gate it clears on
    # completion via `clears`, releasing a waiting fork — no fan-in involved.
    seen = []
    comms = InProcessComms()
    comms.register("role:clearer", Emit("DONE", []))
    comms.register("role:summary", Capture(seen))
    graph = Graph.of(
        Stage("spread", id="gateX", node="", fork=["a"], then="summary"),
        Stage("a", node="role:clearer", clears=["gateX"], then=None),  # clears the fork's gate
        Stage("summary", node="role:summary", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}, {"correlation_id": "R"}))
    assert isinstance(out, Done) and len(seen) == 1, seen
    assert seen[0].get("findings") == [{"id": "DONE"}], seen[0]  # clear carried the clearer's output


async def scenario_fanin_parks_durably() -> None:
    # the SAME fan-in over a durable (FileBackend) EnvelopeStore: arrivals park to disk,
    # the join completes, and the parked set is flushed afterward.
    seen = []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))
    graph = Graph.of(
        Stage("spread", id="g", node="", fork=["a", "b"], then="summary"),
        Stage("a", node="role:a", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    with tempfile.TemporaryDirectory() as d:
        es = EnvelopeStore(FileBackend(d))
        out = await Harness(comms, graph, envelope_store=es).run(
            Envelope(Kind.TASK, {}, {"correlation_id": "R"}))
        assert isinstance(out, Done) and len(seen) == 1, seen
        assert sorted(f["id"] for f in seen[0]["findings"]) == ["A", "B"], seen[0]
        assert await es.list("") == [], "parked set should be flushed after the join"


async def scenario_fork_wait_timeout() -> None:
    # the fork waits for a fan-in that can never clear (expects a,b but only a is
    # forked). Since M9b the timed wait watches the branches: the moment every
    # branch has settled with the join provably unmeetable, the declared degrade
    # happens — the listener hears the REAL reason, without sitting out the TTL.
    ran, errs = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:summary", Capture([]))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", node="", fork=["a"], then="summary",
              wait={"timeout": 30.0, "on_timeout": "fork.timeout"}),  # long TTL: must not matter
        Stage("a", node="role:a", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    out = await asyncio.wait_for(
        Harness(comms, graph).run(Envelope(Kind.TASK, {})), timeout=5)
    assert isinstance(out, Done), out
    assert len(errs) == 1 and errs[0].payload["reason"] == "fanin_unmeetable", errs


class _BadNode:
    """Output never satisfies the hard validator — the branch hard-fails."""
    async def invoke(self, env, config):
        return env.reply_with(Kind.RESULT, {"ok": False})


class _HardCheck:
    async def invoke(self, env, config):
        from yaah import Failure, Verdict
        if env.payload.get("ok"):
            return Verdict.passed().to_envelope(env)
        return Verdict.failed(Failure("bad", "branch output not ok", "set ok")).to_envelope(env)


class _SoftCheck:
    async def invoke(self, env, config):
        from yaah import Failure, Verdict
        return Verdict.failed(Failure("nit", "minor", "consider"),
                              severity="soft").to_envelope(env)


async def scenario_branch_failure_fails_fork_instead_of_hanging() -> None:
    """H2 regression (assessment 2026-06-10): a branch whose stage hard-fails
    meant the fan-in policy could never be met, nobody published the clear, and
    the fork's unbounded `await fut` hung the run FOREVER with the StageFailed
    swallowed inside the branch task. Now the failure surfaces promptly."""
    from yaah import StageFailed
    ran = []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:bad", _BadNode())
    comms.register("role:check", _HardCheck())
    comms.register("role:summary", Capture([]))
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "bad"], then="summary"),  # NO wait.timeout
        Stage("a", node="role:a", then="join"),
        Stage("bad", node="role:bad", validators=["role:check"], max_attempts=1, then="join"),
        Stage("join", node="", fanin={"expect": ["a", "bad"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    try:  # the 5s bound is the test's hang detector — the old code never returned
        await asyncio.wait_for(
            Harness(comms, graph).run(Envelope(Kind.TASK, {})), timeout=5)
        raise AssertionError("a failed branch must fail the fork, not complete")
    except StageFailed as e:
        assert e.stage == "bad", e.stage
        assert "branch output not ok" in str(e), str(e)


async def scenario_dead_arm_under_timed_wait_degrades_immediately() -> None:
    """Mailbox M9b: with `wait.timeout` set, a dead arm used to leave the fork
    waiting out the FULL TTL (20 min of looks-hung in the incident) before
    degrading. The timed wait now watches the branches: once every arm has
    settled with the outcome determined (arm dead / join unmeetable), the
    DECLARED degrade happens immediately, and the on_timeout listener learns
    the real reason instead of a misleading 'wait_timeout'."""
    import time
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))
    comms.register("role:bad", _BadNode())
    comms.register("role:check", _HardCheck())
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", node="", fork=["a", "bad"], then="summary",
              wait={"timeout": 30.0, "on_timeout": "fork.timeout"}),  # long TTL
        Stage("a", node="role:a", then="join"),
        Stage("bad", node="role:bad", validators=["role:check"], max_attempts=1, then="join"),
        Stage("join", node="", fanin={"expect": ["a", "bad"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    t0 = time.monotonic()
    out = await asyncio.wait_for(
        Harness(comms, graph).run(Envelope(Kind.TASK, {"seed": 1})), timeout=5)
    assert time.monotonic() - t0 < 5, "must not sit out the 30s TTL"
    assert isinstance(out, Done), out
    assert len(errs) == 1 and errs[0].payload["reason"] == "branch_failed", errs
    assert "branch output not ok" in errs[0].payload.get("detail", ""), errs[0].payload
    assert seen and seen[0].get("seed") == 1, seen  # pre-fork payload survives
    # …and since the M33 review's HIGH-1 the healthy arm rides out with it: this
    # graph's arm `a` DID deposit at the join before `bad` killed the fork.
    assert seen[0].get("fork_partial", {}).get("arrived") == ["a"], seen[0]


async def scenario_timed_wait_happy_path_clears_normally() -> None:
    """The timed wait's HAPPY path (eval YELLOW on M9b): `wait.timeout` set AND
    the join met — the route every production fork with a fanin + TTL takes,
    rewritten by M9b from a bare wait_for(fut). The joined result must flow,
    fast, with no degrade and no on_timeout publish."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 30.0, "on_timeout": "fork.timeout"}),
        Stage("a", node="role:a", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    out = await asyncio.wait_for(
        Harness(comms, graph).run(Envelope(Kind.TASK, {})), timeout=5)
    assert isinstance(out, Done), out
    assert errs == [], errs                                # no degrade published
    assert len(seen) == 1, seen                            # flow resumed once, joined
    assert sorted(f["id"] for f in seen[0]["findings"]) == ["A", "B"], seen[0]


async def scenario_pure_liveness_ttl_still_fires() -> None:
    """The TTL keeps its liveness role: a CLEAN settle with no fan-in (the
    external-clear pattern) and no clearer waits out the full wait.timeout,
    then degrades with the plain 'wait_timeout' reason. With NOTHING parked at a
    fan-in the degrade output is the pre-fork input UNCHANGED (M33): no
    `fork_partial` key, so a degrade with no evidence stays loud downstream."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", node="", fork=["a"], then="summary",
              wait={"timeout": 0.2, "on_timeout": "fork.timeout"}),
        Stage("a", node="role:a", then=None),   # branch ends; NO fan-in, no clearer
        Stage("summary", node="role:summary", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"seed": 1}))
    assert isinstance(out, Done), out
    assert len(errs) == 1 and errs[0].payload["reason"] == "wait_timeout", errs
    assert len(seen) == 1 and "fork_partial" not in seen[0], seen


class _Slow:
    """A branch stage that outlives the fork's TTL — the starving arm."""
    def __init__(self, secs):
        self.secs = secs

    async def invoke(self, env, config):
        await asyncio.sleep(self.secs)
        return env.reply_with(Kind.RESULT, {"findings": [{"id": "SLOW"}]})


async def scenario_timeout_delivers_completed_arm() -> None:
    """M33 (live incident TASK-JK-260): one arm DEPOSITED at the fan-in and the
    other was still retrying when the fork's TTL fired. The degrade used to
    return the pre-fork input, so the completed arm's whole review — paid for,
    parked on disk — was discarded and the app saw an empty fork. Now the parked
    arrivals ride the output under `fork_partial` (and the parked set is
    flushed), so the app can degrade to single-arm evidence instead of zero."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:slow", _Slow(5.0))
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", id="g", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 0.3, "on_timeout": "fork.timeout"}),
        Stage("a", node="role:slow", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    with tempfile.TemporaryDirectory() as d:
        es = EnvelopeStore(FileBackend(d))
        out = await asyncio.wait_for(
            Harness(comms, graph, envelope_store=es).run(
                Envelope(Kind.TASK, {"seed": "S"}, {"correlation_id": "R"})), timeout=5)
        assert isinstance(out, Done), out
        assert len(errs) == 1 and errs[0].payload["reason"] == "wait_timeout", errs
        assert len(seen) == 1, seen
        part = seen[0].get("fork_partial")
        assert part is not None, ("the completed arm must reach the continuation", seen[0])
        assert part["arrived"] == ["b"], part
        assert part["expected"] == ["a", "b"] and part["missing"] == ["a"], part
        assert part["fork"] == "spread" and part["reason"] == "wait_timeout", part
        assert part["results"]["b"]["findings"] == [{"id": "B"}], part
        assert seen[0].get("seed") == "S", ("pre-fork payload must survive", seen[0])
        assert await es.list("") == [], "parked set must be released on the degrade"


async def scenario_branch_failed_delivers_completed_arm() -> None:
    """M33 review HIGH-1, route 2 of 3. M33 shipped the salvage as a promise over
    ALL THREE degrade reasons and delivered it on `wait_timeout` alone: on
    `branch_failed` the fan-in's own coordinator reaches its unmeetable exit
    FIRST (the watch loop sets the join's event, the coordinator wakes and
    FLUSHES) and only then does the fork degrade — so `_degraded_output` listed
    an already-empty store and threw the completed arm away exactly as before the
    fix. The route matters most in exactly the shape M33 was written for: an app
    whose arm RAISES (rather than starving) still lost its healthy sibling's
    whole output. The coordinator now snapshots before flushing."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:b", Emit("B", []))
    comms.register("role:bad", _BadNode())
    comms.register("role:check", _HardCheck())
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", id="g", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 30.0, "on_timeout": "fork.timeout"}),   # long TTL
        Stage("a", node="role:bad", validators=["role:check"],
              max_attempts=1, then="join"),                            # the dead arm
        Stage("b", node="role:b", then="join"),                        # completes + deposits
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    with tempfile.TemporaryDirectory() as d:
        es = EnvelopeStore(FileBackend(d))
        out = await asyncio.wait_for(
            Harness(comms, graph, envelope_store=es).run(
                Envelope(Kind.TASK, {"seed": "S"}, {"correlation_id": "R"})), timeout=5)
        assert isinstance(out, Done), out
        assert len(errs) == 1 and errs[0].payload["reason"] == "branch_failed", errs
        part = seen[0].get("fork_partial")
        assert part is not None, ("branch_failed must deliver the completed arm", seen[0])
        assert part["arrived"] == ["b"] and part["reason"] == "branch_failed", part
        assert part["expected"] == ["a", "b"] and part["missing"] == ["a"], part
        assert part["results"]["b"]["findings"] == [{"id": "B"}], part
        assert "branch output not ok" in part.get("detail", ""), part
        assert seen[0].get("seed") == "S", ("pre-fork payload must survive", seen[0])
        assert await es.list("") == [], "parked set must be released on the degrade"


async def scenario_fanin_unmeetable_delivers_completed_arm() -> None:
    """M33 review HIGH-1, route 3 of 3 — same lost race as `branch_failed`, no
    exception anywhere: arm `a` runs cleanly to its OWN terminal and never
    reaches the join, so once every branch has settled the policy is provably
    unmeetable (H2), the coordinator exits + flushes, and the fork degrades with
    `fanin_unmeetable`. Arm `b`'s deposited work must still ride out."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", id="g", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 30.0, "on_timeout": "fork.timeout"}),   # long TTL
        Stage("a", node="role:a", then=None),        # ends on its own — never joins
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    with tempfile.TemporaryDirectory() as d:
        es = EnvelopeStore(FileBackend(d))
        out = await asyncio.wait_for(
            Harness(comms, graph, envelope_store=es).run(
                Envelope(Kind.TASK, {"seed": "S"}, {"correlation_id": "R"})), timeout=5)
        assert isinstance(out, Done), out
        assert len(errs) == 1 and errs[0].payload["reason"] == "fanin_unmeetable", errs
        part = seen[0].get("fork_partial")
        assert part is not None, ("fanin_unmeetable must deliver the completed arm", seen[0])
        assert part["arrived"] == ["b"] and part["reason"] == "fanin_unmeetable", part
        assert part["expected"] == ["a", "b"] and part["missing"] == ["a"], part
        assert part["results"]["b"]["findings"] == [{"id": "B"}], part
        assert seen[0].get("seed") == "S", ("pre-fork payload must survive", seen[0])
        assert await es.list("") == [], "parked set must be released on the degrade"


async def scenario_fanin_own_timeout_still_salvages() -> None:
    """M33 review MED-3: a fan-in with its OWN `timeout` SHORTER than the fork's
    `wait.timeout` gives up first, publishes its join error and releases the
    parked set — and the fork, still holding TTL, then reached `_degraded_output`
    to find an empty store. Same snapshot-before-flush fixes it: the fan-in
    hands its arrivals to the fork's degrade instead of erasing them.

    ORDERING, stated: this is the fan-in's clock, not the fork's, so the reason
    the listener sees is still the fork's own `wait_timeout` — the fan-in gave up
    early, the fork ran out later."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:slow", _Slow(5.0))
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", id="g", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 0.6, "on_timeout": "fork.timeout"}),
        Stage("a", node="role:slow", then="join"),
        Stage("b", node="role:b", then="join"),
        # the JOIN gives up long before the fork does
        Stage("join", node="", then=None,
              fanin={"expect": ["a", "b"], "wait": "all", "timeout": 0.1}),
        Stage("summary", node="role:summary", then=None),
    )
    with tempfile.TemporaryDirectory() as d:
        es = EnvelopeStore(FileBackend(d))
        out = await asyncio.wait_for(
            Harness(comms, graph, envelope_store=es).run(
                Envelope(Kind.TASK, {"seed": "S"}, {"correlation_id": "R"})), timeout=5)
        assert isinstance(out, Done), out
        assert len(errs) == 1 and errs[0].payload["reason"] == "wait_timeout", errs
        part = seen[0].get("fork_partial")
        assert part is not None, ("an early fan-in must not erase the salvage", seen[0])
        assert part["arrived"] == ["b"], part
        assert part["results"]["b"]["findings"] == [{"id": "B"}], part
        assert await es.list("") == [], "parked set must be released on the degrade"


class _ListBrokenStore(EnvelopeStore):
    """An EnvelopeStore whose SCAN fails — a full disk, a swept file, a backend
    that lost its connection."""
    async def list(self, group: str = ""):
        raise OSError("store scan failed")


async def scenario_degrade_survives_a_broken_store() -> None:
    """M33 review MED-2: the salvage put STORE CALLS on a path whose old body was
    infallible (`cleared = input`). A store that cannot be listed must therefore
    cost the run its salvage and NOTHING more — the degrade completes, the
    continuation runs on the pre-fork payload with no `fork_partial`, and the
    fault is traced. A degrade that crashes is strictly worse than the bug it
    replaced."""
    errs, seen = [], []
    comms = InProcessComms()
    comms.register("role:slow", _Slow(5.0))
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", id="g", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 0.3, "on_timeout": "fork.timeout"}),
        Stage("a", node="role:slow", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    with tempfile.TemporaryDirectory() as d:
        es = _ListBrokenStore(FileBackend(d))
        out = await asyncio.wait_for(
            Harness(comms, graph, envelope_store=es).run(
                Envelope(Kind.TASK, {"seed": "S"}, {"correlation_id": "R"})), timeout=5)
        assert isinstance(out, Done), out
        assert len(errs) == 1 and errs[0].payload["reason"] == "wait_timeout", errs
        assert len(seen) == 1 and "fork_partial" not in seen[0], seen
        assert seen[0].get("seed") == "S", ("the old, infallible degrade", seen[0])


async def scenario_partial_names_arms_under_a_count_expect() -> None:
    """M33 review LOW-5: with a `{"count": n}` expect the engine named NO branch,
    so `expected`/`missing` came out empty and a listener could not say which arm
    was lost. A count EQUAL to the fork's width means "every arm", and branch ids
    ARE the fork's stage names, so naming them is reporting rather than inventing
    — a count SHORTER than the fork stays empty, since a partial policy does not
    say which arms it wanted."""
    async def _run(slow, fast, count):
        """One fork over `slow + fast` arms whose join wants `count` of them —
        chosen so the policy is never met and the TTL always degrades."""
        seen = []
        comms = InProcessComms()
        comms.register("role:slow", _Slow(5.0))
        comms.register("role:fast", Emit("F", []))
        comms.register("role:summary", Capture(seen))
        arms = list(slow) + list(fast)
        graph = Graph.of(
            Stage("spread", node="", fork=arms, then="summary",
                  wait={"timeout": 0.2}),
            *[Stage(n, node="role:slow" if n in slow else "role:fast", then="join")
              for n in arms],
            Stage("join", node="", then=None,
                  fanin={"expect": {"count": count}, "wait": "all"}),
            Stage("summary", node="role:summary", then=None),
        )
        out = await asyncio.wait_for(
            Harness(comms, graph).run(Envelope(Kind.TASK, {})), timeout=5)
        assert isinstance(out, Done), out
        return seen[0]["fork_partial"]

    # count == the fork's width: "every arm", so the missing one is nameable
    full = await _run(("a",), ("b",), 2)
    assert full["arrived"] == ["b"], full
    assert full["expected"] == ["a", "b"] and full["missing"] == ["a"], full
    # count SHORTER than the fork: which two of the three? the engine won't guess
    partial = await _run(("a", "b"), ("c",), 2)
    assert partial["arrived"] == ["c"], partial       # the salvage is unchanged
    assert partial["expected"] == [] and partial["missing"] == [], partial


async def scenario_terminal_fork_branch_failure_surfaces() -> None:
    """H2 terminal case: a TERMINAL fork (no `then`, no `wait`) used to let a
    branch's StageFailed escape the gather mid-spread; siblings + coordinators
    were abandoned un-drained. Now siblings finish, the drain settles, and the
    first branch failure is raised."""
    from yaah import StageFailed
    ran = []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:bad", _BadNode())
    comms.register("role:check", _HardCheck())
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "bad"], then=None),
        Stage("a", node="role:a", then=None),
        Stage("bad", node="role:bad", validators=["role:check"], max_attempts=1, then=None),
    )
    try:
        await asyncio.wait_for(
            Harness(comms, graph).run(Envelope(Kind.TASK, {})), timeout=5)
        raise AssertionError("a failed branch must fail the terminal fork")
    except StageFailed as e:
        assert e.stage == "bad", e.stage
    assert ran == ["A"], "the healthy sibling must still have run: " + repr(ran)


async def scenario_branch_soft_concerns_surface() -> None:
    """Theme B side-fix: branch stages' SOFT validator concerns used to be
    dropped on the floor inside the fork walker; now they flow into the baton
    like linear-path concerns and surface on the final output."""
    ran, seen = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:b", Emit("B", ran))
    comms.register("role:soft", _SoftCheck())
    comms.register("role:summary", Capture(seen))
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b"], then="summary"),
        Stage("a", node="role:a", validators=["role:soft"], max_attempts=1, then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done), out
    concerns = out.output.payload.get("concerns")
    assert concerns and concerns[0]["code"] == "nit", concerns
    assert concerns[0]["stage"] == "a", concerns


async def scenario_sticky_folds_inside_fork_branch() -> None:
    """Pre-existing harness gap (runtime-probed 2026-07): `graph.sticky` keys were
    re-folded after every LINEAR stage (harness._drive) and after a fork's reduced
    join, but NOT between chained stages INSIDE a fork branch (`_walk` never called
    `_fold_sticky`) — contradicting Graph.sticky's "after every passing stage". A
    payload-replacing stage in a branch silently dropped a sticky key a downstream
    branch stage needed: the SAME chain that works linearly lost data in a branch.
    The fix folds sticky in `_walk` exactly as `_drive` does, so a branch and the
    linear path agree. Asserted side-by-side to pin that equivalence."""
    # s1 REPLACES the payload (drops the sticky key k); s2 records what it sees.
    seen_linear, seen_fork = [], []
    lc = InProcessComms()
    lc.register("role:drop", Emit("DROP", []))          # reply drops k (findings-only)
    lc.register("role:cap", Capture(seen_linear))
    lgraph = Graph(stages={
        "s1": Stage("s1", node="role:drop", then="s2"),
        "s2": Stage("s2", node="role:cap", then=None),
    }, start="s1", sticky=["k"])
    lout = await Harness(lc, lgraph).run(Envelope(Kind.TASK, {"k": "V"}))
    assert isinstance(lout, Done) and seen_linear and seen_linear[0].get("k") == "V", \
        ("linear chain must re-fold sticky k", seen_linear)

    fc = InProcessComms()
    fc.register("role:drop", Emit("DROP", []))
    fc.register("role:cap", Capture(seen_fork))
    fgraph = Graph(stages={
        "spread": Stage("spread", node="", fork=["s1"], then=None),
        "s1": Stage("s1", node="role:drop", then="s2"),
        "s2": Stage("s2", node="role:cap", then=None),
    }, start="spread", sticky=["k"])
    fout = await Harness(fc, fgraph).run(Envelope(Kind.TASK, {"k": "V"}))
    assert isinstance(fout, Done), fout
    assert seen_fork and seen_fork[0].get("k") == "V", (
        "sticky key k must re-fold BETWEEN branch stages like the linear chain "
        "(was dropped by the payload-replacing s1): " + repr(seen_fork))


async def scenario_sticky_fill_if_missing_inside_branch() -> None:
    """The re-fold is FILL-IF-MISSING inside a branch too (never CLOBBER a fresher
    same-key value), matching _drive/_fold_sticky. A branch stage that deliberately
    SETS the sticky key must win over the folded original."""
    seen = []
    comms = InProcessComms()

    class _SetK:
        async def invoke(self, env, config):
            return env.reply_with(Kind.RESULT, {"k": "FRESH"})  # sets k itself
    comms.register("role:setk", _SetK())
    comms.register("role:cap", Capture(seen))
    graph = Graph(stages={
        "spread": Stage("spread", node="", fork=["s1"], then=None),
        "s1": Stage("s1", node="role:setk", then="s2"),
        "s2": Stage("s2", node="role:cap", then=None),
    }, start="spread", sticky=["k"])
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"k": "ORIG"}))
    assert isinstance(out, Done) and seen and seen[0].get("k") == "FRESH", \
        ("stage-set sticky value must win over the folded original", seen)


async def scenario_sticky_reduce_sees_folded_branch_values() -> None:
    """Edge: sticky folded inside branches must reach the fan-in reduce. Each branch
    drops k at s1 (re-folded), then arrives at the fanin; the default reduce unions
    the arrivals, so the joined payload carries k downstream to `summary`."""
    seen = []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", []))   # both branch heads drop k
    comms.register("role:b", Emit("B", []))
    comms.register("role:summary", Capture(seen))
    graph = Graph(stages={
        "spread": Stage("spread", node="", fork=["a", "b"], then="summary"),
        "a": Stage("a", node="role:a", then="join"),
        "b": Stage("b", node="role:b", then="join"),
        "join": Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        "summary": Stage("summary", node="role:summary", then=None),
    }, start="spread", sticky=["k"])
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"k": "V"}))
    assert isinstance(out, Done) and len(seen) == 1, seen
    assert seen[0].get("k") == "V", (
        "fan-in reduce must see the sticky key re-folded inside the branches", seen[0])


async def main() -> None:
    await scenario_abcd_example()
    await scenario_wait_any()
    await scenario_wait_n_of()
    await scenario_timeout_to_listener()
    await scenario_reduce_override_node()
    await scenario_branch_never_rejoins()
    await scenario_fork_waits_for_clear()
    await scenario_clear_from_anyone()
    await scenario_clear_scopes()
    await scenario_node_clears_gate()
    await scenario_fanin_parks_durably()
    await scenario_fork_wait_timeout()
    await scenario_dead_arm_under_timed_wait_degrades_immediately()
    await scenario_timed_wait_happy_path_clears_normally()
    await scenario_pure_liveness_ttl_still_fires()
    await scenario_timeout_delivers_completed_arm()
    await scenario_branch_failed_delivers_completed_arm()
    await scenario_fanin_unmeetable_delivers_completed_arm()
    await scenario_fanin_own_timeout_still_salvages()
    await scenario_degrade_survives_a_broken_store()
    await scenario_partial_names_arms_under_a_count_expect()
    await scenario_branch_failure_fails_fork_instead_of_hanging()
    await scenario_terminal_fork_branch_failure_surfaces()
    await scenario_branch_soft_concerns_surface()
    await scenario_sticky_folds_inside_fork_branch()
    await scenario_sticky_fill_if_missing_inside_branch()
    await scenario_sticky_reduce_sees_folded_branch_values()
    await scenario_build_classifies_fanout_as_fork()
    scenario_default_reduce_unit()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
