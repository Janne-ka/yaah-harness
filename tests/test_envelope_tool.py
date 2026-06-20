"""envelope_get — the built-in tool that lets an agent SELECTIVELY pull data from
its own invocation envelope (R9). Unit checks (allow-list / filter / cap) + an
end-to-end tool-loop where a scripted model calls envelope_get and gets the value.

Run: cd yaah && PYTHONPATH=src python3 tests/test_envelope_tool.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Kind, NodeConfig
from yaah.agents import Agent, RoutingBackend, ScriptedToolBackend, make_envelope_get_tool


async def scenario_allowlist_filter_cap() -> None:
    env = Envelope(Kind.TASK, {"diff": "a\nb\nc\nd", "secret": 42},
                   {"baton": "B", "correlation_id": "R"})
    tool = make_envelope_get_tool(
        env, expose={"payload": ["diff"], "header": []},
        filters={"head": lambda v, n=1: "\n".join(v.splitlines()[:n])}, max_chars=3)

    r1 = await tool.impl({"key": "diff"})
    assert r1["value"] == "a\nb" and r1["truncated"]
    assert "error" in await tool.impl({"key": "secret"})              # not exposed
    assert "error" in await tool.impl({"source": "header", "key": "baton"})  # leak rule
    assert (await tool.impl({"key": "diff", "filter": {"name": "head", "n": 1}}))["value"] == "a"
    assert "error" in await tool.impl({"key": "diff", "filter": {"name": "nope"}})  # unknown filter


async def scenario_tool_loop_pull() -> None:
    # the model "decides" to call envelope_get, then answers using the pulled value
    backend = RoutingBackend({"tool": ScriptedToolBackend([
        {"calls": [{"id": "c1", "name": "envelope_get", "args": {"key": "diff"}}]},
        {"text": "done"},
    ])})
    agent = Agent(backend, template="review the diff", stage="rev",
                  expose={"payload": ["diff"]}, parse=False)
    out = await agent.invoke(
        Envelope(Kind.TASK, {"diff": "the change"}, {"correlation_id": "R"}),
        NodeConfig(model="tool:x"))
    assert out.payload["raw"] == "done", out.payload


def scenario_build_parses_expose() -> None:
    from yaah.build import build
    from yaah.agents import FakeBackend
    cfg = {
        "nodes": {"role:r": {"type": "agent", "template": "t", "model": "fake:x",
                             "expose": {"payload": ["diff"]}, "max_chars": 500}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:r"}}},
    }
    h = build(cfg, backend=FakeBackend(default="{}"))
    a = h.graph.stages["s"]
    assert a.node == "role:r"  # built without error (expose/max_chars accepted)


async def main() -> None:
    await scenario_allowlist_filter_cap()
    await scenario_tool_loop_pull()
    scenario_build_parses_expose()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
