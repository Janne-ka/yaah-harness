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
            on_usage({"tokens_in": 10, "tokens_cache_read": 900,
                      "tokens_cache_write": 40, "tokens_out": 3, "model": model})
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


async def scenario_phase_projects_retry_cause() -> None:
    """A stage-error span must project WHY the attempt was rejected — `retry`,
    `attempt`/`n` and a LENGTH-BOUNDED `error`. Without this the trace can say
    that a stage retried but never why, so a run that burned four paid model
    calls on rejected replies is undiagnosable (the M7 ladder_from lesson: an
    attr missing from the projection is dead in real runs)."""
    tr = RecordingTracer([PhaseContributor()])
    bound = PhaseContributor.ERROR_MAX
    long_detail = "not_ok: " + ("x" * (bound + 400))
    await tr.emit(Span(id="e1", corr="run-1", name="stage", parent="p0",
                       duration_ms=12.0, status="error",
                       attrs={"stage": "review", "retry": "feedback",
                              "attempt": 2, "error": long_detail}))
    r = tr.records[-1]
    assert r["status"] == "error" and r["stage"] == "review", r
    assert r["retry"] == "feedback" and r["attempt"] == 2, r
    # bounded: ERROR_MAX chars of the detail + the marker, and the HEAD is kept
    # (the failure code is at the front, where the diagnosis lives)
    assert r["error"].startswith("not_ok: xxx"), r["error"][:40]
    assert r["error"] == long_detail[:bound] + PhaseContributor.ERROR_TRUNCATED_MARKER, r["error"]
    assert len(r["error"]) == bound + len(PhaseContributor.ERROR_TRUNCATED_MARKER), len(r["error"])

    # a short error rides through verbatim — no marker, no clipping. The
    # transient counter is `error_retry_n`; the pre-2026-08 bare `n` is a
    # READ-side concern of the persisted-record parsers (aggregate/pretty), not
    # of this projection — span.attrs is in-process, so nothing can arrive here
    # under the old spelling and projecting it would be dead code.
    await tr.emit(Span(id="e2", corr="run-1", name="stage", status="error",
                       attrs={"retry": "transient", "error_retry_n": 1,
                              "error": "boom: overloaded"}))
    r2 = tr.records[-1]
    assert r2["error"] == "boom: overloaded", r2
    assert r2["retry"] == "transient" and r2["error_retry_n"] == 1, r2
    assert "n" not in r2, r2

    # a normal (non-error) span is unchanged: no retry keys invented
    await tr.emit(_model_span())
    r3 = tr.records[-1]
    assert "error" not in r3 and "retry" not in r3, r3
    assert "error_retry_n" not in r3, r3


def scenario_bounded_is_the_one_truncation_policy() -> None:
    """One helper, one marker, callers own only their limit — so a clipped
    message can never read as a complete one (an unmarked `[:500]` is exactly
    how that happens). PhaseContributor's ERROR_MAX and ClaudeCliProvider's
    stderr tail both go through it."""
    from yaah.trace.bounded_text import TRUNCATED_MARKER, bounded

    assert bounded("short", 10) == "short"          # under the bound: verbatim
    assert bounded("exactly10!", 10) == "exactly10!"  # AT the bound: no marker
    assert bounded("abcdef", 3) == "abc" + TRUNCATED_MARKER   # head kept + marked
    assert bounded(12345, 3) == "123" + TRUNCATED_MARKER      # non-str coerced
    # the phase capture publishes the same marker, so the two can't drift apart
    assert PhaseContributor.ERROR_TRUNCATED_MARKER == TRUNCATED_MARKER

    # keep="tail" for the sources whose diagnosis is at the END (a subprocess's
    # stderr: the banner scrolls past, the fatal line is written last). The
    # marker sits AT the cut, so it leads here instead of trailing.
    assert bounded("abcdef", 3, keep="tail") == TRUNCATED_MARKER + "def"
    assert bounded("short", 10, keep="tail") == "short"   # under the bound: verbatim


