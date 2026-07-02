"""Harness.clear() (graceful reset, not pkill) + universal clearability.

Every node is clearable by DEFAULT now (no opt-in needed): a `*` clear cancels
whatever is in-flight. Harness.clear() composes the primitives — broadcast `*`
clear (cancel in-flight nodes, release waiters) + flush the parked set + drop
suspended batons — so an operator resets the harness without killing the process.

Run: cd yaah && PYTHONPATH=src python3 tests/test_harness_clear.py
"""
from __future__ import annotations

import asyncio

from yaah import Cleared, Envelope, Graph, Harness, InProcessComms, Stage, Suspended
from yaah.core import Kind, NodeConfig
from yaah.store import EnvelopeStore, MemoryBackend


class Slow:
    def __init__(self, started, finished):
        self.started, self.finished = started, finished

    async def invoke(self, env, config):
        self.started.append(1)
        await asyncio.sleep(10)
        self.finished.append(1)
        return env.reply_with(Kind.RESULT, {})


class Gate:
    async def invoke(self, env, config):
        return env.reply("await", awaiting="human")


async def scenario_default_clearable_no_flag() -> None:
    # a plain stage with NO `clearable` set is clearable anyway (default on)
    started, finished = [], []
    comms = InProcessComms()
    comms.register("role:slow", Slow(started, finished))
    graph = Graph.of(Stage("work", id="w", node="role:slow", then=None))  # no clearable flag

    async def clearer():
        await asyncio.sleep(0.02)
        await comms.publish("clear", Envelope(Kind.RESULT, {}, {"clear_id": "*"}))
    task = asyncio.ensure_future(clearer())
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}, {"correlation_id": "R"}))
    await task
    assert isinstance(out, Cleared), out                 # cancelled despite no opt-in
    assert started == [1] and finished == [], (started, finished)


async def scenario_harness_clear_resets() -> None:
    # park envelopes + suspend a run, then clear() drops both
    comms = InProcessComms()
    comms.register("role:gate", Gate())
    es = EnvelopeStore(MemoryBackend())
    await es.save("g:R:a", Envelope(Kind.RESULT, {"n": 1}))
    await es.save("g:R:b", Envelope(Kind.RESULT, {"n": 2}))
    graph = Graph.of(Stage("g", node="role:gate", then=None))
    h = Harness(comms, graph, envelope_store=es)

    out = await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "R"}))
    assert isinstance(out, Suspended), out               # parked at the gate (baton saved)

    result = await h.clear()
    assert result["parked_flushed"] == 2, result          # both parked envelopes dropped
    assert result["batons_dropped"] == 1, result          # the suspended run abandoned
    assert await es.list("") == [], "parked set empty after clear"
    assert await h.batons.list_suspended() == [], "no suspended batons after clear"


async def main() -> None:
    await scenario_default_clearable_no_flag()
    await scenario_harness_clear_resets()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
