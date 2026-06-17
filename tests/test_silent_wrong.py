"""Fault-tolerance pass E6 — close the silent-wrong-output class.

- A render with a placeholder that has NO payload value used to ship the literal
  `{{name}}` at exit 0 (a broken report/spec — the worst fault class). It now
  FAILS the stage by default (`render_unfilled_placeholders`), pointing at the
  likely-missing parse step. `allow_unfilled=true` opts a template back into the
  degrade-and-surface behaviour for intentionally-optional fields.
- A branch whose `on` key is ABSENT from the payload (a typo'd producer) silently
  takes the default; the stage span now marks it `<absent→default>` so the silent
  misroute is visible.

Run: cd yaah && PYTHONPATH=src python3 tests/test_silent_wrong.py
"""
from __future__ import annotations

import asyncio

from yaah import Envelope, Graph, Harness, InProcessComms, Stage
from yaah.core import Kind, NodeConfig
from yaah.nodes.render_node import RenderNode


async def scenario_render_fails_on_unfilled() -> None:
    node = RenderNode(template="<h1>{{title}}</h1> by {{author}} — {{missing}}")
    out = await node.invoke(Envelope("task", {"title": "Report", "author": "x"}), NodeConfig())
    assert out.kind == Kind.VERDICT, out.kind
    assert out.payload["status"] == "fail", out.payload
    f = out.payload["failures"][0]
    assert f["code"] == "render_unfilled_placeholders", f
    assert "missing" in f["message"], f                      # names the offending key
    assert "parse" in f["fix_hint"], f                       # points at the likely cause
    print("PASS render FAILS on an unfilled placeholder (footgun closed)")


async def scenario_render_allow_unfilled_degrades() -> None:
    node = RenderNode(template="<h1>{{title}}</h1> — {{missing}}", allow_unfilled=True)
    out = await node.invoke(Envelope("task", {"title": "Report"}), NodeConfig())
    assert "{{missing}}" in out.payload["output"], out.payload["output"]  # opt-in: still renders
    assert out.payload.get("unfilled") == ["missing"], out.payload.get("unfilled")
    print("PASS allow_unfilled=true opts back into degrade-and-surface")


async def scenario_render_no_unfilled_key_when_complete() -> None:
    node = RenderNode(template="<h1>{{title}}</h1>")
    out = await node.invoke(Envelope("task", {"title": "Report"}), NodeConfig())
    assert "unfilled" not in out.payload, out.payload
    print("PASS a fully-filled render carries no unfilled marker")


class CapturingTracer:
    def __init__(self):
        self.spans = []

    async def emit(self, span):
        self.spans.append(span)


class NoKeyNode:
    async def invoke(self, env, config):
        return env.reply(Kind.RESULT, something_else="x")  # the branch key is absent


async def scenario_absent_route_key_is_traced() -> None:
    comms = InProcessComms()
    comms.register("role:n", NoKeyNode())
    graph = Graph.of(
        Stage("decide", node="role:n",
              branch={"on": "decision", "routes": {"approve": None}, "default": None})
    )
    tracer = CapturingTracer()
    await Harness(comms, graph, tracer=tracer).run(Envelope("task", {}))
    routes = [s.attrs.get("route") for s in tracer.spans if s.attrs.get("route")]
    assert routes == ["<absent→default>"], routes
    print("PASS an absent branch key is traced as <absent→default>")


async def main() -> None:
    await scenario_render_fails_on_unfilled()
    await scenario_render_allow_unfilled_degrades()
    await scenario_render_no_unfilled_key_when_complete()
    await scenario_absent_route_key_is_traced()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