async def scenario_harness_retry_cause_reaches_the_record() -> None:
    """e2e over the real attempt loop: the harness's own error note must arrive
    at a sink as a projected record, not sit dead in span.attrs."""
    from yaah.core import Failure, NodeConfig, Verdict
    from yaah.harness import Graph, Stage, Suspended

    class _Nope:
        async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
            return input.reply("result", text="nope")

    class _AlwaysFails:
        async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
            return Verdict.failed(  # longer than PhaseContributor.ERROR_MAX,
                # so the projection's truncation is exercised end-to-end
                Failure("not_ok", "y" * (PhaseContributor.ERROR_MAX + 400),
                        "fix it")).to_envelope()

    comms = InProcessComms()
    comms.register("role:nope", _Nope())
    comms.register("role:check", _AlwaysFails())
    tr = RecordingTracer([PhaseContributor()])
    from yaah.harness import Harness
    h = Harness(comms, Graph.of(
        Stage("review", node="role:nope", validators=["role:check"],
              max_attempts=2, feedback=True, escalate="human")), tracer=tr)
    outcome = await h.run(Envelope("task", {}))
    assert isinstance(outcome, Suspended), outcome

    errs = [r for r in tr.records if r.get("name") == "stage" and r.get("status") == "error"]
    assert errs, "the rejected attempt must be traced: {!r}".format(tr.records)
    e = errs[0]
    assert e["stage"] == "review" and e["retry"] == "feedback" and e["attempt"] == 1, e
    assert e["error"].startswith("not_ok: yyy"), e["error"][:40]
    assert e["error"].endswith(PhaseContributor.ERROR_TRUNCATED_MARKER), e["error"][-40:]


async def scenario_emitter_records_artifact_from_path() -> None:
    # Y2: when a stage's output payload carries a generic `path` (the render
    # node's rendered-artifact key), the stage span records `artifact` as the
    # basename — so the progress sink can point an operator at what to open.
    from yaah.harness.span_emitter import SpanEmitter

    tr = RecordingTracer([PhaseContributor()])
    em = SpanEmitter(tr, clock=lambda: 1.0)
    inp = Envelope("task", {})
    out = Envelope("task", {"path": "/tmp/runs/report.html"})
    await em.stage("review", inp, 0.0, status="suspended",
                   output=out, awaiting="human:gate")
    span = tr.spans[-1]
    assert span.attrs["artifact"] == "report.html", span.attrs    # basename only
    assert span.attrs["awaiting"] == "human:gate", span.attrs     # unperturbed
    # e2e CONTRACT: the PROJECTED record — what ProgressFileSink actually receives —
    # must carry awaiting + artifact. The phase contributor must project them, not just
    # leave them in span.attrs. (This also covers a latent bug: `awaiting` was dropped
    # by the projection too, so the inline awaiting= feature was dead in real runs.)
    rec = tr.records[-1]
    assert rec.get("artifact") == "report.html", rec
    assert rec.get("awaiting") == "human:gate", rec

    # no `path` in the output -> no artifact attr (byte-identical to today)
    out2 = Envelope("task", {})
    await em.stage("review", inp, 0.0, status="ok", output=out2)
    assert "artifact" not in tr.spans[-1].attrs, tr.spans[-1].attrs


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
    # a call with NO cache usage still carries both keys, as zeros — see
    # scenario_cost_always_emits_cache_classes for why the zeros must be there
    assert r["tokens_cache_read"] == 0 and r["tokens_cache_write"] == 0, r


