"""Escalation surfacing (Y3): when a stage exhausts its attempts and escalates
to a human, the failed node's VERDICT must not be thrown away. The harness folds
a generic `escalation` dict onto the parked envelope's payload (so it round-trips
through the baton store), and `yaah list --json` / the prose view surface it — so
`yaah list` shows WHY the stage broke at exactly the moment it broke.

DOMAIN-FREE: generic vocabulary only ('escalation','failures','stage').

Run: cd yaah && PYTHONPATH=src python3 tests/test_escalation_surface.py
"""
from __future__ import annotations

import asyncio

from yaah import (
    Envelope,
    Failure,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    Suspended,
    Verdict,
)
from yaah.runtime import _baton_json


class Stubborn:
    """Never satisfies its validator -> the stage escalates to a human."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text="nope", ok=False)


class HardCheck:
    """A HARD validator: fails with a named failure code/message/fix_hint."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("ok"):
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(
            Failure("not_ok", "needs ok=true", "set ok=true")
        ).to_envelope(input)


def _escalating_harness() -> Harness:
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", HardCheck())
    return Harness(comms, Graph.of(Stage(
        "audit", node="role:stubborn", validators=["role:check"],
        max_attempts=1, escalate="human")))


async def scenario_parked_baton_carries_the_failed_verdict() -> None:
    """At the escalate-to-human park, the failed validator's verdict is folded
    onto the parked envelope's payload as a generic `escalation` dict carrying
    the failure code/message/fix_hint — so it survives in baton.pending."""
    h = _escalating_harness()
    out = await h.run(Envelope("task", {"text": "x"}))
    assert isinstance(out, Suspended) and out.awaiting == "human:audit", out

    baton = await h.batons.load(out.baton_id)
    esc = baton.pending.payload.get("escalation")
    assert esc is not None, baton.pending.payload
    assert esc["stage"] == "audit", esc
    fails = esc["failures"]
    assert len(fails) == 1, fails
    assert fails[0]["code"] == "not_ok", fails
    assert fails[0]["message"] == "needs ok=true", fails
    assert fails[0]["fix_hint"] == "set ok=true", fails


async def scenario_escalation_round_trips_through_baton_store() -> None:
    """The `escalation` dict is scalar (same shape as `concerns`), so it survives
    a serialize -> deserialize cycle through the baton store."""
    h = _escalating_harness()
    out = await h.run(Envelope("task", {"text": "x"}))
    baton = await h.batons.load(out.baton_id)
    revived = baton.from_dict(baton.to_dict())
    esc = revived.pending.payload.get("escalation")
    assert esc["failures"][0]["code"] == "not_ok", esc


async def scenario_baton_json_surfaces_escalation() -> None:
    """The `yaah list --json` per-baton shape (_baton_json) includes `escalation`
    when present, so a driver skill sees the failure that parked the stage."""
    h = _escalating_harness()
    out = await h.run(Envelope("task", {"text": "x"}))
    baton = await h.batons.load(out.baton_id)
    j = _baton_json(baton)
    assert j["escalation"] is not None, j
    assert j["escalation"]["failures"][0]["code"] == "not_ok", j


async def scenario_no_escalation_when_gate_parks_cleanly() -> None:
    """A normal human-gate park (a node that returns `await`, not an exhausted
    escalation) carries NO escalation dict -> _baton_json escalation is None."""
    h = _escalating_harness()
    # reuse the same harness shape but make the validator pass so no escalation
    comms = InProcessComms()

    class AwaitGate:
        async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
            return input.reply("await", awaiting="review")

    comms.register("role:gate", AwaitGate())
    h2 = Harness(comms, Graph.of(Stage("g", node="role:gate")))
    out = await h2.run(Envelope("task", {"text": "x"}))
    assert isinstance(out, Suspended), out
    baton = await h2.batons.load(out.baton_id)
    j = _baton_json(baton)
    assert j["escalation"] is None, j


async def main() -> None:
    await scenario_parked_baton_carries_the_failed_verdict()
    await scenario_escalation_round_trips_through_baton_store()
    await scenario_baton_json_surfaces_escalation()
    await scenario_no_escalation_when_gate_parks_cleanly()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
