"""Non-agent node types: shell (worker), shell_check (validator), render.

Run: cd yaah && PYTHONPATH=src python3 tests/test_nodes.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Kind, NodeConfig, Verdict
from yaah.nodes import RenderNode, ShellCheck, ShellNode


async def main() -> None:
    cfg = NodeConfig()
    task = Envelope(Kind.TASK, {"title": "Eval Report"})

    # shell worker
    out = await ShellNode(["echo", "hello"]).invoke(task, cfg)
    assert out.payload["ok"] is True and "hello" in out.payload["stdout"], out.payload

    # shell_check validator: passing and failing
    ok = Verdict.from_envelope(await ShellCheck(["true"]).invoke(task, cfg))
    assert ok.ok, ok
    bad = Verdict.from_envelope(await ShellCheck(["false"]).invoke(task, cfg))
    assert not bad.ok and bad.failures[0].code == "shell_exit", bad

    # shell_check with expect_exit (RED-style: command must fail)
    red = Verdict.from_envelope(await ShellCheck(["false"], expect_exit=1).invoke(task, cfg))
    assert red.ok, "expect_exit=1 passes when the command exits 1"

    # render: fill a mustache template
    r = await RenderNode(template="<h1>{{title}}</h1>").invoke(task, cfg)
    assert r.payload["output"] == "<h1>Eval Report</h1>", r.payload

    # config.timeout is honored per-node: a slow command past the deadline raises
    # (and the subprocess is killed) — early_review #13.
    timed_out = False
    try:
        await ShellNode(["sleep", "5"]).invoke(task, NodeConfig(timeout=0.2))
    except asyncio.TimeoutError:
        timed_out = True
    assert timed_out, "ShellNode must honor config.timeout"

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