async def scenario_cost_always_emits_cache_classes() -> None:
    """The three INPUT token classes reach the record separately (they bill at
    different rates, so a lumped `tokens_in` is unpriceable) — and the two cache
    keys are emitted ALWAYS, zeros included.

    That is the load-bearing part: absence is the ONLY marker of a pre-split
    record, whose cost is an unpriceable upper bound. Emit zeros conditionally
    and a no-cache call is byte-identical to a pre-split one, so aggregate mixes
    exact and upper-bound costs with nothing to tell them apart."""
    tr = RecordingTracer([CostContributor()])
    await tr.emit(Span(id="m2", corr="run-1", name="model_call",
                       tokens_in=800, tokens_cache_read=40000,
                       tokens_cache_write=2000, tokens_out=120,
                       model="claude:sonnet", status="ok"))
    r = tr.records[-1]
    assert r["tokens_in"] == 800, r
    assert r["tokens_cache_read"] == 40000 and r["tokens_cache_write"] == 2000, r

    # a genuinely cache-free call: the keys are PRESENT and zero, so the record
    # is distinguishable from one written before the split
    await tr.emit(Span(id="m3", corr="run-1", name="model_call",
                       tokens_in=800, tokens_out=120, model="claude:sonnet"))
    r2 = tr.records[-1]
    assert r2["tokens_cache_read"] == 0 and r2["tokens_cache_write"] == 0, r2


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
        {"name": "stage", "corr": "r1", "stage": "code", "duration_ms": 50.0,
         # `n` = the PRE-2026-08 spelling, as it sits in an archived trace file;
         # aggregate reads it and reports it under the current name
         "status": "error", "retry": "feedback", "attempt": 1, "n": 2,
         "error": "not_ok: missing summary"},
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
    # the error ENTRY carries the retry cause through, so a consumer of the JSON
    # can tell N rejected attempts of one stage from N separate stage failures.
    # The legacy `n` above lands under the current name, `error_retry_n`.
    e = agg["errors"][0]
    assert e["stage"] == "code" and e["retry"] == "feedback", e
    assert e["attempt"] == 1 and e["error_retry_n"] == 2, e
    assert e["detail"] == "not_ok: missing summary", e
    # every model_call here predates the cache split -> all four are upper bounds
    assert agg["totals"]["unpriced_upper_bound_calls"] == 4, agg["totals"]
    assert abs(agg["runs"]["r1"]["cost_usd"] - 4.0) < 1e-9
    # per-stage latency percentiles (spec ran in both runs: [100, 200])
    assert agg["stages"]["spec"]["count"] == 2
    assert agg["stages"]["spec"]["p50_ms"] == 150.0 and agg["stages"]["spec"]["max_ms"] == 200.0
    assert agg["tools"]["grep"] == 2
    assert agg["models"]["m1"]["calls"] == 3 and agg["models"]["m2"]["calls"] == 1


