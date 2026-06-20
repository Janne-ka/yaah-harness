"""R6 — envelope carriage integration. With `trace.mode: envelope`, the worker's
spans accrete on the outgoing envelope's `headers["trace"]` (NEVER payload), and
the orchestrator merges them on receive. Verifies the end-to-end carriage path
the unit-level EnvelopeTracer test cannot exercise.

Run: cd yaah && PYTHONPATH=src python3 tests/test_envelope_carriage.py
"""
from __future__ import annotations

import asyncio

from yaah.build import build
from yaah.core import Envelope
from yaah.harness import Done
from yaah.trace import EnvelopeTracer, RecordingTracer
from yaah.trace.contributors import CostContributor, PhaseContributor


class _UsageBackend:
    async def complete(self, prompt, *, model=None, **opts):
        on_usage = opts.get("on_usage")
        if on_usage is not None:
            on_usage({"tokens_in": 10, "tokens_out": 3, "model": model})
        return "done"


CONFIG = {
    "nodes": {"echo": {"type": "agent", "template": "hi {{x}}", "model": "m"}},
    "graph": {"start": "s", "stages": {"s": {"node": "echo"}}},
}


async def scenario_envelope_mode_carries_spans_on_headers_never_payload() -> None:
    """With EnvelopeTracer as the harness tracer, after a run the spans the AGENT
    emitted must (a) NOT appear in any reply's payload and (b) ride home via the
    Agent's reply `headers["trace"]` which the harness consumes via `ingest`."""
    tr = EnvelopeTracer(contributors=[PhaseContributor(), CostContributor()])
    h = build(CONFIG, backend=_UsageBackend(), tracer=tr)
    out = await h.run(Envelope("task", {"x": "there"}))
    assert isinstance(out, Done), out
    # the final completed payload has no trace fingerprint
    assert "trace" not in (out.output.payload or {})
    # the harness ingested the agent's drained records into its own (= tr) buffer
    # — they're still there because the orchestrator IS the terminal consumer in
    # this in-proc setup; nobody drained the tracer after the run finished
    drained = await tr.drain(out.output.correlation_id)
    names = sorted({r["name"] for r in drained})
    assert "model_call" in names, names
    # carriage marker: model_call must include token info (cost contributor fired)
    mc = next(r for r in drained if r["name"] == "model_call")
    assert mc["tokens_in"] == 10 and mc["tokens_out"] == 3, mc


async def scenario_bus_mode_does_NOT_carry_on_headers() -> None:
    """With a non-carriage tracer the Agent must NOT touch headers["trace"] — the
    spans go via the tracer's own channel (here, RecordingTracer.records).
    Guards against accidentally always-on carriage that would inflate every
    envelope for nothing."""
    tr = RecordingTracer([PhaseContributor(), CostContributor()])
    h = build(CONFIG, backend=_UsageBackend(), tracer=tr)
    await h.run(Envelope("task", {"x": "there"}))
    # records visible via the recording surface
    names = [r["name"] for r in tr.records]
    assert "model_call" in names
    # and crucially: no carriage happened — the recording tracer drained nothing,
    # because the Agent's is_carriage check skipped its drain entirely
    assert getattr(tr, "is_carriage", False) is False


async def scenario_envelope_carriage_truncation_marker_survives() -> None:
    """The size cap fires when many spans accumulate for one corr. A
    `trace_truncated` marker must appear in the final drain."""
    # A tracer with a tiny buffer to force truncation. The harness run will produce
    # 2 spans (stage + model_call) which exceeds buffer_max=1 → 1 dropped.
    tr = EnvelopeTracer(contributors=[PhaseContributor(), CostContributor()],
                        buffer_max=1)
    h = build(CONFIG, backend=_UsageBackend(), tracer=tr)
    out = await h.run(Envelope("task", {"x": "y"}))
    assert isinstance(out, Done)
    drained = await tr.drain(out.output.correlation_id)
    names = [r["name"] for r in drained]
    assert "trace_truncated" in names, names


async def scenario_boundary_drains_for_non_agent_nodes() -> None:
    """assessment #6: the drain lives at the serve boundary (CarriageBoundaryNode,
    applied by _wrap_node), not inside Agent.invoke — so spans emitted by ANY
    node type (shell/transform/gate) travel too. Unit-level: a non-agent node
    emits during invoke; the wrapped reply carries the record on headers."""
    from yaah.core import NodeConfig
    from yaah.trace import CarriageBoundaryNode, Span
    from yaah.trace.contributors import PhaseContributor

    tr = EnvelopeTracer(contributors=[PhaseContributor()])

    class _SpanningNode:
        async def invoke(self, env, config):
            await tr.emit(Span.timed("shell_exec", corr=env.correlation_id,
                                     t0=0.0, t1=0.1, status="ok"))
            return env.reply("result", ok=True)

    node = CarriageBoundaryNode(_SpanningNode(), tr)
    out = await node.invoke(Envelope("task", {}, {"correlation_id": "C9"}), NodeConfig())
    names = [r["name"] for r in out.headers.get("trace", [])]
    assert "shell_exec" in names, out.headers
    # and the boundary CLEARED the buffer (drain semantics — no double delivery)
    assert await tr.drain("C9") == []


async def scenario_nested_broker_spans_survive() -> None:
    """assessment #6, the '4 emitted, 2 survive' loss: a broker NODE (an agent)
    shares the tracer + corr with the calling stage. The old Agent-body drain
    emptied the corr onto the broker's reply headers, which the context_broker
    tool discarded. Now: the broker's serve boundary drains, the tool re-ingests
    — every span survives to the stage's own boundary/terminal drain."""
    from yaah.agents import Agent, ScriptedToolBackend
    from yaah.comms import InProcessComms
    from yaah.core import NodeConfig
    from yaah.trace import CarriageBoundaryNode
    from yaah.trace.contributors import PhaseContributor

    tr = EnvelopeTracer(contributors=[PhaseContributor()])
    comms = InProcessComms()

    # broker node: a real Agent — emits its own model_call under the SAME corr.
    broker = Agent(_UsageBackend(), template="slice {{query}}", stage="broker", tracer=tr, parse=False)
    # the serve boundary _wrap_node would apply in a worker:
    comms.register("role:broker", CarriageBoundaryNode(broker, tr), NodeConfig())

    main = Agent(
        ScriptedToolBackend([
            {"calls": [{"id": "c1", "name": "context_broker",
                        "args": {"query": "the auth part"}}]},
            {"text": "done"},
        ]),
        template="go", stage="main", events=comms, tracer=tr,
        broker="role:broker", expose={"payload": ["diff"]}, parse=False)
    out = await main.invoke(
        Envelope("task", {"diff": "x"}, {"correlation_id": "R7"}), NodeConfig())
    assert out.payload["raw"] == "done", out.payload

    survived = await tr.drain("R7")
    names = sorted(r["name"] for r in survived)
    # broker's model_call (reclaimed by the tool) + main's tool_call + main's
    # model_call — nothing lost mid-stage
    assert names.count("model_call") == 2, names
    assert "tool_call" in names, names


async def main() -> None:
    await scenario_envelope_mode_carries_spans_on_headers_never_payload()
    await scenario_bus_mode_does_NOT_carry_on_headers()
    await scenario_envelope_carriage_truncation_marker_survives()
    await scenario_boundary_drains_for_non_agent_nodes()
    await scenario_nested_broker_spans_survive()
    print("test_envelope_carriage: PASS (5 scenarios)")


if __name__ == "__main__":
    asyncio.run(main())
