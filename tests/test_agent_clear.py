"""Agent-clear tests — a clear addressed to a running stage's node-id cancels
its in-flight work ("stop and remove whatever you are doing") and ends the run
as Cleared. Opt-in via Stage.clearable; sender-agnostic (any party may clear).

Run: cd yaah && PYTHONPATH=src python3 tests/test_agent_clear.py
"""
from __future__ import annotations

import asyncio

from yaah import Cleared, Done, Envelope, Graph, Harness, InProcessComms, Stage
from yaah.core import Kind
from yaah.build import build


class Slow:
    """Long-running work; records start and (only on completion) finish, so a
    test can prove cancellation by a finish that never happens."""
    def __init__(self, started, finished):
        self.started, self.finished = started, finished

    async def invoke(self, env, config):
        self.started.append(1)
        await asyncio.sleep(10)          # in-flight work the clear will cancel
        self.finished.append(1)
        return env.reply_with(Kind.RESULT, {"done": True})


class Quick:
    async def invoke(self, env, config):
        return env.reply_with(Kind.RESULT, {"ok": True})


class Capture:
    def __init__(self, sink):
        self.sink = sink

    async def invoke(self, env, config):
        self.sink.append(dict(env.payload))
        return env.reply_with(Kind.RESULT, dict(env.payload))


def _graph(node_role, *, clearable=True):
    return Graph.of(
        Stage("work", id="agent1", node=node_role, clearable=clearable, then="after"),
        Stage("after", node="role:after", then=None),
    )


async def scenario_clear_cancels_inflight() -> None:
    started, finished, after = [], [], []
    comms = InProcessComms()
    comms.register("role:slow", Slow(started, finished))
    comms.register("role:after", Capture(after))

    async def clearer():
        await asyncio.sleep(0.02)        # let the slow node start
        await comms.publish("clear", Envelope(
            Kind.RESULT, {"why": "stop"}, {"clear_id": "agent1:RID"}))
    task = asyncio.ensure_future(clearer())
    out = await Harness(comms, _graph("role:slow")).run(
        Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    await task

    assert isinstance(out, Cleared), out
    assert out.node == "work" and out.clear_id == "agent1:RID", out
    assert out.payload.get("why") == "stop", out.payload
    assert started == [1] and finished == [], (started, finished)  # cancelled mid-flight
    assert after == [], after                                       # downstream never ran


async def scenario_completes_when_not_cleared() -> None:
    after = []
    comms = InProcessComms()
    comms.register("role:quick", Quick())
    comms.register("role:after", Capture(after))
    out = await Harness(comms, _graph("role:quick")).run(
        Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    assert isinstance(out, Done), out                  # finished first; clear never came
    assert after == [{"ok": True}], after              # downstream ran on the real output


async def scenario_clear_scopes() -> None:
    # node-id alone ("agent1", any run) and "*" (flush) also cancel — not just the
    # exact "<node>:<corr>" instance address.
    for cid in ("agent1", "*"):
        started, finished = [], []
        comms = InProcessComms()
        comms.register("role:slow", Slow(started, finished))
        comms.register("role:after", Capture([]))

        async def clearer(c=cid):
            await asyncio.sleep(0.02)
            await comms.publish("clear", Envelope(Kind.RESULT, {}, {"clear_id": c}))
        task = asyncio.ensure_future(clearer())
        out = await Harness(comms, _graph("role:slow")).run(
            Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
        await task
        assert isinstance(out, Cleared) and out.clear_id == cid, (cid, out)
        assert finished == [], (cid, finished)         # cancelled in every scope


async def scenario_unmatched_clear_ignored() -> None:
    # a clear addressed to a DIFFERENT node must not cancel this one; the work
    # completes normally.
    started, finished, after = [], [], []
    comms = InProcessComms()

    class Brief:
        async def invoke(self, env, config):
            started.append(1)
            await asyncio.sleep(0.05)
            finished.append(1)
            return env.reply_with(Kind.RESULT, {"done": True})
    comms.register("role:brief", Brief())
    comms.register("role:after", Capture(after))

    async def clearer():
        await asyncio.sleep(0.01)
        await comms.publish("clear", Envelope(
            Kind.RESULT, {}, {"clear_id": "someoneElse:RID"}))  # not agent1
    task = asyncio.ensure_future(clearer())
    out = await Harness(comms, _graph("role:brief")).run(
        Envelope(Kind.TASK, {}, {"correlation_id": "RID"}))
    await task
    assert isinstance(out, Done), out
    assert finished == [1] and len(after) == 1, (finished, after)


def scenario_build_parses_clearable() -> None:
    from yaah.agents import FakeBackend
    cfg = {
        "nodes": {"role:x": {"type": "agent", "template": "t", "model": "fake:x"}},
        "graph": {"start": "work", "stages": {
            "work": {"node": "role:x", "clearable": True, "then": None},
        }},
    }
    h = build(cfg, backend=FakeBackend(default="{}"))
    assert h.graph.stages["work"].clearable is True


async def main() -> None:
    await scenario_clear_cancels_inflight()
    await scenario_completes_when_not_cleared()
    await scenario_clear_scopes()
    await scenario_unmatched_clear_ignored()
    scenario_build_parses_clearable()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
