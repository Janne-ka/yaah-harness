"""Gate driver: drive() runs a gated pipeline to completion via injected decisions.

The harness parks at each gate (Suspended); drive() loops run->decide->resume
until Done. Decisions come from an injected callable (sync or async), so the
decision source stays out of the harness.

Run: cd yaah && PYTHONPATH=src python3 tests/test_gate_driver.py
"""
from __future__ import annotations

import asyncio

from yaah import (
    Done,
    Envelope,
    Failure,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    Suspended,
    Verdict,
    drive,
)
from yaah.harness import build_decider


class Stubborn:
    """Never satisfies its validator -> the stage escalates to human."""
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", text="nope", ok=False)


class OkValidator:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if input.payload.get("ok"):
            return Verdict.passed().to_envelope(input)
        return Verdict.failed(Failure("not_ok", "needs ok=true", "set ok=true")).to_envelope(input)


class Echo:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", **input.payload)


class Soft:
    """A soft validator: records a concern without blocking (a sceptic)."""
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return Verdict.failed(Failure("nit", "minor", "tidy"), severity="soft").to_envelope(input)


class AwaitGate:
    """A node that deliberately parks the run for an external decision."""
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("await", awaiting="review")


def _gate_stage(name, then=None):
    return Stage(name, node="role:stubborn", validators=["role:check"],
                 max_attempts=1, feedback=True, escalate="human", then=then)


async def scenario_no_gate_passthrough() -> None:
    """A pipeline with no gate drives straight to Done; decide is never called."""
    comms = InProcessComms()
    comms.register("role:echo", Echo())
    h = Harness(comms, Graph.of(Stage("only", node="role:echo")))

    called = {"n": 0}
    def decide(s):  # should never run
        called["n"] += 1
        return Envelope("result", {})

    out = await drive(h, Envelope("task", {"text": "hi"}), decide)
    assert isinstance(out, Done), out
    assert called["n"] == 0, "decide must not be called when there is no gate"


async def scenario_single_gate_to_done() -> None:
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    h = Harness(comms, Graph.of(_gate_stage("gate")))

    seen = {}
    def decide(s: Suspended) -> Envelope:
        seen["awaiting"] = s.awaiting
        return Envelope("result", {"text": "approved", "ok": True})

    out = await drive(h, Envelope("task", {}), decide)
    assert isinstance(out, Done), out
    assert seen["awaiting"] == "human:gate", seen
    assert out.output.payload["text"] == "approved", out.output
    assert not await h.batons.list_suspended(), "baton evicted after the driven run finishes"


async def scenario_multi_gate_async_decider() -> None:
    """Two gates back to back; an ASYNC decider approves each."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    h = Harness(comms, Graph.of(_gate_stage("g1", then="g2"), _gate_stage("g2")))

    order = []
    async def decide(s: Suspended) -> Envelope:
        order.append(s.awaiting)
        await asyncio.sleep(0)  # prove async deciders are awaited
        return Envelope("result", {"ok": True})

    out = await drive(h, Envelope("task", {}), decide)
    assert isinstance(out, Done), out
    assert order == ["human:g1", "human:g2"], order


async def scenario_concerns_reach_decider() -> None:
    """The real pattern: a sceptic stage PASSES while recording a soft concern,
    then a later await-gate surfaces the accumulated concerns to the decider."""
    comms = InProcessComms()
    comms.register("role:work", Echo())       # passes (soft validator doesn't block)
    comms.register("role:soft", Soft())       # records a concern
    comms.register("role:gate", AwaitGate())  # parks the run
    h = Harness(comms, Graph.of(
        Stage("work", node="role:work", validators=["role:soft"], then="gate"),
        Stage("gate", node="role:gate"),
    ))

    grabbed = {}
    def decide(s: Suspended) -> Envelope:
        grabbed["concerns"] = s.concerns
        return Envelope("result", {"ok": True})

    out = await drive(h, Envelope("task", {"text": "x"}), decide)
    assert isinstance(out, Done), out
    assert grabbed["concerns"] and grabbed["concerns"][0]["code"] == "nit", grabbed


async def scenario_fault_park_exact_key_only() -> None:
    """M13: a FAULT park (escalate lane) is tagged 'human:<stage>'. A decisions
    map keyed by the BARE stage name must NOT auto-resume it (that suffix match
    silently auto-approved a parked FAILURE); only an EXACT 'human:<stage>' key
    may. Driven end-to-end through build_decider + drive."""
    def fresh_harness():
        comms = InProcessComms()
        comms.register("role:stubborn", Stubborn())
        comms.register("role:check", OkValidator())
        return Harness(comms, Graph.of(_gate_stage("merge")))

    # (a) bare 'merge' key must NOT match the fault park 'human:merge' -> walk away PARKED
    h = fresh_harness()
    decide = build_decider({"decisions": {"merge": {"ok": True}}})
    out = await drive(h, Envelope("task", {}), decide)
    assert isinstance(out, Suspended), "human:merge must not suffix-match decisions['merge']; got {}".format(out)
    assert out.awaiting == "human:merge", out.awaiting
    assert await h.batons.list_suspended(), "the unresumed fault must stay a parked baton"

    # (b) exact 'human:merge' key DOES resume the fault park to completion
    h2 = fresh_harness()
    decide2 = build_decider({"decisions": {"human:merge": {"ok": True}}})
    out2 = await drive(h2, Envelope("task", {}), decide2)
    assert isinstance(out2, Done), out2
    assert not await h2.batons.list_suspended(), "baton evicted after the exact-key resume finishes"


async def scenario_driven_walkaway_is_graceful() -> None:
    """A decisions-driven run that reaches a gate it has no answer for must end
    as a parked baton, NOT a traceback: drive() returns the Suspended outcome
    (previously build_decider raised RuntimeError inside the loop)."""
    comms = InProcessComms()
    comms.register("role:stubborn", Stubborn())
    comms.register("role:check", OkValidator())
    h = Harness(comms, Graph.of(_gate_stage("gate")))

    decide = build_decider({"decisions": {"something-else": {"ok": True}}})
    out = await drive(h, Envelope("task", {}), decide)  # must not raise
    assert isinstance(out, Suspended), out
    assert out.awaiting == "human:gate", out.awaiting
    parked = await h.batons.list_suspended()
    assert parked and parked[0].id == out.baton_id, "walk-away leaves a resumable baton"


async def scenario_max_gates_guard() -> None:
    """A graph that routes back to its own gate (here: a branch that always
    defaults to itself) re-suspends forever — the guard makes drive() raise,
    not hang."""
    comms = InProcessComms()
    comms.register("role:gate", AwaitGate())
    # branch on a field the decision never sets -> default always loops back
    loop = Stage("loop", node="role:gate", branch={"on": "done", "default": "loop"})
    h = Harness(comms, Graph.of(loop))

    def never_done(s: Suspended) -> Envelope:
        return Envelope("result", {})  # never sets 'done' -> routes back to the gate

    try:
        await drive(h, Envelope("task", {}), never_done, max_gates=5)
        raise AssertionError("expected RuntimeError from the max_gates guard")
    except RuntimeError as e:
        assert "exceeded 5 gates" in str(e), e


async def main() -> None:
    await scenario_no_gate_passthrough()
    await scenario_single_gate_to_done()
    await scenario_multi_gate_async_decider()
    await scenario_concerns_reach_decider()
    await scenario_fault_park_exact_key_only()
    await scenario_driven_walkaway_is_graceful()
    await scenario_max_gates_guard()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