def scenario_cache_classes_priced_at_their_own_rates() -> None:
    """A cache-heavy stage must NOT be priced as if every input token were fresh.
    The cache classes bill at their own multiples of the input rate (see
    yaah.trace.aggregate); summing the three classes into `tokens_in` (the
    pre-2026-08 provider behaviour) and pricing that at the full input rate
    inflated long agentic stages several-fold — the exact axis cost rankings are
    made on."""
    from yaah.trace.aggregate import aggregate, cost_usd

    price = {"m1": {"input": 3.0, "output": 15.0}}   # $/1k
    # 1k fresh + 100k cache-read + 10k cache-write + 1k out
    fresh, read, write, out = 1000, 100000, 10000, 1000
    got = cost_usd("m1", fresh, out, price,
                   tokens_cache_read=read, tokens_cache_write=write)
    expected = 3.0 + 100 * 0.3 + 10 * 3.75 + 15.0        # 3 + 30 + 37.5 + 15
    assert abs(got - expected) < 1e-9, got
    # the OLD lumped arithmetic (all 111k input at the full rate) is 3.5x higher
    lumped = cost_usd("m1", fresh + read + write, out, price)
    assert abs(lumped - (333.0 + 15.0)) < 1e-9, lumped
    assert lumped > got * 3, (lumped, got)

    # a map may state explicit per-1k cache rates instead of the derived ones
    explicit = {"m1": {"input": 3.0, "output": 15.0,
                       "cache_read": 0.3, "cache_write": 6.0}}   # 1h-TTL write = 2x
    assert abs(cost_usd("m1", fresh, out, explicit,
                        tokens_cache_read=read, tokens_cache_write=write)
               - (3.0 + 30.0 + 60.0 + 15.0)) < 1e-9

    # BACK-COMPAT: a pre-split record carries no cache fields -> priced exactly
    # as before (everything in tokens_in at the full input rate; an upper bound)
    old = [{"name": "model_call", "corr": "r1", "model": "m1",
            "tokens_in": fresh + read + write, "tokens_out": out}]
    old_totals = aggregate(old, price_map=price)["totals"]
    assert abs(old_totals["cost_usd"] - lumped) < 1e-9
    # ...and the rollup SAYS the figure is an upper bound rather than mixing it
    # in silently — absence of the cache keys is the only marker there is
    assert old_totals["unpriced_upper_bound_calls"] == 1, old_totals

    # ...and a split record prices at the corrected total, with the cache classes
    # rolled up so the operator can see the hit rate behind the $ figure
    new = [{"name": "model_call", "corr": "r1", "model": "m1",
            "tokens_in": fresh, "tokens_cache_read": read,
            "tokens_cache_write": write, "tokens_out": out}]
    t = aggregate(new, price_map=price)["totals"]
    assert abs(t["cost_usd"] - expected) < 1e-9, t
    assert t["tokens_in"] == fresh, t                      # uncached input only
    assert t["tokens_cache_read"] == read and t["tokens_cache_write"] == write, t
    assert t["unpriced_upper_bound_calls"] == 0, t         # every call exact

    # a genuinely cache-free POST-split call (explicit zeros) is exact, NOT an
    # upper bound — the distinction the always-emit rule buys
    nocache = [{"name": "model_call", "corr": "r1", "model": "m1",
                "tokens_in": fresh, "tokens_cache_read": 0,
                "tokens_cache_write": 0, "tokens_out": out}]
    assert aggregate(nocache, price_map=price)["totals"]["unpriced_upper_bound_calls"] == 0


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

    # A CACHE-BEARING record on the v2 path: the legacy client's `usage` is a
    # TYPED model with only input/output/total, so the cache classes are FOLDED
    # INTO input rather than posted under anthropic key names it has no field
    # for. Lossy upward (a cache read bills at the full input rate here) — the
    # same upper-bound convention pre-split traces get, and the direction that
    # doesn't understate. v4 carries the split properly.
    await sink.handle(Envelope("event",
                              {"name": "model_call", "corr": "run-1", "id": "m2",
                               "parent": "s1", "model": "claude", "tokens_in": 1000,
                               "tokens_cache_read": 100000, "tokens_cache_write": 2000,
                               "tokens_out": 300, "status": "ok"}))
    cached_gen = stub.gens[-1]
    assert cached_gen["usage"] == {"input": 103000, "output": 300}, cached_gen
    # no anthropic-dialect key reaches the typed v2 model
    assert set(cached_gen["usage"]) == {"input", "output"}, cached_gen


