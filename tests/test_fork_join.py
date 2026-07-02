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
    # forked); the fork's own timeout fires, publishes to the listener, and proceeds.
    ran, errs = [], []
    comms = InProcessComms()
    comms.register("role:a", Emit("A", ran))
    comms.register("role:summary", Capture([]))

    async def on_to(env):
        errs.append(env)
    await comms.subscribe("fork.timeout", on_to)

    graph = Graph.of(
        Stage("spread", node="", fork=["a"], then="summary",
              wait={"timeout": 0.05, "on_timeout": "fork.timeout"}),
        Stage("a", node="role:a", then="join"),
        Stage("join", node="", fanin={"expect": ["a", "b"], "wait": "all"}, then=None),
        Stage("summary", node="role:summary", then=None),
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done), out
    assert len(errs) == 1 and errs[0].payload["reason"] == "wait_timeout", errs


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
    await scenario_branch_failure_fails_fork_instead_of_hanging()
    await scenario_terminal_fork_branch_failure_surfaces()
    await scenario_branch_soft_concerns_surface()
    await scenario_build_classifies_fanout_as_fork()
    scenario_default_reduce_unit()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
