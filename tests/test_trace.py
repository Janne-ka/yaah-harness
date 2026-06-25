"""Tracer engine core: Span + contributors (phase/cost/tools) projected by the
Null / Recording / Bus carriages.

Run: cd yaah && PYTHONPATH=src python3 tests/test_trace.py
"""
from __future__ import annotations

import asyncio
from typing import List, Tuple

from yaah.build import build
from yaah.comms import InProcessComms
from yaah.core import Envelope
from yaah.harness import Done
from yaah.trace import BusTracer, EnvelopeTracer, NullTracer, RecordingTracer, Span
from yaah.trace.contributors import CostContributor, PhaseContributor, ToolsContributor


class _UsageBackend:
    """A backend that reports token usage via the R4 on_usage callback."""
    async def complete(self, prompt, *, model=None, **opts):
        on_usage = opts.get("on_usage")
        if on_usage is not None:
            on_usage({"tokens_in": 10, "tokens_out": 3, "model": model})
        return "done"


def _stage_span() -> Span:
    return Span(id="s1", corr="run-1", name="stage", parent="p0",
                t_start=1.0, t_end=1.5, duration_ms=500.0,
                status="ok", attrs={"stage": "review", "attempt": 1})


def _model_span() -> Span:
    return Span(id="m1", corr="run-1", name="model_call", parent="s1",
                duration_ms=1200.0, tokens_in=800, tokens_out=120,
                model="claude:sonnet", status="ok")


async def scenario_phase_minimum() -> None:
    tr = RecordingTracer([PhaseContributor()])
    assert tr.captures == frozenset({"phase"})
    await tr.emit(_stage_span())
    [r] = tr.records
    # structural stitch keys always present
    assert r["id"] == "s1" and r["corr"] == "run-1" and r["parent"] == "p0"
    assert r["name"] == "stage" and r["t_start"] == 1.0 and r["t_end"] == 1.5
    # phase fragment
    assert r["stage"] == "review" and r["status"] == "ok" and r["duration_ms"] == 500.0
    # cost is OFF -> no token leakage into the record
    assert "tokens_in" not in r and "model" not in r


async def scenario_cost_is_orthogonal() -> None:
    tr = RecordingTracer([PhaseContributor(), CostContributor()])
    assert tr.captures == frozenset({"phase", "cost"})
    # cost contributes only on model calls -> stage records stay lean
    await tr.emit(_stage_span())
    assert "tokens_in" not in tr.records[-1]
    # a model_call gets the cost fragment
    await tr.emit(_model_span())
    r = tr.records[-1]
    assert r["tokens_in"] == 800 and r["tokens_out"] == 120 and r["model"] == "claude:sonnet"
    assert r["duration_ms"] == 1200.0  # phase still applies (orthogonal)


async def scenario_tools_capture() -> None:
    tr = RecordingTracer([PhaseContributor(), ToolsContributor()])
    span = Span(id="t1", corr="run-1", name="tool_call", parent="m1",
                tool="grep", duration_ms=10.0, status="ok")
    await tr.emit(span)
    r = tr.records[-1]
    assert r["tool"] == "grep" and r["status"] == "ok"
    # tools contributes nothing to a stage span
    await tr.emit(_stage_span())
    assert "tool" not in tr.records[-1]


async def scenario_drain_by_corr() -> None:
    # R6 port semantic: drain RETURNS AND CLEARS that corr's buffer (the records
    # are about to ride on an outgoing envelope; don't deliver them twice).
    # Tests inspect `tracer.records` (the unconditional list) for assertions
    # that shouldn't disturb the buffer.
    tr = RecordingTracer([PhaseContributor()])
    await tr.emit(_stage_span())                       # corr run-1
    await tr.emit(Span(id="x", corr="run-2", name="stage", status="ok"))
    got = await tr.drain("run-1")
    assert len(got) == 1 and got[0]["corr"] == "run-1"
    # cleared: a second drain returns nothing; the other corr untouched
    assert await tr.drain("run-1") == []
    assert [r.get("corr") for r in tr.records] == ["run-2"]