def scenario_usage_vocabulary_is_shared() -> None:
    """The four-field usage payload is ONE named shape (api_provider.Usage +
    USAGE_TOKEN_KEYS), not four hand-spelled dict literals. A provider that
    invents `tokens_cache_reads` prices tokens at $0 forever with zero errors,
    so every consumer of the shape derives its keys from the one list."""
    from yaah.adapters.trace.langfuse_trace_sink import (
        _INPUT_TOKEN_KEYS, _V4_USAGE_KEYS, _usage_details_v4, _usage_v2)
    from yaah.agents.api_provider import USAGE_TOKEN_KEYS, Usage

    # the type documents exactly the accumulated keys, plus `model`
    assert set(Usage.__annotations__) == set(USAGE_TOKEN_KEYS) | {"model"}
    # the Langfuse translation covers every class — a new one can't go missing
    assert set(_V4_USAGE_KEYS) == set(USAGE_TOKEN_KEYS), _V4_USAGE_KEYS
    # both shipped real backends report under exactly these names
    from yaah.adapters.providers.claude_cli_provider import _map_usage
    mapped = _map_usage({"input_tokens": 1, "cache_read_input_tokens": 2,
                         "cache_creation_input_tokens": 3, "output_tokens": 4}, "m")
    assert set(mapped) == set(USAGE_TOKEN_KEYS) | {"model"}, mapped

    # ...and the two sink builders ITERATE that vocabulary rather than naming
    # classes inline. A record carrying every class must produce every v4 key —
    # a hardcoded loop would pass the coverage assert above while still dropping
    # the new class from every generation, which is the whole failure mode.
    full = dict.fromkeys(USAGE_TOKEN_KEYS, 7)
    v4 = _usage_details_v4(full)
    assert set(v4) == set(_V4_USAGE_KEYS.values()), v4
    assert all(n == 7 for n in v4.values()), v4
    # v2 has no cache fields, so EVERY input class folds into `input` — a fifth
    # class must join the sum, not vanish
    assert _usage_v2(full) == {"input": 7 * len(_INPUT_TOKEN_KEYS), "output": 7}
    assert set(_INPUT_TOKEN_KEYS) == set(USAGE_TOKEN_KEYS) - {"tokens_out"}


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

    # A CACHE-BEARING record on the v4 path: usage_details is an OPEN key->count
    # map, so the two cache classes travel under Langfuse's own anthropic-dialect
    # names and Langfuse prices each at its own rate. Folding them into `input`
    # (what the v2 path must do) would over-report a cache-heavy run's spend.
    await sink.handle(Envelope("event",
                              {"name": "model_call", "corr": corr, "model": "claude",
                               "tokens_in": 1000, "tokens_cache_read": 100000,
                               "tokens_cache_write": 2000, "tokens_out": 300}))
    cached = stub.calls[-1]["usage_details"]
    assert cached == {"input": 1000, "output": 300,
                      "cache_read_input_tokens": 100000,
                      "cache_creation_input_tokens": 2000}, cached
    # explicit zeros (every post-split record carries them) add no noise keys
    await sink.handle(Envelope("event",
                              {"name": "model_call", "corr": corr, "model": "claude",
                               "tokens_in": 5, "tokens_cache_read": 0,
                               "tokens_cache_write": 0, "tokens_out": 1}))
    assert stub.calls[-1]["usage_details"] == {"input": 5, "output": 1}, stub.calls[-1]
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
    # the cached-input classes survive the bridge -> span -> record path too
    assert model["tokens_cache_read"] == 900, model
    assert model["tokens_cache_write"] == 40, model
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
    await scenario_phase_projects_retry_cause()
    scenario_bounded_is_the_one_truncation_policy()
    await scenario_harness_retry_cause_reaches_the_record()
    await scenario_emitter_records_artifact_from_path()
    await scenario_cost_is_orthogonal()
    await scenario_cost_always_emits_cache_classes()
    await scenario_tools_capture()
    await scenario_drain_by_corr()
    await scenario_null_tracer_off()
    await scenario_bus_tracer_publishes()
    await scenario_bus_tracer_ingest_is_capped_and_dict_only()
    await scenario_envelope_tracer_buffers_per_corr_and_drains()
    await scenario_envelope_tracer_caps_buffer_with_truncated_marker()
    await scenario_envelope_tracer_satisfies_tracer_protocol()
    scenario_aggregate()
    scenario_cache_classes_priced_at_their_own_rates()
    await scenario_console_sink()
    await scenario_file_sink_appends()
    await scenario_langfuse_sink_mapping()
    await scenario_langfuse_v4_mapping()
    scenario_usage_vocabulary_is_shared()
    await scenario_emit_through_harness()
    await scenario_bad_sink_doesnt_abort_run()
    await scenario_cost_off_skips_gathering()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
