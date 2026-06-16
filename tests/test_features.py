"""Harness features: fan-out (parallel stages) and human-gate nodes (await/resume).

Run: cd yaah && PYTHONPATH=src python3 tests/test_features.py
"""
from __future__ import annotations

import asyncio

from yaah import (
    Done,
    Envelope,
    Graph,
    Harness,
    InProcessComms,
    Kind,
    NodeConfig,
    Stage,
    StageFailed,
    Suspended,
)
from yaah.build import build


class Lens:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(Kind.RESULT, lens=self.tag, findings=[self.tag + "-f1"])


async def scenario_fanout() -> None:
    comms = InProcessComms()
    for t in ["a", "b", "c"]:
        comms.register("role:" + t, Lens(t))
    graph = Graph(
        stages={"rev": Stage("rev", node="role:a", fanout=["role:a", "role:b", "role:c"])},
        start="rev",
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done), out
    results = out.output.payload["results"]
    assert len(results) == 3, out.output
    assert {r["lens"] for r in results} == {"a", "b", "c"}, out.output  # all lenses ran


class Boom:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raise RuntimeError("lens exploded")


async def scenario_fanout_one_role_fails() -> None:
    """One fan-out role failing yields a StageFailed (not a raw exception) and
    names the failed role; the others still ran (early_review #15)."""
    comms = InProcessComms()
    comms.register("role:a", Lens("a"))
    comms.register("role:b", Boom())
    comms.register("role:c", Lens("c"))
    graph = Graph(
        stages={"rev": Stage("rev", node="role:a", fanout=["role:a", "role:b", "role:c"])},
        start="rev",
    )
    raised = None
    try:
        await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    except StageFailed as e:
        raised = e
    assert raised is not None, "a failing fan-out role must raise StageFailed, not a raw error"
    assert "role:b" in raised.verdict.failures[0].message, raised.verdict.failures[0].message


async def scenario_human_gate() -> None:
    config = {
        "nodes": {"gate:approve": {"type": "human_gate", "ask": "approve?"}},
        "graph": {"start": "g", "stages": {"g": {"node": "gate:approve", "then": None}}},
    }
    harness = build(config)  # in-proc; human_gate needs no backend

    res = await harness.run(Envelope(Kind.TASK, {}))
    assert isinstance(res, Suspended), res
    assert res.awaiting == "human", res

    done = await harness.resume(res.baton_id, Envelope(Kind.RESULT, {"decision": "approved"}))
    assert isinstance(done, Done), done
    assert done.output.payload["decision"] == "approved", done.output


class Judge:
    def __init__(self, decision: str) -> None:
        self.decision = decision

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(Kind.RESULT, decision=self.decision)


class Marker:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(Kind.RESULT, marker=self.tag)


async def scenario_branch() -> None:
    comms = InProcessComms()
    comms.register("role:judge", Judge("accept"))
    comms.register("role:accepted", Marker("ACCEPTED"))
    comms.register("role:rejected", Marker("REJECTED"))
    graph = Graph(
        stages={
            "judge": Stage("judge", node="role:judge",
                           branch={"on": "decision",
                                   "routes": {"accept": "accepted", "reject": "rejected"}}),
            "accepted": Stage("accepted", node="role:accepted"),
            "rejected": Stage("rejected", node="role:rejected"),
        },
        start="judge",
    )
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {}))
    assert isinstance(out, Done), out
    assert out.output.payload["marker"] == "ACCEPTED", out.output  # routed by the verdict


class Flag:
    def __init__(self, val: bool) -> None:
        self.val = val

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(Kind.RESULT, passed=self.val)


async def scenario_branch_bool_and_missing() -> None:
    """Branch on a JSON boolean matches "true"/"false" route keys, not Python's
    "True"; an absent field routes to default (early_review #8)."""
    comms = InProcessComms()
    comms.register("role:flag", Flag(True))
    comms.register("role:yes", Marker("YES"))
    comms.register("role:no", Marker("NO"))
    comms.register("role:def", Marker("DEFAULT"))
    boolgraph = Graph(stages={
        "f": Stage("f", node="role:flag",
                   branch={"on": "passed", "routes": {"true": "yes", "false": "no"}}),
        "yes": Stage("yes", node="role:yes"),
        "no": Stage("no", node="role:no"),
    }, start="f")
    out = await Harness(comms, boolgraph).run(Envelope(Kind.TASK, {}))
    assert out.output.payload["marker"] == "YES", out.output  # bool True -> "true"

    missgraph = Graph(stages={
        "f": Stage("f", node="role:flag",
                   branch={"on": "absent", "routes": {"x": "yes"}, "default": "def"}),
        "yes": Stage("yes", node="role:yes"),
        "def": Stage("def", node="role:def"),
    }, start="f")
    out = await Harness(comms, missgraph).run(Envelope(Kind.TASK, {}))
    assert out.output.payload["marker"] == "DEFAULT", out.output  # absent field -> default


async def scenario_branch_after_fanout() -> None:
    """A branch after a fan-out can still read an ORIGINAL input field — the
    merge carries it forward (early_review #17)."""
    comms = InProcessComms()
    comms.register("role:a", Lens("a"))
    comms.register("role:b", Lens("b"))
    comms.register("role:picked", Marker("PICKED"))
    comms.register("role:miss", Marker("MISS"))
    graph = Graph(stages={
        "rev": Stage("rev", node="role:a", fanout=["role:a", "role:b"],
                     branch={"on": "route", "routes": {"x": "picked"}, "default": "miss"}),
        "picked": Stage("picked", node="role:picked"),
        "miss": Stage("miss", node="role:miss"),
    }, start="rev")
    out = await Harness(comms, graph).run(Envelope(Kind.TASK, {"route": "x"}))
    assert isinstance(out, Done), out
    # routed to PICKED → the original `route` field survived the fan-out merge
    assert out.output.payload["marker"] == "PICKED", out.output


async def main() -> None:
    await scenario_fanout()
    await scenario_fanout_one_role_fails()
    await scenario_branch_bool_and_missing()
    await scenario_branch_after_fanout()
    await scenario_human_gate()
    await scenario_branch()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
