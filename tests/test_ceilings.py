"""Fault-tolerance pass E4 — default ceilings so nothing hangs forever.

- A shell command that exceeds its timeout is KILLED and surfaced STRUCTURALLY:
  ShellNode → a result with ok=False + timed_out=True (never exit 0); ShellCheck →
  a `shell_timeout` verdict that is NOT a pass even for an `expect_nonzero` RED gate
  (a hang must not read as 'tests failed as required').
- A backward branch route can't spin `_drive` forever — a step ceiling fails the
  run cleanly (StageFailed) instead of livelocking.

Run: cd yaah && PYTHONPATH=src python3 tests/test_ceilings.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage, StageFailed
from yaah.core import Kind, NodeConfig, Verdict
from yaah.nodes.shell_node import ShellNode
from yaah.nodes.shell_check import ShellCheck


async def scenario_shell_node_timeout_is_structured() -> None:
    node = ShellNode("sleep 5", timeout=0.2, shell=True)
    out = await node.invoke(Envelope("task", {}), NodeConfig())
    assert out.payload.get("timed_out") is True, out.payload
    assert out.payload.get("ok") is False, out.payload
    assert out.payload.get("exit_code") == 124, out.payload
    print("PASS ShellNode timeout → structured ok=False/timed_out (not exit 0)")


async def scenario_shell_check_timeout_never_passes() -> None:
    # expect_nonzero is the RED gate: a real nonzero exit is a PASS. A timeout must
    # NOT be — else a hung test runner satisfies RED falsely.
    check = ShellCheck("sleep 5", timeout=0.2, shell=True, expect_nonzero=True)
    out = await check.invoke(Envelope("task", {}), NodeConfig())
    verdict = Verdict.from_envelope(out)
    assert not verdict.ok, "a timeout must never satisfy an expect_nonzero gate"
    assert verdict.failures[0].code == "shell_timeout", verdict.failures
    print("PASS ShellCheck timeout → shell_timeout fail, not a RED pass")


class TickNode:
    async def invoke(self, env, config):
        return env.reply(Kind.RESULT, go="yes")  # always routes back → would loop forever


async def scenario_drive_step_ceiling() -> None:
    comms = InProcessComms()
    comms.register("role:tick", TickNode())
    graph = Graph.of(
        Stage("loop", node="role:tick",
              branch={"on": "go", "routes": {"yes": "loop"}, "default": None})
    )
    h = Harness(comms, graph)
    h._max_steps = 5  # tiny ceiling so the test is fast
    raised = None
    try:
        await h.run(Envelope("task", {}))
    except StageFailed as e:
        raised = e
    assert raised is not None, "a self-cycling branch must hit the step ceiling"
    assert raised.verdict.failures[0].code == "step_ceiling", raised.verdict.failures
    print("PASS _drive step ceiling stops a livelocking branch route")


async def main() -> None:
    await scenario_shell_node_timeout_is_structured()
    await scenario_shell_check_timeout_never_passes()
    await scenario_drive_step_ceiling()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
