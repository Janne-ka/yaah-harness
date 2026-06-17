"""Fault-tolerance pass E7 — a failing fan-in `reduce` must be OBSERVABLE, not a
silent hang.

The fan-in coordinator runs as a background task; a `reduce` target that raised
used to have its exception retrieved-and-discarded by `_drain`, so the fork saw
only a MISSING clear and hung (the #2-ranked engine fault). E7 wraps the reduce:
on failure it emits a `fanin_reduce_failed` error span and publishes a join error,
so the fork fails observably (and, bounded by `wait.timeout`, proceeds) instead of
hanging forever.

Run: cd yaah && PYTHONPATH=src python3 tests/test_fork_reduce_error.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage
from yaah.core import Kind


def boom_reduce(arrived):  # fn: reduce target — always raises
    raise RuntimeError("reduce target is broken")


class BranchNode:
    async def invoke(self, env, config):
        return env.reply(Kind.RESULT, ran=True)


class CapturingTracer:
    def __init__(self):
        self.spans = []

    async def emit(self, span):
        self.spans.append(span)


async def scenario_reduce_failure_is_observable_and_bounded() -> None:
    comms = InProcessComms()
    comms.register("role:a", BranchNode())
    comms.register("role:b", BranchNode())
    comms.register("role:summary", BranchNode())
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b"], then="summary",
              wait={"timeout": 1.0}),  # bound the fork wait so a hang can't pass
        Stage("a", node="role:a", then="join"),
        Stage("b", node="role:b", then="join"),
        Stage("join", node="", then=None,
              fanin={"expect": ["a", "b"], "wait": "all",
                     "reduce": "fn:test_fork_reduce_error:boom_reduce"}),
        Stage("summary", node="role:summary", then=None),
    )
    tracer = CapturingTracer()
    h = Harness(comms, graph, tracer=tracer)

    # the whole run must COMPLETE (no hang) — a generous wall guard around it.
    await asyncio.wait_for(h.run(Envelope("task", {})), timeout=5.0)

    reduce_errs = [s for s in tracer.spans
                   if "fanin_reduce_failed" in str(s.attrs.get("error", ""))]
    assert reduce_errs, "a failed reduce must emit an observable error span"
    print("PASS failed reduce is observable (error span) and does not hang")


async def main() -> None:
    await scenario_reduce_failure_is_observable_and_bounded()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
