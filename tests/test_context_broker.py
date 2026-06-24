"""context_broker — the cheap Haiku-grade fuzzy companion to envelope_get (R12).
Unit checks (fast-path verbatim fetch / fuzzy dispatch / allow-list leak guard /
broker-error surface) plus an end-to-end tool-loop where a scripted model calls
context_broker and the broker NODE returns a slice over Comms.

Run: cd yaah && PYTHONPATH=src python3 tests/test_context_broker.py
"""
from __future__ import annotations

import asyncio

from yaah.core import Envelope, Kind, NodeConfig
from yaah.agents import (
    Agent,
    RoutingBackend,
    ScriptedToolBackend,
    make_context_broker_tool,
)
from yaah.comms import InProcessComms


class _Broker:
    """A stand-in broker node. Behaves like a yaah node — `invoke` returns the
    reply. The test wires it into Comms with `serve(role, node, _)` via
    `_inproc_serve` below."""

    def __init__(self, slice_text: str = "the relevant slice") -> None:
        self._slice = slice_text
        self.calls: list = []

    async def invoke(self, env: Envelope, config) -> Envelope:
        self.calls.append(env.payload)
        return env.reply(Kind.RESULT, slice=self._slice)


def _serve_role(comms: InProcessComms, role: str, node) -> None:
    """Register `role` -> `node` on the in-proc comms (the same shape build()
    uses, distilled to one line for tests)."""
    comms.register(role, node, NodeConfig(model=None))


class _NodeAdapter:
    """Wrap a plain async callable as a Node so it can register with
    InProcessComms (which calls `node.invoke(env, config)`)."""
    def __init__(self, handler) -> None:
        self._handler = handler

    async def invoke(self, env: Envelope, config) -> Envelope:
        return await self._handler(env)


async def scenario_fast_path_verbatim() -> None:
    """If the model passes `field: "diff"` and `diff` is allow-listed, the
    broker returns the value LOCALLY — no broker node call, no model call."""
    comms = InProcessComms()
    broker = _Broker(slice_text="UNUSED")
    _serve_role(comms, "role:broker", broker)
    env = Envelope(Kind.TASK, {"diff": "a\nb\nc\nd"},
                   {"correlation_id": "R"})
    tool = make_context_broker_tool(
        env, broker_role="role:broker", comms=comms,
        expose={"payload": ["diff"]}, max_chars=100)

    out = await tool.impl({"query": "anything", "field": "diff"})
    assert out["value"] == "a\nb\nc\nd", out
    assert out["fast_path"] is True, out
    assert broker.calls == [], "fast-path must not call the broker node"


async def scenario_fast_path_allowlist_blocks_leak() -> None:
    """field= not in expose returns error; doesn't fall through to the broker."""
    comms = InProcessComms()
    broker = _Broker()
    _serve_role(comms, "role:broker", broker)
    env = Envelope(Kind.TASK, {"diff": "ok", "secret": "shh"},
                   {"correlation_id": "R"})
    tool = make_context_broker_tool(
        env, broker_role="role:broker", comms=comms,
        expose={"payload": ["diff"]})

    out = await tool.impl({"query": "x", "field": "secret"})
    assert "error" in out and "not exposed" in out["error"], out
    assert broker.calls == [], "leak attempt must NOT reach the broker"


async def scenario_fuzzy_dispatch() -> None:
    """No `field` → the broker node is called with the query + an allow-listed
    payload snapshot, and its `slice` reply is returned."""
    comms = InProcessComms()
    broker = _Broker(slice_text="the auth-touching part")
    _serve_role(comms, "role:broker", broker)
    env = Envelope(Kind.TASK, {"diff": "FULL DIFF", "secret": "shh"},
                   {"correlation_id": "R"})
    tool = make_context_broker_tool(
        env, broker_role="role:broker", comms=comms,
        expose={"payload": ["diff"]})

    out = await tool.impl({"query": "the part touching auth"})
    assert out["value"] == "the auth-touching part", out
    assert out["fast_path"] is False, out
    assert len(broker.calls) == 1
    sent = broker.calls[0]
    assert sent["query"] == "the part touching auth", sent
    # Snapshot honors the allow-list: `diff` flows through, `secret` does not.
    assert sent["envelope"] == {"diff": "FULL DIFF"}, sent


async def scenario_fuzzy_empty_query() -> None:
    """Empty query AND no field → error (model didn't pass anything to act on)."""
    comms = InProcessComms()
    _serve_role(comms, "role:broker", _Broker())
    env = Envelope(Kind.TASK, {"diff": "x"}, {"correlation_id": "R"})
    tool = make_context_broker_tool(
        env, broker_role="role:broker", comms=comms,
        expose={"payload": ["diff"]})
    out = await tool.impl({"query": "   "})
    assert "error" in out, out


async def scenario_broker_error_surface() -> None:
    """A broker reply with Kind.ERROR is surfaced (the model gets to see it),
    not crashed up to the agent."""
    comms = InProcessComms()

    async def _err_handler(env: Envelope) -> Envelope:
        return env.reply(Kind.ERROR, failure="broker is sad")

    comms.register("role:broker", _NodeAdapter(_err_handler),
                   NodeConfig(model=None))
    env = Envelope(Kind.TASK, {"diff": "x"}, {"correlation_id": "R"})
    tool = make_context_broker_tool(
        env, broker_role="role:broker", comms=comms,
        expose={"payload": ["diff"]})
    out = await tool.impl({"query": "what?"})
    assert "error" in out and "ERROR" in out["error"], out


async def scenario_tool_loop_calls_broker() -> None:
    """End-to-end: the model calls context_broker with a fuzzy query; the
    Agent dispatches to the configured broker over Comms; the agent's final
    answer reflects the broker's slice."""
    comms = InProcessComms()
    broker = _Broker(slice_text="diff-of-interest")
    _serve_role(comms, "role:broker", broker)
    backend = RoutingBackend({"tool": ScriptedToolBackend([
        {"calls": [{"id": "c1", "name": "context_broker",
                    "args": {"query": "what touches auth?"}}]},
        {"text": "found it: diff-of-interest"},
    ])})
    agent = Agent(backend, template="review the diff", stage="rev",
                  events=comms, expose={"payload": ["diff"]},
                  broker="role:broker", parse=False)
    out = await agent.invoke(
        Envelope(Kind.TASK, {"diff": "huge"}, {"correlation_id": "R"}),
        NodeConfig(model="tool:x"))
    assert out.payload["raw"] == "found it: diff-of-interest", out.payload
    assert len(broker.calls) == 1


def scenario_build_parses_broker() -> None:
    """`broker:` flows from the JSON spec through _build_agent into the Agent."""
    from yaah.build import build
    from yaah.agents import FakeBackend
    cfg = {
        "nodes": {"role:r": {"type": "agent", "template": "t", "model": "fake:x",
                             "expose": {"payload": ["diff"]},
                             "broker": "role:context-broker"}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:r"}}},
    }
    h = build(cfg, backend=FakeBackend(default="{}"))
    assert h.graph.stages["s"].node == "role:r"  # built without error


async def main() -> None:
    await scenario_fast_path_verbatim()
    await scenario_fast_path_allowlist_blocks_leak()
    await scenario_fuzzy_dispatch()
    await scenario_fuzzy_empty_query()
    await scenario_broker_error_surface()
    await scenario_tool_loop_calls_broker()
    scenario_build_parses_broker()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
