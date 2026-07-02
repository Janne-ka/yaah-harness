"""Non-agent node types: shell (worker), shell_check (validator), render.

Run: cd yaah && PYTHONPATH=src python3 tests/test_nodes.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Kind, NodeConfig, Verdict
from yaah.core import Node
from yaah.nodes import RenderNode, ShellCheck, ShellNode


def test_nodes_declare_and_enforce_the_node_contract() -> None:
    # The shipped nodes DECLARE `class X(Node)`, so the contract is visible in the
    # class header + enforced: a declared subclass missing invoke() can't be built.
    # Check `Node in __mro__` (real inheritance), NOT issubclass — Node is a
    # @runtime_checkable Protocol, so issubclass is structural and would pass even
    # for a class that only conforms by shape (vacuous). __mro__ proves the decl.
    for cls in (RenderNode, ShellCheck, ShellNode):
        assert Node in cls.__mro__, "{} must explicitly declare Node".format(cls.__name__)
    try:
        class BadNode(Node):  # declares the port but never implements invoke
            pass
        BadNode()
    except TypeError as e:
        assert "invoke" in str(e), e
    else:
        raise AssertionError("an incomplete Node subclass must not instantiate")


async def main() -> None:
    test_nodes_declare_and_enforce_the_node_contract()
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

    # config.timeout is honored per-node: a slow command past the deadline is
    # KILLED and surfaced STRUCTURALLY (fault-tolerance E4) — ok=False +
    # timed_out=True (never exit 0), not a bare raise — early_review #13.
    r = await ShellNode(["sleep", "5"]).invoke(task, NodeConfig(timeout=0.2))
    assert r.payload.get("timed_out") is True and r.payload.get("ok") is False, r.payload

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
