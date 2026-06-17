"""Fault-tolerance pass E2 — a parked run must survive an INFRASTRUCTURAL error.

A baton lives in the store ONLY because the run previously PARKED (suspended,
awaiting a human). `_settle` used to `delete` the baton on ANY exception out of
`_drive` — so a transport/store blip (or a cancellation) while RESUMING destroyed
the parked run and lost the human's pending decision. E2 splits the handler:
a logical `StageFailed` still evicts (the run genuinely failed), but any other
exception is preserved (left resumable). Paired with E1, an ordinary node
exception is now a logical `StageFailed` (evict); only a true infrastructural
`BaseException` preserves.

Run: cd yaah && PYTHONPATH=src python3 tests/test_baton_preserve.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage, StageFailed
from yaah.core import Failure, Verdict


class InfraError(BaseException):
    """A non-`Exception` fault — models a transport/store blip or a cancellation
    that bypasses E1's in-proc `Exception` convergence and reaches `_settle`."""


class GateNode:
    async def invoke(self, env, config):
        return env.reply("result", text="needs review", ok=False)  # fails -> parks


class OkValidator:
    async def invoke(self, env, config):
        if env.payload.get("ok"):
            return Verdict.passed().to_envelope()
        return Verdict.failed(Failure("not_ok", "need ok", "set ok")).to_envelope()


class BoomNode:
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def invoke(self, env, config):
        self.calls += 1
        raise self.exc


def _gate_then_boom(boom: BoomNode) -> Harness:
    comms = InProcessComms()
    comms.register("role:gate", GateNode())
    comms.register("role:check", OkValidator())
    comms.register("role:boom", boom)
    graph = Graph.of(
        Stage("gate", node="role:gate", validators=["role:check"],
              max_attempts=1, escalate="human", then="boom"),
        Stage("boom", node="role:boom", max_attempts=1),
    )
    return Harness(comms, graph)


def _track_deletes(h: Harness) -> list:
    deletes: list = []
    real = h.batons.delete

    async def tracking(bid):
        deletes.append(bid)
        return await real(bid)

    h.batons.delete = tracking
    return deletes


async def scenario_infra_error_preserves_parked_baton() -> None:
    boom = BoomNode(InfraError("simulated store/transport blip"))
    h = _gate_then_boom(boom)
    deletes = _track_deletes(h)

    susp = await h.run(Envelope("task", {}))
    bid = susp.baton_id
    assert deletes == [], "parking must not delete the baton"

    caught = None
    try:
        await h.resume(bid, Envelope("result", {"ok": True}))  # advances gate -> boom -> raises
    except BaseException as e:
        caught = e
    assert isinstance(caught, InfraError), caught
    assert boom.calls == 1, boom.calls
    assert deletes == [], "an infrastructural error must NOT evict the parked run"
    assert await h.batons.load(bid) is not None, "the parked run must remain resumable"
    print("PASS infrastructural error preserves the parked, resumable baton")


async def scenario_logical_failure_still_evicts() -> None:
    # E1 turns an ordinary node exception into a node_error verdict -> StageFailed
    # (a LOGICAL terminal failure) -> _settle evicts, as before.
    boom = BoomNode(RuntimeError("an ordinary logic bug, not a blip"))
    h = _gate_then_boom(boom)
    deletes = _track_deletes(h)

    susp = await h.run(Envelope("task", {}))
    bid = susp.baton_id
    raised = False
    try:
        await h.resume(bid, Envelope("result", {"ok": True}))
    except StageFailed:
        raised = True
    assert raised, "a logical failure propagates as StageFailed"
    assert bid in deletes, "a logical terminal failure MUST evict the baton"
    print("PASS logical StageFailed still evicts the baton")


async def main() -> None:
    await scenario_infra_error_preserves_parked_baton()
    await scenario_logical_failure_still_evicts()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
