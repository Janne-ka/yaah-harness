"""Fault-tolerance pass E8 — NATS connection-state is OBSERVABLE.

The orchestrator used to discover a dead/partitioned broker only via a 300s
request timeout — a transient blip and a permanent partition looked identical.
Wiring `error/disconnected/reconnected/closed` callbacks to a tracer makes the
connection state a span stream. No NATS server is needed here: the callbacks are
exercised directly (nats-py is only imported by `connect`).

Run: cd yaah && PYTHONPATH=src python3 tests/test_nats_conn_spans.py
"""
from __future__ import annotations

import asyncio

from yaah.adapters.transports.nats_comms import NatsComms


class CapturingTracer:
    def __init__(self):
        self.spans = []

    async def emit(self, span):
        self.spans.append(span)


async def main() -> None:
    tracer = CapturingTracer()
    c = NatsComms(tracer=tracer)
    await c._on_disconnected()
    await c._on_reconnected()
    await c._on_error(RuntimeError("broker unreachable"))
    await c._on_closed()
    events = [(s.attrs.get("event"), s.status) for s in tracer.spans if s.name == "nats"]
    assert events == [("disconnected", "error"), ("reconnected", "ok"),
                      ("error", "error"), ("closed", "error")], events
    assert "broker unreachable" in tracer.spans[2].attrs.get("detail", "")
    print("PASS NATS connection-state callbacks emit observable spans")

    # no tracer wired → silent (today's behaviour for a plain local broker)
    silent = NatsComms()
    await silent._on_disconnected()  # must not crash, must emit nothing
    print("PASS NATS is silent when no tracer is wired")

    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
