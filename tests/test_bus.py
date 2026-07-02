"""Distributed model over LocalBus.

Proves: (1) envelopes survive the wire (JSON round-trip); (2) the SAME
build()/Harness runs over a serialized request/reply bus by swapping only the
transport (comms=LocalBus()), with nodes reached by subject name — i.e. the
comms<->harness separation and location transparency hold.

Run: cd yaah && PYTHONPATH=src python3 tests/test_bus.py
"""
from __future__ import annotations

import asyncio
import json

from yaah import Done, Envelope, Kind
from yaah.agents import FakeProvider, RoutingProvider
from yaah.build import build
from yaah.comms import Comms, InMemorySubscription, InProcessComms, Subscription
from yaah.adapters.transports import LocalBus


def test_transports_declare_the_comms_port() -> None:
    # Declaration via __mro__ (real inheritance) — isinstance would be structural
    # for these @runtime_checkable Protocols and prove nothing about the decl.
    # NatsComms/_NatsSubscription declare too (checked where the NATS tests import).
    for cls in (InProcessComms, LocalBus):
        assert Comms in cls.__mro__, "{} must declare Comms".format(cls.__name__)
    assert Subscription in InMemorySubscription.__mro__
    # Enforcement: a declared-but-incomplete transport can't instantiate.
    try:
        class HalfComms(Comms):  # missing publish/subscribe
            async def request(self, target, envelope): ...
        HalfComms()
    except TypeError as e:
        assert "publish" in str(e) or "subscribe" in str(e), e
    else:
        raise AssertionError("an incomplete Comms subclass must not instantiate")

CONFIG = {
    "nodes": {
        "role:spec": {"type": "agent", "template": "Task: {{task}}", "model": "fake:spec", "stage": "spec"},
        "role:json": {"type": "json_object", "required": ["summary", "items"]},
    },
    "graph": {
        "start": "spec",
        "stages": {
            "spec": {"node": "role:spec", "validators": ["role:json"],
                     "max_attempts": 3, "feedback": True, "then": None},
        },
    },
}


def test_serialization() -> None:
    e = Envelope(Kind.TASK, {"x": [1, 2], "s": "hi"}, headers={"baton": "b", "correlation_id": "c"})
    back = Envelope.from_json(e.to_json())
    assert back.kind == e.kind, back
    assert back.payload == e.payload, back
    assert back.headers == e.headers, back
    assert back.id == e.id, back


async def test_pipeline_over_bus() -> None:
    bus = LocalBus()
    backend = RoutingProvider(
        {"fake": FakeProvider(responses=[
            '{"summary": "ok", "items": ["a"  ',     # invalid -> retry over the bus
            '{"summary": "ok", "items": ["a"]}',     # valid
        ])},
        default="fake",
    )
    # Same build() and Harness as the in-process case; only the transport differs.
    harness = build(CONFIG, comms=bus, backend=backend)

    seen = []

    async def on_event(e: Envelope) -> None:
        seen.append(e.payload.get("msg"))

    await bus.subscribe("events", on_event)

    out = await harness.run(Envelope(Kind.TASK, {"task": "go"}))
    assert isinstance(out, Done), out
    assert json.loads(out.output.payload["raw"]) == {"summary": "ok", "items": ["a"]}, out.output
    assert len(seen) >= 2, "events crossed the bus (two attempts)"  # retry happened over the wire
    await bus.close()


async def test_cancelled_request_no_inbox_leak() -> None:
    """A request cancelled before the reply lands must not orphan its inbox
    future (early_review #4)."""
    bus = LocalBus()

    async def slow(env: Envelope) -> Envelope:
        await asyncio.sleep(10)  # never replies within the test
        return env.reply(Kind.RESULT)

    bus.serve("role:slow", slow)  # serve takes a handler fn (register takes a Node)
    task = asyncio.ensure_future(bus.request("role:slow", Envelope(Kind.TASK, {})))
    await asyncio.sleep(0.05)  # let it register its inbox + block on the future
    assert len(bus._inboxes) == 1, "request should have registered one inbox"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert bus._inboxes == {}, "cancelled request leaked an inbox entry"
    await bus.close()


async def main() -> None:
    test_transports_declare_the_comms_port()
    test_serialization()
    await test_pipeline_over_bus()
    await test_cancelled_request_no_inbox_leak()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