async def scenario_null_tracer_off() -> None:
    tr = NullTracer()
    assert tr.captures == frozenset()
    await tr.emit(_stage_span())          # no-op, must not raise
    # R6: drain is now part of the port; non-carriage tracers (Null/Bus) return []
    assert await tr.drain("any-corr") == []


async def scenario_bus_tracer_publishes() -> None:
    published: List[Tuple[str, Envelope]] = []

    class StubComms:
        async def publish(self, topic: str, envelope: Envelope) -> None:
            published.append((topic, envelope))

    tr = BusTracer(StubComms(), contributors=[PhaseContributor(), CostContributor()])
    await tr.emit(_model_span())
    assert len(published) == 1
    topic, env = published[0]
    assert topic == "trace" and env.kind == "event"
    assert env.payload["tokens_in"] == 800 and env.payload["name"] == "model_call"
    # bus carriage publishes; it doesn't accrue on envelopes — drain always []
    assert await tr.drain("run-1") == []


async def scenario_bus_tracer_ingest_is_capped_and_dict_only() -> None:
    # assessment #6: ingested records arrive from REMOTE reply headers — cap the
    # per-call batch (one truncation marker for the rest) and drop non-dicts so
    # a runaway/malicious worker can't flood every subscribed sink.
    published: List[Tuple[str, Envelope]] = []

    class StubComms:
        async def publish(self, topic: str, envelope: Envelope) -> None:
            published.append((topic, envelope))

    tr = BusTracer(StubComms())
    over = tr.INGEST_MAX + 5
    await tr.ingest([{"name": "n{}".format(i)} for i in range(over)]
                    + ["garbage", 42])  # non-dicts silently dropped
    # the published batch is capped at EXACTLY INGEST_MAX, marker included (not +1)
    assert len(published) == tr.INGEST_MAX, len(published)
    marker = published[-1][1].payload
    # kept INGEST_MAX-1 real records + this marker; dropped = 1005 - (INGEST_MAX-1) = 6
    assert marker["name"] == "trace_truncated" and marker["dropped"] == 6, marker


async def scenario_envelope_tracer_buffers_per_corr_and_drains() -> None:
    # R6: emit() buffers; drain(corr) returns AND CLEARS that corr only
    tr = EnvelopeTracer(contributors=[PhaseContributor()])
    await tr.emit(_stage_span())                                            # corr run-1
    await tr.emit(Span(id="x", corr="run-2", name="stage", status="ok"))    # corr run-2
    out_a = await tr.drain("run-1")
    assert len(out_a) == 1 and out_a[0]["corr"] == "run-1"
    assert await tr.drain("run-1") == []                                    # cleared
    out_b = await tr.drain("run-2")                                          # untouched
    assert len(out_b) == 1 and out_b[0]["corr"] == "run-2"


async def scenario_envelope_tracer_caps_buffer_with_truncated_marker() -> None:
    # R6 size cap: with buffer_max=2 and 4 emits on one corr, drain returns the
    # 2 newest + 1 trace_truncated marker (count of dropped). Never blow envelopes.
    tr = EnvelopeTracer(contributors=[PhaseContributor()], buffer_max=2)
    for i in range(4):
        await tr.emit(Span(id="s{}".format(i), corr="r", name="stage",
                           status="ok", attrs={"stage": "n{}".format(i)}))
    out = await tr.drain("r")
    assert len(out) == 3, [r.get("name") for r in out]
    assert out[-1]["name"] == "trace_truncated" and out[-1]["dropped"] == 2
    # the surviving records are the NEWEST (FIFO drop oldest)
    surviving_stages = [r.get("stage") for r in out[:-1]]
    assert surviving_stages == ["n2", "n3"], surviving_stages


