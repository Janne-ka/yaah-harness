"""Human-gate machinery unit coverage (the TODO 'very good unit-test coverage'
list): ask rendering, the AWAIT park/merge contract, revise-loop edge cases,
resume-merge precedence, and the TTL-expired resume. Deterministic, no claude.

Run: cd yaah && PYTHONPATH=src python3 tests/test_gate_machinery.py
"""
from __future__ import annotations

import asyncio

from yaah import (
    Done,
    Envelope,
    Graph,
    Harness,
    InProcessComms,
    NodeConfig,
    Stage,
    Suspended,
)
from yaah.build.human_gate import HumanGate
from yaah.core import Kind


class CollidingGate:
    """A gate whose reply sets a key the input already carries — exercises the
    park-merge precedence (the gate's reply ENRICHES the artifact, gate wins)."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("await", ask="q?", awaiting="human:gate", spec="GATE-WROTE")


class CountingWriter:
    """Re-produces the artifact each revise round; carries every input key
    except the consumed decision, bumps `rounds`, versions `spec`."""

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        n = int(input.payload.get("rounds", 0)) + 1
        carry = {k: v for k, v in input.payload.items()
                 if k not in ("decision", "rounds", "spec", "ask", "awaiting")}
        return input.reply("result", rounds=n, spec="v{}".format(n), **carry)


class Echo:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply("result", **dict(input.payload))


async def scenario_ask_rendering() -> None:
    """{{key}} fills from the payload; unknown placeholders stay literal;
    non-string values render via str(); empty ask and the awaiting
    default/explicit split behave."""
    gate = HumanGate(ask="Approve {{spec}}? extra={{nope}}")
    out = await gate.invoke(Envelope("task", {"spec": {"acs": [1, 2]}}), NodeConfig())
    assert out.kind == Kind.AWAIT, out.kind
    assert out.payload["awaiting"] == "human", out.payload          # default tag
    assert "{'acs': [1, 2]}" in out.payload["ask"], out.payload     # str() of a dict
    assert "{{nope}}" in out.payload["ask"], out.payload            # unknown stays literal

    explicit = HumanGate(ask="", awaiting="spec:approve")
    out2 = await explicit.invoke(Envelope("task", {"x": 1}), NodeConfig())
    assert out2.payload["ask"] == "" and out2.payload["awaiting"] == "spec:approve", out2.payload


async def scenario_await_park_merges_gate_reply_over_artifact() -> None:
    """_produce_single AWAIT contract: the parked envelope is the gate's INPUT
    artifact augmented with the gate's reply (reply wins a key collision),
    kind and headers preserved — what resume() later merges the decision onto."""
    comms = InProcessComms()
    comms.register("role:gate", CollidingGate())
    h = Harness(comms, Graph.of(Stage("gate", node="role:gate")))
    inp = Envelope("task", {"spec": "ORIGINAL", "task": "T-1"}, {"correlation_id": "c-1"})
    out = await h.run(inp)
    assert isinstance(out, Suspended) and out.awaiting == "human:gate", out
    assert out.ask == "q?", out

    baton = await h.batons.load(out.baton_id)
    parked = baton.pending
    assert parked.kind == inp.kind, parked.kind                      # kind preserved
    assert parked.headers.get("correlation_id") == "c-1", parked.headers
    assert parked.payload["spec"] == "GATE-WROTE", parked.payload    # gate reply wins
    assert parked.payload["task"] == "T-1", parked.payload           # input keys survive
    assert parked.payload["ask"] == "q?", parked.payload             # mailbox shows the question


async def scenario_gate_inside_fanout_parks_the_stage() -> None:
    comms = InProcessComms()
    comms.register("role:gate", CollidingGate())
    comms.register("role:echo", Echo())
    h = Harness(comms, Graph.of(
        Stage("s", node="role:gate", fanout=["role:gate", "role:echo"])))
    out = await h.run(Envelope("task", {"spec": "x"}))
    assert isinstance(out, Suspended) and out.awaiting == "human:gate", out


def _revise_loop() -> Harness:
    """write -> gate; gate branches: revise -> write (the loop), default -> final."""
    comms = InProcessComms()
    comms.register("role:writer", CountingWriter())
    comms.register("role:gate", HumanGate(ask="ok? {{spec}}", awaiting="spec"))
    comms.register("role:final", Echo())
    return Harness(comms, Graph(start="write", stages={
        "write": Stage("write", node="role:writer", then="gate"),
        "gate": Stage("gate", node="role:gate",
                      branch={"on": "decision",
                              "routes": {"revise": "write"}, "default": "final"}),
        "final": Stage("final", node="role:final"),
    }))


async def scenario_multi_round_revise_then_approve() -> None:
    """Three revise rounds (each WITHOUT feedback — must not crash), then an
    approve whose decision keys override the artifact; request survives the
    whole loop."""
    h = _revise_loop()
    out = await h.run(Envelope("task", {"request": "R", "rounds": 0}))
    assert isinstance(out, Suspended), out
    for _ in range(3):  # revise without feedback, N > 2 rounds
        out = await h.resume(out.baton_id, Envelope("result", {"decision": "revise"}))
        assert isinstance(out, Suspended), out
    out = await h.resume(out.baton_id, Envelope(
        "result", {"decision": "approve", "spec": "HUMAN-EDIT"}))
    assert isinstance(out, Done), out
    p = out.output.payload
    assert p["rounds"] == 4, p          # 1 initial + 3 revise rounds
    assert p["spec"] == "HUMAN-EDIT", p  # the human's key overrode the artifact
    assert p["request"] == "R", p        # original request survived the loop


async def scenario_unrecognized_decision_takes_default_route() -> None:
    h = _revise_loop()
    out = await h.run(Envelope("task", {"rounds": 0}))
    out = await h.resume(out.baton_id, Envelope("result", {"decision": "wat"}))
    assert isinstance(out, Done), out    # not a crash, not a loop: the default route


async def scenario_empty_resume_payload_keeps_artifact() -> None:
    """A malformed/empty decision payload must not destroy the parked artifact:
    the merge is artifact + response, so the spec under decision survives."""
    h = _revise_loop()
    out = await h.run(Envelope("task", {"rounds": 0}))
    out = await h.resume(out.baton_id, Envelope("result", {}))
    assert isinstance(out, Done), out    # no decision key -> default route
    assert out.output.payload["spec"] == "v1", out.output.payload


async def scenario_expired_gate_resume_fails_cleanly() -> None:
    """A parked gate whose baton TTL lapsed is swept on the next resume — the
    resume fails with the clean 'finished, expired, or never existed' error,
    not a half-alive run."""
    now = [1000.0]
    comms = InProcessComms()
    comms.register("role:gate", HumanGate(ask="ok?"))
    h = Harness(comms, Graph.of(Stage("gate", node="role:gate")),
                wall_clock=lambda: now[0])
    out = await h.run(Envelope("task", {"spec": "x"}), ttl=60)
    assert isinstance(out, Suspended), out
    now[0] += 3600.0                     # the human walked away for an hour
    try:
        await h.resume(out.baton_id, Envelope("result", {"decision": "approve"}))
    except KeyError as e:
        assert "expired" in str(e), e
        return
    raise AssertionError("resume of an expired baton should raise KeyError")


async def main() -> None:
    await scenario_ask_rendering()
    await scenario_await_park_merges_gate_reply_over_artifact()
    await scenario_gate_inside_fanout_parks_the_stage()
    await scenario_multi_round_revise_then_approve()
    await scenario_unrecognized_decision_takes_default_route()
    await scenario_empty_resume_payload_keeps_artifact()
    await scenario_expired_gate_resume_fails_cleanly()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
