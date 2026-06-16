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
from yaah.store import EnvelopeStore, MemoryStore

# module-level sink so an `fn:` compensation target (imported by call_target) can record
COMPENSATED = []


def compensate_fn(ctx):
    """An `fn:` compensation target — records the recovery context it received."""
    COMPENSATED.append(ctx)
    return {"compensated": True}


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

    es = EnvelopeStore(MemoryStore())
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
    es = EnvelopeStore(MemoryStore())
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
    from yaah.agents import FakeBackend
    cfg = {
        "nodes": {"role:x": {"type": "agent", "template": "t", "model": "fake:x"}},
        "graph": {"start": "work", "stages": {
            "work": {"node": "role:x", "on_error": {"compensate": "fn:m:f"}, "then": None},
        }},
    }
    h = build(cfg, backend=FakeBackend(default="{}"))
    assert h.graph.stages["work"].on_error == {"compensate": "fn:m:f"}


async def main() -> None:
    await scenario_on_error_clear_publishes_and_flushes()
    await scenario_on_error_compensate_fn()
    await scenario_on_error_compensate_node()
    await scenario_no_on_error_fails_through()
    await scenario_flush_drops_parked_set()
    scenario_build_parses_on_error()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