async def scenario_envelope_tracer_satisfies_tracer_protocol() -> None:
    # protocol guard: EnvelopeTracer can stand in anywhere a Tracer is expected
    from yaah.trace import Tracer
    assert isinstance(EnvelopeTracer(), Tracer)
    assert isinstance(NullTracer(), Tracer)


def scenario_aggregate() -> None:
    from yaah.trace.aggregate import aggregate, cost_usd, percentile

    # pure helpers
    assert cost_usd("m1", 1000, 500, {"m1": {"input": 1.0, "output": 2.0}}) == 2.0
    assert cost_usd("unknown", 1000, 500, {"m1": {}}) == 0.0   # unknown model -> 0
    assert cost_usd("m1", 1, 1, None) == 0.0                   # no price-map -> 0
    assert percentile([], 95) == 0.0 and percentile([7.0], 95) == 7.0
    assert percentile([100.0, 200.0], 50) == 150.0

    records = [
        {"name": "stage", "corr": "r1", "stage": "spec", "duration_ms": 100.0, "status": "ok"},
        # assessment cluster 5 #5: a stage that FAILED is a retry indicator; the
        # old metric was n_model_calls - n_stage_spans (over-reported for
        # tool-loop turns, under-reported when the retry didn't reach the model).
        {"name": "stage", "corr": "r1", "stage": "code", "duration_ms": 50.0, "status": "error"},
        {"name": "stage", "corr": "r1", "stage": "code", "duration_ms": 300.0, "status": "ok"},
        {"name": "model_call", "corr": "r1", "model": "m1", "tokens_in": 1000, "tokens_out": 500},
        {"name": "model_call", "corr": "r1", "model": "m1", "tokens_in": 1000, "tokens_out": 500},
        {"name": "model_call", "corr": "r1", "model": "m2", "tokens_in": 0, "tokens_out": 0},
        {"name": "tool_call", "corr": "r1", "tool": "grep"},
        {"name": "tool_call", "corr": "r1", "tool": "grep"},
        {"name": "stage", "corr": "r2", "stage": "spec", "duration_ms": 200.0, "status": "ok"},
        {"name": "model_call", "corr": "r2", "model": "m1", "tokens_in": 100, "tokens_out": 50},
    ]
    agg = aggregate(records, price_map={"m1": {"input": 1.0, "output": 2.0}})

    assert agg["totals"]["runs"] == 2
    assert abs(agg["totals"]["cost_usd"] - 4.2) < 1e-9
    assert agg["totals"]["tokens_in"] == 2100 and agg["totals"]["tokens_out"] == 1050
    assert agg["totals"]["retries"] == 1            # one error-status stage span (the failed code attempt)
    assert agg["totals"]["errors"] == 1            # the same failed attempt also shows up in the error list
    assert abs(agg["runs"]["r1"]["cost_usd"] - 4.0) < 1e-9
    # per-stage latency percentiles (spec ran in both runs: [100, 200])
    assert agg["stages"]["spec"]["count"] == 2
    assert agg["stages"]["spec"]["p50_ms"] == 150.0 and agg["stages"]["spec"]["max_ms"] == 200.0
    assert agg["tools"]["grep"] == 2
    assert agg["models"]["m1"]["calls"] == 3 and agg["models"]["m2"]["calls"] == 1


async def scenario_console_sink() -> None:
    import io

    from yaah.adapters.trace import ConsoleTraceSink

    buf = io.StringIO()
    sink = ConsoleTraceSink(stream=buf)
    await sink.handle(Envelope("event", {"name": "stage", "stage": "review",
                                         "status": "ok", "duration_ms": 12.0}))
    await sink.handle(Envelope("event", {"name": "model_call"}))  # not a stage -> skipped
    out = buf.getvalue()
    assert "stage review ok" in out and "12ms" in out
    assert out.count("\n") == 1  # only the stage line printed


