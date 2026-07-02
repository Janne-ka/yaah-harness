"""Error-handling tests — the per-node recovery alias (`Stage.on_error`) run on
TERMINAL failure, plus the `*`-flush of the durable parked set (Harness.flush).

`on_error` resolves to one of two recoveries (composing the dumb primitives):
  - "clear"            → reversible node: publish a clear for it + drop its parked set
  - {"compensate": T}  → side-effecting node: run the undo target T, then drop parked
Recovery runs, then the failure still propagates (StageFailed) — cleanup does not
paper over the error.

Run: cd yaah && PYTHONPATH=src python3 tests/test_error_handling.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage, StageFailed
from yaah.core import Failure, Kind, NodeConfig, Verdict
from yaah.build import build
from yaah.store import EnvelopeStore, MemoryBackend

# module-level sink so an `fn:` compensation target (imported by call_target) can record
COMPENSATED = []


def compensate_fn(ctx):
    """An `fn:` compensation target — records the recovery context it received."""
    COMPENSATED.append(ctx)
    return {"compensated": True}


def failing_compensate_fn(ctx):
    """A compensation target whose own undo FAILS (rollback target down)."""
    COMPENSATED.append(ctx)
    raise RuntimeError("rollback API unreachable")


class Writer:
    async def invoke(self, env, config):
        return env.reply("result", text="nope", ok=False)


class FailValidator:
    async def invoke(self, env, config):
        return Verdict.failed(Failure("not_ok", "needs ok", "set ok")).to_envelope()


class UndoNode:
    """A `node:` compensation target — records that it ran the undo."""
    def __init__(self, sink):
        self.sink = sink

    async def invoke(self, env, config):
        self.sink.append(dict(env.payload))
        return env.reply_with(Kind.RESULT, {"undone": True})


class RaisingUndoNode:
    """A `node:` compensation target whose undo RAISES (rollback service down).
    InProcessComms.request awaits invoke() directly, so the raise propagates out
    of call_target — the same failure signal as an fn: target that raises."""
    async def invoke(self, env, config):
        raise RuntimeError("undo node blew up")


def _failing_graph(on_error):
    return Graph.of(
        Stage("work", id="agent1", node="role:writer", validators=["role:check"],
              max_attempts=1, on_error=on_error))


async def scenario_on_error_clear_publishes_and_flushes() -> None:
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    clears = []

    async def on_clear(env):
        clears.append(env.headers.get("clear_id"))
    await comms.subscribe("clear", on_clear)

    es = EnvelopeStore(MemoryBackend())
    # something parked at this node's address that the recovery should drop
    await es.save("agent1:RID:branch", Envelope(Kind.RESULT, {"stale": True}))

    h = Harness(comms, _failing_graph("clear"), envelope_store=es)
    raised = False
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed:
        raised = True
    await asyncio.sleep(0)  # let the published clear be delivered

    assert raised, "the failure must still propagate after recovery"
    assert "agent1:RID" in clears, clears                 # clear published for the node
    assert await es.list("agent1:RID:") == [], "parked set dropped"


async def scenario_on_error_compensate_fn() -> None:
    import importlib
    # call_target imports the module by name; when this file runs as __main__ that is a
    # SECOND module object, so the fn records into ITS COMPENSATED — assert on that one.
    mod = importlib.import_module("test_error_handling")
    mod.COMPENSATED.clear()
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    h = Harness(comms, _failing_graph({"compensate": "fn:test_error_handling:compensate_fn"}))
    raised = False
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed:
        raised = True
    assert raised, "failure still propagates"
    assert len(mod.COMPENSATED) == 1, mod.COMPENSATED
    ctx = mod.COMPENSATED[0]
    assert ctx["correlation_id"] == "RID" and ctx["node"] == "agent1", ctx
    assert ctx["error"] == ["not_ok"], ctx                # the failed verdict's codes


async def scenario_on_error_compensate_node() -> None:
    undone = []
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    comms.register("role:undo", UndoNode(undone))
    h = Harness(comms, _failing_graph({"compensate": "node:role:undo"}))
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed:
        pass
    assert len(undone) == 1 and undone[0]["node"] == "agent1", undone


async def scenario_compensate_failure_default_escalates() -> None:
    # When the undo ITSELF fails and on_compensate_fail is unset,
    # the default is "error" — escalate, carrying the ORIGINAL failure PLUS a
    # compensation_failed one (masks nothing), and the parked set still flushes.
    import importlib
    mod = importlib.import_module("test_error_handling")
    mod.COMPENSATED.clear()
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    es = EnvelopeStore(MemoryBackend())
    await es.save("agent1:RID:branch", Envelope(Kind.RESULT, {"stale": True}))
    h = Harness(comms, _failing_graph(
        {"compensate": "fn:test_error_handling:failing_compensate_fn"}), envelope_store=es)
    caught = None
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed as e:
        caught = e
    assert caught is not None, "compensation failure must escalate by default"
    codes = [f.code for f in caught.verdict.failures]
    assert "compensation_failed" in codes, codes         # the undo-failure surfaces
    assert "not_ok" in codes, codes                       # AND the original is preserved
    assert len(mod.COMPENSATED) == 1, "the undo was attempted"
    assert await es.list("agent1:RID:") == [], "parked set still flushed"


async def scenario_compensate_failure_warn_tolerates() -> None:
    # on_compensate_fail="warn": a failed undo is NOTED, and the
    # ORIGINAL StageFailed surfaces (not a compensation_failed one); flush runs.
    import importlib
    mod = importlib.import_module("test_error_handling")
    mod.COMPENSATED.clear()
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    es = EnvelopeStore(MemoryBackend())
    await es.save("agent1:RID:branch", Envelope(Kind.RESULT, {"stale": True}))
    h = Harness(comms, _failing_graph(
        {"compensate": "fn:test_error_handling:failing_compensate_fn",
         "on_compensate_fail": "warn"}), envelope_store=es)
    caught = None
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed as e:
        caught = e
    assert caught is not None, "the original failure still propagates"
    codes = [f.code for f in caught.verdict.failures]
    assert codes == ["not_ok"], codes                     # ONLY the original, not compensation_failed
    assert len(mod.COMPENSATED) == 1, "the undo was attempted"
    assert await es.list("agent1:RID:") == [], "parked set still flushed"


async def scenario_compensate_node_failure_escalates() -> None:
    # node: path — a node: undo target that RAISES is caught the same
    # as an fn: one (comms.request awaits invoke directly), and escalates by default.
    # (A node target that instead RETURNS an error envelope does not raise, so it is
    # NOT detectable as a compensation failure — there is no engine contract for a
    # result payload meaning "undo failed"; only a raise is a uniform failure signal.)
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    comms.register("role:undo", RaisingUndoNode())
    h = Harness(comms, _failing_graph({"compensate": "node:role:undo"}))
    caught = None
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed as e:
        caught = e
    assert caught is not None, "a raising node: undo escalates by default"
    codes = [f.code for f in caught.verdict.failures]
    assert "compensation_failed" in codes and "not_ok" in codes, codes


async def scenario_no_on_error_fails_through() -> None:
    comms = InProcessComms()
    comms.register("role:writer", Writer())
    comms.register("role:check", FailValidator())
    h = Harness(comms, _failing_graph(None))  # no recovery configured
    raised = False
    try:
        await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    except StageFailed:
        raised = True
    assert raised, "no on_error → straight-through failure (today's behaviour)"


async def scenario_flush_drops_parked_set() -> None:
    es = EnvelopeStore(MemoryBackend())
    await es.save("g1:R:a", Envelope(Kind.RESULT, {"n": 1}))
    await es.save("g1:R:b", Envelope(Kind.RESULT, {"n": 2}))
    await es.save("g2:R:a", Envelope(Kind.RESULT, {"n": 3}))
    h = Harness(InProcessComms(), Graph.of(Stage("x", node="role:x")), envelope_store=es)

    n = await h.flush("g1:R:")                 # group-scoped flush
    assert n == 2, n
    assert len(await es.list("")) == 1, "only g1 dropped"

    n = await h.flush()                         # the `*` flush: everything
    assert n == 1 and await es.list("") == [], "parked set empty after full flush"


def scenario_build_parses_on_error() -> None:
    from yaah.agents import FakeProvider
    cfg = {
        "nodes": {"role:x": {"type": "agent", "template": "t", "model": "fake:x"}},
        "graph": {"start": "work", "stages": {
            "work": {"node": "role:x", "on_error": {"compensate": "fn:m:f"}, "then": None},
        }},
    }
    h = build(cfg, backend=FakeProvider(default="{}"))
    assert h.graph.stages["work"].on_error == {"compensate": "fn:m:f"}


async def main() -> None:
    await scenario_on_error_clear_publishes_and_flushes()
    await scenario_on_error_compensate_fn()
    await scenario_on_error_compensate_node()
    await scenario_compensate_failure_default_escalates()
    await scenario_compensate_failure_warn_tolerates()
    await scenario_compensate_node_failure_escalates()
    await scenario_no_on_error_fails_through()
    await scenario_flush_drops_parked_set()
    scenario_build_parses_on_error()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
