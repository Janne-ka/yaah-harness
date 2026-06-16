"""Integration test over a REAL NATS broker.

Same Harness as every other test; nodes are served over NATS and the harness
reaches them by subject, so every request/reply and event crosses the broker.
Needs `nats-py` and a running nats-server (set NATS_URL, default
nats://127.0.0.1:4222). Self-skips if either is missing, so the normal suite
(system python, no nats) is unaffected.

Run: cd yaah && PYTHONPATH=src .venv/bin/python tests/test_nats_integration.py
"""
from __future__ import annotations

import asyncio
import json
import os

try:
    import nats  # noqa: F401
except Exception:
    print("skip: nats-py not installed")
    raise SystemExit(0)

from yaah import Done, Envelope, Graph, Harness, Kind, NodeConfig, Stage
from yaah.agents import Agent, FakeBackend, RoutingBackend
from yaah.adapters.transports import NatsComms
from yaah.validators import JsonObjectValidator


async def main() -> None:
    servers = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
    try:
        comms = await NatsComms(servers).connect()
    except Exception as e:  # no server reachable
        print("skip: cannot connect to nats ({})".format(e))
        return

    backend = RoutingBackend(
        {"fake": FakeBackend(responses=[
            '{"summary": "ok", "items": ["a"  ',     # invalid -> retry over the broker
            '{"summary": "ok", "items": ["a"]}',     # valid
        ])},
        default="fake",
    )

    # Serve nodes over NATS (in a real deployment these run in worker processes).
    await comms.serve_node(
        "role:spec",
        Agent(backend, "Task: {{task}}", events=comms, stage="spec"),
        NodeConfig(model="fake:spec"),
    )
    await comms.serve_node("role:json", JsonObjectValidator(required=["summary", "items"]))

    graph = Graph(
        stages={"spec": Stage("spec", node="role:spec", validators=["role:json"],
                              max_attempts=3, feedback=True)},
        start="spec",
    )

    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"task": "go"}))
    assert isinstance(out, Done), out
    assert json.loads(out.output.payload["raw"]) == {"summary": "ok", "items": ["a"]}, out.output

    await scenario_queue_group(servers)

    await comms.close()
    print("ok (over real NATS at {})".format(servers))


async def scenario_queue_group(servers: str) -> None:
    """Two replicas of a node in one queue group form a shared pool: each request
    goes to exactly ONE of them (load-balanced), not both. The shared-cloud node
    serving multiple callers draws from this same pool."""
    comms = await NatsComms(servers).connect()
    counts = {"a": 0, "b": 0}

    def make(tag: str):
        async def handler(env: Envelope) -> Envelope:
            counts[tag] += 1
            return env.reply(Kind.RESULT, by=tag)
        return handler

    # two subscribers, same subject + same queue group (serve_node defaults the
    # queue to the role; here we use serve() to control the two replicas)
    await comms.serve("role:pool", make("a"), queue="role:pool")
    await comms.serve("role:pool", make("b"), queue="role:pool")

    n = 10
    for _ in range(n):
        await comms.request("role:pool", Envelope(Kind.TASK, {}))

    assert counts["a"] + counts["b"] == n, counts  # each request handled ONCE, not twice
    assert counts["a"] > 0 and counts["b"] > 0, ("not load-balanced", counts)
    await comms.close()


if __name__ == "__main__":
    asyncio.run(main())