async def scenario_file_sink_appends() -> None:
    import json as _json
    import os
    import tempfile

    from yaah.adapters.trace import FileTraceSink

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "trace.jsonl")  # parent dir auto-created
        sink = FileTraceSink(path)
        await sink.handle(Envelope("event", {"name": "stage", "corr": "r1"}))
        await sink.handle(Envelope("event", {"name": "model_call", "corr": "r1"}))
        with open(path) as f:
            lines = [_json.loads(x) for x in f if x.strip()]
        assert [l["name"] for l in lines] == ["stage", "model_call"]


async def scenario_langfuse_sink_mapping() -> None:
    from yaah.adapters.trace import LangfuseTraceSink

    class StubLangfuse:
        def __init__(self):
            self.traces, self.spans, self.gens = [], [], []
        def trace(self, **kw):       self.traces.append(kw)
        def span(self, **kw):        self.spans.append(kw)
        def generation(self, **kw):  self.gens.append(kw)

    stub = StubLangfuse()
    sink = LangfuseTraceSink(client=stub)
    stage_rec = {"name": "stage", "corr": "run-1", "id": "s1", "parent": "p0",
                 "stage": "review", "status": "ok", "duration_ms": 5.0}
    model_rec = {"name": "model_call", "corr": "run-1", "id": "m1", "parent": "s1",
                 "model": "claude", "tokens_in": 10, "tokens_out": 2, "status": "ok"}
    await sink.handle(Envelope("event", stage_rec))
    await sink.handle(Envelope("event", model_rec))

    assert len(stub.traces) == 1 and stub.traces[0]["id"] == "run-1"  # one trace, deduped
    assert len(stub.spans) == 1 and stub.spans[0]["name"] == "review"  # stage -> span
    assert len(stub.gens) == 1                                          # model_call -> generation
    gen = stub.gens[0]
    assert gen["model"] == "claude"
    assert gen["usage"] == {"input": 10, "output": 2}                   # Langfuse computes $
    assert gen["trace_id"] == "run-1" and gen["parent_observation_id"] == "s1"
    # a record with no corr is ignored (no crash)
    await sink.handle(Envelope("event", {"name": "stage"}))
    assert len(stub.traces) == 1


async def scenario_langfuse_v4_mapping() -> None:
    # The v4 (OpenTelemetry) client surface: start_observation(as_type=...) -> obs,
    # obs.end(). Detected by capability, so this stub (no .trace) takes the v4 path.
    from yaah.adapters.trace import LangfuseTraceSink

    ended = []

    class _Obs:
        def __init__(self, as_type):
            self.as_type = as_type

        def end(self):
            ended.append(self.as_type)

    class StubV4:
        def __init__(self):
            self.calls = []

        def start_observation(self, **kw):
            self.calls.append(kw)
            return _Obs(kw.get("as_type"))

    stub = StubV4()
    sink = LangfuseTraceSink(client=stub)
    corr = "abcd1234" * 4  # 32-hex -> a valid OTel trace id
    await sink.handle(Envelope("event", {"name": "stage", "corr": corr,
                                         "stage": "review", "status": "ok"}))
    await sink.handle(Envelope("event", {"name": "model_call", "corr": corr,
                                         "model": "claude", "tokens_in": 10, "tokens_out": 2}))

    assert [c["as_type"] for c in stub.calls] == ["span", "generation"]
    span_call, gen_call = stub.calls
    assert span_call["name"] == "review"
    assert span_call["trace_context"] == {"trace_id": corr}
    assert gen_call["model"] == "claude"
    assert gen_call["usage_details"] == {"input": 10, "output": 2}  # Langfuse computes $
    assert gen_call["trace_context"] == {"trace_id": corr}
    assert ended == ["span", "generation"]                          # every obs .end()ed
    # a non-OTel corr -> no trace_context (let the SDK mint its own id), no crash
    stub.calls.clear()
    await sink.handle(Envelope("event", {"name": "stage", "corr": "run-1"}))
    assert "trace_context" not in stub.calls[0]
    # a record with no corr is ignored
    await sink.handle(Envelope("event", {"name": "stage"}))
    assert len(stub.calls) == 1


