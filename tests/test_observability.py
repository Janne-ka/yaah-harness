"""Fault-tolerance pass E5 — cheap "why" observability.

- DECISION PROVENANCE: a branch stage's span records the value that drove its
  route (`attrs.route`) — previously the branch key lived only in transient
  payload, so "why did it park/rework/block?" was untraceable.
- PER-ATTEMPT history: a failed/retried attempt emits its own note span
  (`attrs.retry`), so a stage that passed on try 3 no longer looks identical to
  one that passed on try 1.

Run: cd yaah && PYTHONPATH=src python3 tests/test_observability.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage
from yaah.core import Kind


class CapturingTracer:
    def __init__(self):
        self.spans = []

    async def emit(self, span):
        self.spans.append(span)


class RouteNode:
    async def invoke(self, env, config):
        return env.reply(Kind.RESULT, decision="approve")


class FlakyNode:
    def __init__(self, fail_n, text="overloaded 503"):
        self.fail_n = fail_n
        self.text = text
        self.calls = 0

    async def invoke(self, env, config):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RuntimeError(self.text)
        return env.reply(Kind.RESULT, ok=True)


async def scenario_route_provenance_traced() -> None:
    comms = InProcessComms()
    comms.register("role:r", RouteNode())
    graph = Graph.of(
        Stage("decide", node="role:r",
              branch={"on": "decision", "routes": {"approve": None}, "default": None})
    )
    tracer = CapturingTracer()
    await Harness(comms, graph, tracer=tracer).run(Envelope("task", {}))
    routes = [s.attrs.get("route") for s in tracer.spans
              if s.name == "stage" and "route" in s.attrs]
    assert routes == ["approve"], routes
    print("PASS branch decision value recorded on the stage span (attrs.route)")


async def scenario_per_attempt_history_traced() -> None:
    comms = InProcessComms()
    comms.register("role:n", FlakyNode(fail_n=2))
    h = Harness(comms, Graph.of(Stage("s", node="role:n", max_attempts=1, error_retries=2)),
                tracer=CapturingTracer())

    async def _noop(_):
        return None

    h._sleep = _noop
    await h.run(Envelope("task", {}))
    retries = [s.attrs for s in h._tracer.spans
               if s.name == "stage" and s.attrs.get("retry") == "transient"]
    assert len(retries) == 2, retries  # two transient retries before success
    assert retries[0].get("n") == 1 and retries[1].get("n") == 2, retries
    print("PASS each transient retry emits its own note span (per-attempt history)")


async def main() -> None:
    await scenario_route_provenance_traced()
    await scenario_per_attempt_history_traced()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
