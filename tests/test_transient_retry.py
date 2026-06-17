"""Transient-fault tolerance + in-proc exception convergence (fault-tolerance
pass E1 + E3).

E1: an in-proc node that RAISES must become a retryable hard-fail verdict
(a clean StageFailed with a traced span) — NOT a bare traceback out of run(),
the way a remote Kind.ERROR reply already converges.

E3: a TRANSIENT fault (provider overload/timeout, git index-lock) retries on a
SEPARATE `error_retries` budget that does NOT spend `max_attempts` — so a blip
can't fail a max_attempts:1 hard gate — while a PERMANENT fault fails fast and
consumes none of the transient budget.

Run: cd yaah && PYTHONPATH=src python3 tests/test_transient_retry.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage, StageFailed
from yaah.core import Kind


class FlakyNode:
    """Raises a TRANSIENT error the first `fail_n` calls, then succeeds."""
    def __init__(self, fail_n: int, exc_text: str = "overloaded: 503 try again later"):
        self.fail_n = fail_n
        self.exc_text = exc_text
        self.calls = 0

    async def invoke(self, env, config):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise RuntimeError(self.exc_text)
        return env.reply_with(Kind.RESULT, {"ok": True})


class PermanentNode:
    """Raises a PERMANENT (non-transient) error every call."""
    def __init__(self):
        self.calls = 0

    async def invoke(self, env, config):
        self.calls += 1
        raise ValueError("bad key 'foo' — a logic bug, not a blip")


def _harness(comms: InProcessComms, stage: Stage) -> Harness:
    h = Harness(comms, Graph.of(stage))

    async def _noop(_seconds):  # don't actually wait through the backoff
        return None

    h._sleep = _noop
    return h


async def _run(h: Harness):
    return await h.run(Envelope(Kind.TASK, {}, {"correlation_id": "R"}))


async def scenario_transient_then_succeed_on_hard_gate() -> None:
    comms = InProcessComms()
    node = FlakyNode(fail_n=2)
    comms.register("role:n", node)
    # max_attempts:1 (a one-shot hard gate) — yet TWO transient blips are absorbed
    # by the separate error_retries budget, and the third call succeeds.
    await _run(_harness(comms, Stage("s", node="role:n", max_attempts=1, error_retries=2)))
    assert node.calls == 3, node.calls
    print("PASS transient-then-succeed within budget on a max_attempts:1 gate")


async def scenario_transient_over_budget_fails() -> None:
    comms = InProcessComms()
    node = FlakyNode(fail_n=5)
    comms.register("role:n", node)
    h = _harness(comms, Stage("s", node="role:n", max_attempts=1, error_retries=2))
    raised = False
    try:
        await _run(h)
    except StageFailed:
        raised = True
    assert raised, "exhausting the transient budget must fail the stage"
    assert node.calls == 3, node.calls  # 1 initial + 2 retries, then give up
    print("PASS transient over budget → StageFailed (no infinite retry)")


async def scenario_permanent_fails_fast() -> None:
    comms = InProcessComms()
    node = PermanentNode()
    comms.register("role:n", node)
    h = _harness(comms, Stage("s", node="role:n", max_attempts=1, error_retries=2))
    raised = False
    try:
        await _run(h)
    except StageFailed:
        raised = True
    assert raised, "a permanent fault must fail"
    assert node.calls == 1, "a permanent fault must NOT consume the transient budget"
    print("PASS permanent fault fails fast (0 transient retries)")


async def scenario_inproc_exception_converges() -> None:
    # E1: a raising in-proc node becomes a StageFailed (the retryable verdict
    # path), never a bare RuntimeError/ValueError out of run().
    comms = InProcessComms()
    comms.register("role:n", PermanentNode())
    h = _harness(comms, Stage("s", node="role:n", max_attempts=1, error_retries=0))
    outcome = None
    try:
        await _run(h)
    except StageFailed:
        outcome = "stagefailed"
    except BaseException as e:  # a raw traceback escaping run() is the bug we fixed
        outcome = "raw:" + type(e).__name__
    assert outcome == "stagefailed", outcome
    print("PASS in-proc exception converges to StageFailed, not a raw traceback")


async def main() -> None:
    await scenario_transient_then_succeed_on_hard_gate()
    await scenario_transient_over_budget_fails()
    await scenario_permanent_fails_fast()
    await scenario_inproc_exception_converges()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