async def scenario_emit_through_harness() -> None:
    # a one-stage agent pipeline, traced with phase+cost via build(tracer=)
    config = {
        "nodes": {"echo": {"type": "agent", "template": "hi {{x}}", "model": "m", "parse": False}},
        "graph": {"start": "s", "stages": {"s": {"node": "echo"}}},
    }
    tr = RecordingTracer([PhaseContributor(), CostContributor()])
    h = build(config, backend=_UsageBackend(), tracer=tr)
    out = await h.run(Envelope("task", {"x": "there"}))
    assert isinstance(out, Done)
    names = [r["name"] for r in tr.records]
    assert "stage" in names and "model_call" in names
    stage = next(r for r in tr.records if r["name"] == "stage")
    assert stage["stage"] == "s" and stage["status"] == "ok"
    model = next(r for r in tr.records if r["name"] == "model_call")
    assert model["tokens_in"] == 10 and model["tokens_out"] == 3  # cost bridge fired
    # the model_call's parent chains under the run (same corr)
    assert model["corr"] == stage["corr"]


async def scenario_bad_sink_doesnt_abort_run() -> None:
    # H2: a failing trace sink must NOT crash the pipeline run (publish swallows
    # subscriber errors). Subscribe a throwing handler to the trace topic, then run.
    comms = InProcessComms()

    async def boom(env):
        raise RuntimeError("sink is down")

    await comms.subscribe("trace", boom)
    tracer = BusTracer(comms, contributors=[PhaseContributor()])
    config = {
        "nodes": {"echo": {"type": "agent", "template": "x", "model": "m", "parse": False}},
        "graph": {"start": "s", "stages": {"s": {"node": "echo"}}},
    }
    h = build(config, comms=comms, backend=_UsageBackend(), tracer=tracer)
    out = await h.run(Envelope("task", {}))
    assert isinstance(out, Done), "a broken trace sink must not abort the run"


async def scenario_cost_off_skips_gathering() -> None:
    # with only phase enabled, the agent must NOT pass on_usage (no gathering)
    seen = {"on_usage": True}

    class _Probe:
        async def complete(self, prompt, *, model=None, **opts):
            seen["on_usage"] = "on_usage" in opts
            return "done"

    config = {
        "nodes": {"echo": {"type": "agent", "template": "x", "model": "m", "parse": False}},
        "graph": {"start": "s", "stages": {"s": {"node": "echo"}}},
    }
    tr = RecordingTracer([PhaseContributor()])  # cost OFF
    h = build(config, backend=_Probe(), tracer=tr)
    await h.run(Envelope("task", {}))
    assert seen["on_usage"] is False, "cost capture off -> on_usage not gathered"


async def main() -> None:
    await scenario_phase_minimum()
    await scenario_cost_is_orthogonal()
    await scenario_tools_capture()
    await scenario_drain_by_corr()
    await scenario_null_tracer_off()
    await scenario_bus_tracer_publishes()
    await scenario_bus_tracer_ingest_is_capped_and_dict_only()
    await scenario_envelope_tracer_buffers_per_corr_and_drains()
    await scenario_envelope_tracer_caps_buffer_with_truncated_marker()
    await scenario_envelope_tracer_satisfies_tracer_protocol()
    scenario_aggregate()
    await scenario_console_sink()
    await scenario_file_sink_appends()
    await scenario_langfuse_sink_mapping()
    await scenario_langfuse_v4_mapping()
    await scenario_emit_through_harness()
    await scenario_bad_sink_doesnt_abort_run()
    await scenario_cost_off_skips_gathering()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
