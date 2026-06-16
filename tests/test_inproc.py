"""Smoke test for the YAAH in-process kernel.

Run: cd yaah && PYTHONPATH=src python3 tests/test_inproc.py
Exercises the three Comms modes and the Envelope/Verdict round-trip with the
two interfaces only — no harness yet.
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Failure, InProcessComms, NodeConfig, Verdict


class UpperWorker:
    """Trivial worker: uppercases payload['text']."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text=input.payload["text"].upper())


class TextPresentValidator:
    """Trivial validator: passes if payload has 'text', else fails."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("text"):
            return Verdict.passed().to_envelope()
        return Verdict.failed(Failure("empty", "no text", "include some text")).to_envelope()


async def main() -> None:
    comms = InProcessComms()
    comms.register("role:upper", UpperWorker())
    comms.register("role:check", TextPresentValidator())

    # Call mode (request/reply)
    out = await comms.request("role:upper", Envelope("task", {"text": "hi"}))
    assert out.payload["text"] == "HI", out
    assert out.headers.get("correlation_id"), "reply should keep a correlation id"

    # A validator returning a Verdict, carried as an Envelope
    verdict = Verdict.from_envelope(await comms.request("role:check", out))
    assert verdict.ok, verdict

    # A failing validator round-trips its failures
    bad = Verdict.from_envelope(await comms.request("role:check", Envelope("task", {})))
    assert not bad.ok and bad.failures[0].code == "empty", bad

    # Event mode (publish/subscribe)
    seen = []

    async def on_event(e: Envelope) -> None:
        seen.append(e)

    sub = await comms.subscribe("events", on_event)
    await comms.publish("events", Envelope("event", {"n": 1}))
    assert len(seen) == 1, seen
    sub.cancel()
    await comms.publish("events", Envelope("event", {"n": 2}))
    assert len(seen) == 1, "cancelled subscription should not receive"

    # Unknown target raises
    try:
        await comms.request("role:missing", Envelope("task"))
        raise AssertionError("expected LookupError for unknown target")
    except LookupError:
        pass

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
