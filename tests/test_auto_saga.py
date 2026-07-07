"""ADR-0009 auto-saga — slices 1 (config+arm cross-check) and 2 (executor + hook).

These FALSIFY the spec against the real downstream shapes (a REAL FileTraceSink
round-trip, the REAL rollback.execute executor, a REAL BusTracer projection):

  Slice 1 (saga.check_on_failure_value / saga.check_rollback_trace_sink):
    - value grammar: accepts "rollback" and {mode, include_costly}; rejects a bad
      string, a bad mode, a non-bool include_costly, and an unknown object key.
    - widened cross-check (root + PIPELINE, so it can see graph.on_failure):
      armed-with-no-rollback-node is an ERROR; armed + file sink is fine; armed +
      declaring node + no file sink is an ERROR (subsumed by the declaring path);
      an unarmed pipeline with a declaring node still requires the sink; the
      existing "rollback input" wording is preserved (so the run-path parity test
      in test_rollback_record keeps passing under the widened impl).

  Slice 2 (saga.run_auto_saga / saga.settle_terminal):
    - the runtime-boundary hook re-raises the ORIGINAL StageFailed (object
      identity) after arming; Done/Suspended/Cleared pass through untouched (the
      saga fires on StageFailed and NOTHING else — invariant 1).
    - an END-TO-END real harness run (stage A commits an effect, stage B fails
      ungated) → the saga undoes A, writes a `saga` record, re-raises.
    - cheap-only default vs include_costly.
    - a failing undo → re-raise + report with `failed` + `not_attempted` + a LOUD
      stderr pointer to `yaah rollback`.
    - idempotency: the `saga` record's buckets round-trip through the REAL file
      sink (falsifies the phase-whitelist add), and a second saga EXCLUDES what a
      prior saga already rolled back (retried-resume; keyed by (stage,occurrence)).
    - a looped stage (occ 1+2 rolled back, occ 3 post-resume) undoes only occ 3.
    - compensation_failed in the terminal verdict → saga SKIPPED loudly (record
      marks the skip, empty buckets, no undo call); absent code (the warn path)
      → the saga runs.
    - corr-guard: output None, and a corr absent from the trace, both SKIP loudly
      and undo nothing.

Run: cd yaah && PYTHONPATH=src python3 tests/test_auto_saga.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout

from yaah import Envelope, Graph, Harness, InProcessComms, Stage, StageFailed
from yaah.core import Failure, Kind, Verdict
from yaah import rollback as rb
from yaah import saga
from yaah.trace import BusTracer
from yaah.trace.contributors.phase import PhaseContributor

# The adapter sink is not re-exported at yaah.trace top-level; import directly.
from yaah.adapters.trace.file_trace_sink import FileTraceSink


# ---------- fn: undo targets (a live module call_target imports by name) -----

_MOD = types.ModuleType("sagatest_targets")
_CALLS: list = []


def _record_undo(ctx):
    _CALLS.append(dict(ctx))
    return {"undone": ctx.get("stage")}


def _boom_undo(ctx):
    _CALLS.append(dict(ctx))
    raise RuntimeError("undo endpoint down for {}".format(ctx.get("stage")))


def _emit_handle(args):
    """A transform target committing an 'effect': returns the handle the stage's
    effects_from records (the run_root end-to-end fixture)."""
    return "H-e2e"


def _blow_up(args):
    """A transform target that dies NON-transiently (no overload/timeout/lock
    words), so the harness fails the stage without spending error_retries."""
    raise RuntimeError("hard domain fault")


_MOD._record_undo = _record_undo
_MOD._boom_undo = _boom_undo
_MOD._emit_handle = _emit_handle
_MOD._blow_up = _blow_up
sys.modules["sagatest_targets"] = _MOD

_REC = "fn:sagatest_targets:_record_undo"
_BOOM = "fn:sagatest_targets:_boom_undo"


def _reset():
    _CALLS.clear()


# ---------- fixtures ---------------------------------------------------------

def _clock():
    """A monotonic-ish clock; two reads differ so a saga span is never a point
    span (irrelevant to name=="saga", but keeps timestamps honest)."""
    _clock.t += 1.0
    return _clock.t


_clock.t = 0.0


class _FakeHarness:
    """The two attributes the saga reaches for: a tracer (to emit the `saga`
    record through the SAME file the executor reads) and a clock."""

    def __init__(self, tracer):
        self._tracer = tracer
        self._clock = _clock


async def _file_harness(path):
    """A harness stub whose BusTracer publishes projected records to a real
    FileTraceSink over InProcessComms — the true emission path."""
    comms = InProcessComms()
    await comms.subscribe("trace", FileTraceSink(path).handle)
    tracer = BusTracer(comms, contributors=[PhaseContributor()])
    return _FakeHarness(tracer), comms, tracer


async def _write_stage_records(tracer, corr, specs):
    """Emit real `stage` completion spans (status ok, non-point) through the
    tracer so the file sink writes them exactly as a run would. Each spec:
    (stage, effects) — effects None means no effects attr."""
    from yaah.trace.span import Span
    for stage, effects in specs:
        attrs = {"stage": stage}
        if effects is not None:
            attrs["effects"] = effects
        t0 = _clock()
        span = Span.timed("stage", corr=corr, t0=t0, t1=t0 + 1.0,
                          status="ok", attrs=attrs)
        await tracer.emit(span)


def _root_with_pipeline(path, stage_to_rb, on_failure="rollback"):
    """A root dict with an INLINE pipeline: each stage -> a node; a non-None
    rollback block is attached to the node. graph.on_failure arms the saga."""
    nodes = {}
    stages = {}
    for stage, rb_block in stage_to_rb.items():
        node_id = "n_" + stage
        stages[stage] = {"node": node_id}
        nodes[node_id] = {"type": "transform"}
        if rb_block is not None:
            nodes[node_id]["rollback"] = rb_block
    graph = {"start": next(iter(stages)) if stages else "s", "stages": stages}
    if on_failure is not None:
        graph["on_failure"] = on_failure
    return {"pipeline": {"nodes": nodes, "graph": graph},
            "trace": {"sinks": [{"type": "file", "path": os.path.basename(path)}]}}


def _fail(corr, *codes, output=True):
    verdict = Verdict.failed(*(Failure(c, c) for c in (codes or ("boom",))))
    out = Envelope(Kind.RESULT, {}, {"correlation_id": corr}) if output else None
    return StageFailed("workstage", verdict, out)


# ============================ SLICE 1 =======================================

def test_value_grammar_accepts_and_rejects() -> None:
    assert saga.check_on_failure_value(None) == []
    assert saga.check_on_failure_value("rollback") == []
    assert saga.check_on_failure_value({"mode": "rollback"}) == []
    assert saga.check_on_failure_value(
        {"mode": "rollback", "include_costly": True}) == []
    # bad string
    assert saga.check_on_failure_value("undo"), "bad mode string must error"
    # bad mode in object
    assert saga.check_on_failure_value({"mode": "saga"}), "bad object mode must error"
    # non-bool include_costly
    assert saga.check_on_failure_value(
        {"mode": "rollback", "include_costly": "yes"}), "non-bool must error"
    # unknown key
    assert saga.check_on_failure_value(
        {"mode": "rollback", "cost": "cheap"}), "unknown key must error"
    # wrong type
    assert saga.check_on_failure_value(["rollback"]), "list must error"


def test_widened_cross_check() -> None:
    file_sink = {"trace": {"sinks": [{"type": "file"}]}}
    console = {"trace": {"sinks": [{"type": "console"}]}}

    # armed but NO node declares rollback -> ERROR (self-contradictory, eval #3)
    pl = {"nodes": {"a": {"type": "transform"}},
          "graph": {"start": "s", "stages": {"s": {"node": "a"}},
                    "on_failure": "rollback"}}
    errs = saga.check_rollback_trace_sink(dict(file_sink), pl)
    assert errs and any("declares" in e or "no node" in e.lower() for e in errs), errs

    # armed + a declaring node + a file sink -> OK
    pl_ok = {"nodes": {"a": {"type": "transform", "rollback": {"target": "fn:u"}}},
             "graph": {"start": "s", "stages": {"s": {"node": "a"}},
                       "on_failure": "rollback"}}
    assert saga.check_rollback_trace_sink(dict(file_sink), pl_ok) == [], "armed+sink OK"

    # armed + declaring node + NO file sink -> ERROR (preserves "rollback input")
    errs2 = saga.check_rollback_trace_sink(dict(console), pl_ok)
    assert errs2 and any("rollback input" in e for e in errs2), errs2

    # UNARMED pipeline with a declaring node still requires the sink (parity with
    # the existing rollback-node check that runtime.test_rollback_record drives)
    pl_unarmed = {"nodes": {"a": {"type": "transform",
                                  "rollback": {"target": "fn:u"}}},
                  "graph": {"start": "s", "stages": {"s": {"node": "a"}}}}
    errs3 = saga.check_rollback_trace_sink(dict(console), pl_unarmed)
    assert errs3 and any("rollback input" in e for e in errs3), errs3
    # ...and passes with a file sink
    assert saga.check_rollback_trace_sink(dict(file_sink), pl_unarmed) == []

    # no rollback anywhere + unarmed -> no requirement at all
    plain = {"nodes": {"a": {"type": "transform"}},
             "graph": {"start": "s", "stages": {"s": {"node": "a"}}}}
    assert saga.check_rollback_trace_sink({}, plain) == []

    # a TYPO'D on_failure value is an ERROR from the widened helper itself — the
    # RUN path's only grammar surface until the validate.py diff lands; a bad
    # value must never silently UN-arm a saga the author believes is armed.
    pl_typo = {"nodes": {"a": {"type": "transform",
                               "rollback": {"target": "fn:u"}}},
               "graph": {"start": "s", "stages": {"s": {"node": "a"}},
                         "on_failure": "undo"}}
    errs4 = saga.check_rollback_trace_sink(dict(file_sink), pl_typo)
    assert errs4 and any("on_failure" in e for e in errs4), errs4


def test_widened_check_rejects_nonpersisting_mode() -> None:
    # a declared file sink under mode none/envelope is DEAD weight (parity with
    # the shipped rollback-node check).
    pl = {"nodes": {"a": {"type": "transform", "rollback": {"target": "fn:u"}}},
          "graph": {"start": "s", "stages": {"s": {"node": "a"}},
                    "on_failure": "rollback"}}
    for mode in ("none", "envelope"):
        errs = saga.check_rollback_trace_sink(
            {"trace": {"mode": mode, "sinks": [{"type": "file"}]}}, pl)
        assert errs and any(mode in e for e in errs), (mode, errs)


# ============================ SLICE 2 =======================================

def test_hook_reraises_original_and_passes_through() -> None:
    async def go():
        h = _FakeHarness(None)
        root = {"pipeline": {"nodes": {}, "graph": {"start": "s", "stages": {}}}}
        # Done passes straight through
        done = object()

        async def ok_coro():
            return done
        assert await saga.settle_terminal(root, ".", h, ok_coro()) is done

        # a StageFailed with an UNARMED graph re-raises the SAME object, no saga
        exc = _fail("RID")

        async def fail_coro():
            raise exc
        try:
            await saga.settle_terminal(root, ".", h, fail_coro())
            raise AssertionError("must re-raise")
        except StageFailed as got:
            assert got is exc, "must re-raise the ORIGINAL StageFailed object"
    asyncio.run(go())


def test_no_fire_on_suspended_or_cleared() -> None:
    """Invariant 1: the saga fires on StageFailed and NOTHING else. Driven with
    the REAL outcome types (Suspended/Cleared/Done), not stand-ins, so a future
    isinstance-based arm point could not silently pass this test."""
    from yaah.harness import Cleared, Done, Suspended

    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            h, comms, tracer = await _file_harness(path)
            # arm the graph AND give the run trace records so the saga WOULD have
            # something to undo if it (wrongly) fired — only the outcome type
            # gates it.
            root = _root_with_pipeline(path, {"s": {"target": _REC}})
            await _write_stage_records(tracer, "RID", [("s", "S1")])
            outcomes = (
                Suspended(baton_id="b1", awaiting="human:review"),
                Cleared(baton_id="b2", node="s"),
                Done(baton_id="b3", output=Envelope(Kind.RESULT, {})),
            )
            for outcome in outcomes:
                async def coro(o=outcome):
                    return o
                got = await saga.settle_terminal(root, d, h, coro())
                assert got is outcome
            assert _CALLS == [], "the saga must not undo on a non-StageFailed"
            assert _read_saga(path) == [], "no saga record on a non-StageFailed"
    asyncio.run(go())


def test_end_to_end_undo_and_record() -> None:
    """A real harness: stage 'commit' succeeds emitting an effect handle, stage
    'work' fails ungated -> StageFailed -> the saga undoes 'commit' and writes a
    saga record; the ORIGINAL failure re-raises."""
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            comms = InProcessComms()
            comms.register("role:commit", _OkEffector("handle", "H1"))
            comms.register("role:work", _Writer())
            comms.register("role:check", _FailValidator())
            await comms.subscribe("trace", FileTraceSink(path).handle)
            tracer = BusTracer(comms, contributors=[PhaseContributor()])
            graph = Graph.of(
                Stage("commit", id="commitnode", node="role:commit",
                      effects_from="handle", then="work"),
                Stage("work", id="worknode", node="role:work",
                      validators=["role:check"], max_attempts=1))
            h = Harness(comms, graph, tracer=tracer)
            root = {"pipeline": {"nodes": {
                        "commitnode": {"type": "transform",
                                       "rollback": {"target": _REC}},
                        "worknode": {"type": "transform"}},
                    "graph": {"start": "commit", "on_failure": "rollback",
                              "stages": {
                                  "commit": {"node": "commitnode",
                                             "effects_from": "handle",
                                             "then": "work"},
                                  "work": {"node": "worknode"}}}},
                    "trace": {"sinks": [{"type": "file", "path": "trace.jsonl"}]}}
            task = Envelope(Kind.TASK, {}, {"correlation_id": "RID"})
            buf = io.StringIO()
            raised = False
            try:
                with redirect_stdout(buf):
                    await saga.settle_terminal(root, d, h, h.run(task))
            except StageFailed:
                raised = True
            assert raised, "the original failure must still surface"
            # the undo ran for 'commit' with the recorded effect handle
            assert len(_CALLS) == 1, _CALLS
            assert _CALLS[0]["stage"] == "commit", _CALLS
            assert _CALLS[0]["effects"] == "H1", _CALLS[0]
            # a saga record round-tripped through the REAL file sink with buckets
            sagas = _read_saga(path)
            assert len(sagas) == 1, sagas
            rolled = sagas[0].get("rolled_back")
            assert rolled and rolled[0]["stage"] == "commit", sagas[0]
    asyncio.run(go())


def test_cheap_only_default_and_include_costly() -> None:
    async def go():
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            # a costly node + a cheap node
            root = _root_with_pipeline(path, {
                "cheapstage": {"target": _REC, "cost": "cheap"},
                "costlystage": {"target": _REC, "cost": "costly"}})
            # default: costly skipped
            _reset()
            h, comms, tracer = await _file_harness(path)
            await _write_stage_records(tracer, "RID", [
                ("cheapstage", "C1"), ("costlystage", "K1")])
            rep = await _silent_saga(root, d, h, _fail("RID"))
            assert [e["stage"] for e in rep["rolled_back"]] == ["cheapstage"], rep
            assert [e["stage"] for e in rep["skipped_costly"]] == ["costlystage"], rep
            assert len(_CALLS) == 1 and _CALLS[0]["stage"] == "cheapstage"

            # include_costly: both undone
            os.remove(path)
            _reset()
            root2 = _root_with_pipeline(path, {
                "cheapstage": {"target": _REC, "cost": "cheap"},
                "costlystage": {"target": _REC, "cost": "costly"}},
                on_failure={"mode": "rollback", "include_costly": True})
            h2, comms2, tracer2 = await _file_harness(path)
            await _write_stage_records(tracer2, "RID", [
                ("cheapstage", "C1"), ("costlystage", "K1")])
            rep2 = await _silent_saga(root2, d, h2, _fail("RID"))
            assert sorted(e["stage"] for e in rep2["rolled_back"]) == \
                ["cheapstage", "costlystage"], rep2
            assert rep2["skipped_costly"] == [], rep2
    asyncio.run(go())


def test_partial_undo_reraises_with_pointer() -> None:
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            # reverse order: 'late' undone first (BOOM -> fails), 'early' not attempted
            root = _root_with_pipeline(path, {
                "early": {"target": _REC},
                "late": {"target": _BOOM}})
            h, comms, tracer = await _file_harness(path)
            await _write_stage_records(tracer, "RID", [("early", "E1"), ("late", "L1")])
            errbuf, outbuf = io.StringIO(), io.StringIO()
            exc = _fail("RID")
            with redirect_stderr(errbuf), redirect_stdout(outbuf):
                rep = await saga.run_auto_saga(root, d, h, exc)
            assert rep is not None
            assert [e["stage"] for e in rep["failed"]] == ["late"], rep
            assert [e["stage"] for e in rep["not_attempted"]] == ["early"], rep
            # LOUD stderr pointer to the manual verb (D3)
            err = errbuf.getvalue()
            assert "yaah rollback" in err and "RID" in err, err
            # the saga record persisted the partial buckets
            sagas = _read_saga(path)
            assert sagas and [e["stage"] for e in sagas[0]["failed"]] == ["late"], sagas
    asyncio.run(go())


def test_idempotent_exclusion_on_retried_resume() -> None:
    """First saga rolls back A,B and records it; a later stage C completes; the
    second saga EXCLUDES A,B (from the prior saga record round-tripped through the
    REAL file sink) and undoes only C."""
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            root = _root_with_pipeline(path, {
                "A": {"target": _REC}, "B": {"target": _REC}, "C": {"target": _REC}})
            h, comms, tracer = await _file_harness(path)
            await _write_stage_records(tracer, "RID", [("A", "a"), ("B", "b")])
            rep1 = await _silent_saga(root, d, h, _fail("RID"))
            assert sorted(e["stage"] for e in rep1["rolled_back"]) == ["A", "B"], rep1
            first_calls = [c["stage"] for c in _CALLS]
            assert sorted(first_calls) == ["A", "B"], first_calls

            # a retried resume: a NEW completion of C appends to the SAME file
            await _write_stage_records(tracer, "RID", [("C", "c")])
            _reset()
            rep2 = await _silent_saga(root, d, h, _fail("RID"))
            assert [e["stage"] for e in rep2["rolled_back"]] == ["C"], rep2
            assert [c["stage"] for c in _CALLS] == ["C"], _CALLS
    asyncio.run(go())


def test_looped_stage_occurrences() -> None:
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            root = _root_with_pipeline(path, {"loop": {"target": _REC}})
            h, comms, tracer = await _file_harness(path)
            # loop ran twice
            await _write_stage_records(tracer, "RID", [("loop", "1"), ("loop", "2")])
            rep1 = await _silent_saga(root, d, h, _fail("RID"))
            occ1 = sorted(e["occurrence"] for e in rep1["rolled_back"])
            assert occ1 == [1, 2], rep1
            # third run post-resume
            await _write_stage_records(tracer, "RID", [("loop", "3")])
            _reset()
            rep2 = await _silent_saga(root, d, h, _fail("RID"))
            assert [e["occurrence"] for e in rep2["rolled_back"]] == [3], rep2
            assert len(_CALLS) == 1 and _CALLS[0]["effects"] == "3", _CALLS
    asyncio.run(go())


def test_compensation_failed_skips_loudly() -> None:
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            root = _root_with_pipeline(path, {"a": {"target": _REC}})
            h, comms, tracer = await _file_harness(path)
            await _write_stage_records(tracer, "RID", [("a", "A1")])
            errbuf = io.StringIO()
            exc = _fail("RID", "boom", "compensation_failed")
            with redirect_stderr(errbuf):
                rep = await saga.run_auto_saga(root, d, h, exc)
            assert rep is None, "a compensation_failed skip does not run an unwind"
            assert _CALLS == [], "no undo when local compensate is in unknown state"
            assert "compensation_failed" in errbuf.getvalue(), errbuf.getvalue()
            # a saga record marks the skip with empty buckets
            sagas = _read_saga(path)
            assert sagas and sagas[0].get("skipped") == "compensation_failed", sagas
            assert sagas[0]["rolled_back"] == [], sagas

            # the WARN path: the code is ABSENT from the verdict -> saga runs
            os.remove(path)
            _reset()
            h2, comms2, tracer2 = await _file_harness(path)
            await _write_stage_records(tracer2, "RID", [("a", "A1")])
            rep2 = await _silent_saga(root, d, h2, _fail("RID", "boom"))
            assert rep2 and [e["stage"] for e in rep2["rolled_back"]] == ["a"], rep2
    asyncio.run(go())


def test_corr_guard_skips() -> None:
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            root = _root_with_pipeline(path, {"a": {"target": _REC}})
            h, comms, tracer = await _file_harness(path)
            await _write_stage_records(tracer, "RID", [("a", "A1")])

            # output None -> skip loudly, undo nothing
            errbuf = io.StringIO()
            with redirect_stderr(errbuf):
                rep = await saga.run_auto_saga(root, d, h, _fail("RID", output=False))
            assert rep is None and _CALLS == [], _CALLS
            assert errbuf.getvalue().strip(), "must name why it skipped"

            # corr absent from the trace -> skip loudly
            errbuf2 = io.StringIO()
            with redirect_stderr(errbuf2):
                rep2 = await saga.run_auto_saga(root, d, h, _fail("GHOST"))
            assert rep2 is None and _CALLS == [], _CALLS
            assert "GHOST" in errbuf2.getvalue(), errbuf2.getvalue()
    asyncio.run(go())


def test_entrypoints_route_through_settle_terminal() -> None:
    """The wiring claim (D6): run_root, resume_gate AND the MCP run tool route
    their terminal call through saga.settle_terminal — with the right (root,
    base) so the saga can resolve the pipeline and trace. A spy over the real
    config path proves the wiring without a config-level StageFailed (the
    behaviour ON one is covered by test_run_root_armed_end_to_end and the direct
    settle_terminal tests)."""
    import yaah.runtime as rt
    from yaah.adapters.mcp_server import tools as mcp_tools
    from yaah.harness import Done, Suspended

    calls = []
    real = saga.settle_terminal

    async def spy(root, base, harness, coro):
        calls.append((root, base))
        return await real(root, base, harness, coro)

    inline_pl = {"nodes": {"echo": {"type": "agent", "template": "hi",
                                    "model": "fake:x", "parse": False}},
                 "graph": {"start": "s", "stages": {"s": {"node": "echo"}}}}
    gated_pl = {"nodes": {"echo": {"type": "agent", "template": "hi",
                                   "model": "fake:x", "parse": False},
                          "gate": {"type": "human_gate", "ask": "ok?",
                                   "awaiting": "go", "form": "approve_or_revise"}},
                "graph": {"start": "s", "stages": {
                    "s": {"node": "echo", "then": "g"},
                    "g": {"node": "gate"}}}}
    saga.settle_terminal = spy  # patched on the module all callers dereference
    try:
        with tempfile.TemporaryDirectory() as d:
            base_root = {
                "transport": {"type": "inproc"},
                "providers": {"fake": {"type": "fake", "default": "done"}},
                "default_provider": "fake",
                "input": {}, "run": True,
            }
            inline = dict(base_root, pipeline=inline_pl)
            root_path = os.path.join(d, "root.json")
            with open(root_path, "w") as f:
                json.dump(inline, f)
            # run_root
            out = asyncio.run(rt.run_root(inline, d))
            assert isinstance(out, Done), out
            assert len(calls) == 1 and calls[0] == (inline, d), calls
            # the MCP run tool
            res = asyncio.run(mcp_tools._tool_run({"root_path": root_path}))
            assert res["outcome"] == "done", res
            assert len(calls) == 2 and calls[1][1] == d, calls
            # resume_gate: park a gated run over a durable store, then resume
            gated = dict(base_root, pipeline=gated_pl,
                         state={"type": "file", "dir": os.path.join(d, "state")})
            parked = asyncio.run(rt.run_root(gated, d))
            assert isinstance(parked, Suspended), parked
            assert len(calls) == 3, calls
            done = asyncio.run(rt.resume_gate(gated, d, parked.baton_id,
                                              {"decision": "approve"}))
            assert isinstance(done, Done), done
            assert len(calls) == 4 and calls[3] == (gated, d), calls
    finally:
        saga.settle_terminal = real


def test_run_root_armed_end_to_end() -> None:
    """The FULL config path: an ARMED pipeline file run through the real
    run_root (assembly, file trace sink, transform nodes) — stage `commit`
    completes with an effect handle, stage `work`'s fn: target raises (a
    non-transient node fault -> node_error verdict -> StageFailed) -> the saga
    undoes `commit` from the REAL trace file and the failure re-raises.

    MONKEY-PATCH NOTE (loud, deliberate): `validate._GRAPH_KEYS` does not yet
    contain "on_failure" — that add lives in the LOCKED validate.py (this
    builder ships it as a diff, slice 1) — so build()'s validate_pipeline would
    reject the armed graph as an unknown key TODAY. The patch simulates the
    post-diff state for exactly this test; remove it when the diff lands."""
    import yaah.runtime as rt
    import yaah.validate as v

    async def go(d):
        _reset()
        pipeline = {
            "nodes": {
                "committer": {"type": "transform",
                              "target": "fn:sagatest_targets:_emit_handle",
                              "into": "handle",
                              "rollback": {"target": _REC}},
                "worker": {"type": "transform",
                           "target": "fn:sagatest_targets:_blow_up"},
            },
            "graph": {"start": "commit", "on_failure": "rollback",
                      "stages": {
                          "commit": {"node": "committer",
                                     "effects_from": "handle", "then": "work"},
                          "work": {"node": "worker", "max_attempts": 1,
                                   "error_retries": 0},
                      }},
        }
        root = {
            "transport": {"type": "inproc"},
            "pipeline": "armed-pipeline.json",
            "trace": {"mode": "tracer", "capture": ["phase"],
                      "sinks": [{"type": "file", "path": "trace.jsonl"}]},
            "input": {}, "run": True,
        }
        with open(os.path.join(d, "armed-pipeline.json"), "w") as f:
            json.dump(pipeline, f)
        buf, ebuf = io.StringIO(), io.StringIO()
        raised = False
        try:
            with redirect_stdout(buf), redirect_stderr(ebuf):
                await rt.run_root(root, d)
        except StageFailed:
            raised = True
        assert raised, "the original failure must surface through run_root"
        # the saga undid `commit` with the effect handle recorded by the run
        assert len(_CALLS) == 1 and _CALLS[0]["stage"] == "commit", _CALLS
        assert _CALLS[0]["effects"] == "H-e2e", _CALLS[0]
        # and the saga record landed in the real trace file
        sagas = _read_saga(os.path.join(d, "trace.jsonl"))
        assert sagas and sagas[0]["rolled_back"][0]["stage"] == "commit", sagas

    old_keys = v._GRAPH_KEYS
    v._GRAPH_KEYS = frozenset(old_keys | {"on_failure"})  # see docstring
    try:
        with tempfile.TemporaryDirectory() as d:
            asyncio.run(go(d))
    finally:
        v._GRAPH_KEYS = old_keys


def test_unarmed_graph_is_noop() -> None:
    async def go():
        _reset()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            root = _root_with_pipeline(path, {"a": {"target": _REC}},
                                       on_failure=None)  # NOT armed
            h, comms, tracer = await _file_harness(path)
            await _write_stage_records(tracer, "RID", [("a", "A1")])
            rep = await saga.run_auto_saga(root, d, h, _fail("RID"))
            assert rep is None and _CALLS == []
    asyncio.run(go())


# ---------- shared helpers for slice-2 tests --------------------------------

async def _silent_saga(root, base, harness, exc):
    buf, ebuf = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(ebuf):
        return await saga.run_auto_saga(root, base, harness, exc)


def _read_saga(path):
    if not os.path.exists(path):
        return []
    return [r for r in rb._read_records(path) if r.get("name") == "saga"]


# ---------- real nodes for the end-to-end test ------------------------------

class _OkEffector:
    def __init__(self, key, value):
        self.key, self.value = key, value

    async def invoke(self, env, config):
        return env.reply_with(Kind.RESULT, {self.key: self.value})


class _Writer:
    async def invoke(self, env, config):
        return env.reply("result", text="nope", ok=False)


class _FailValidator:
    async def invoke(self, env, config):
        return Verdict.failed(Failure("not_ok", "needs ok", "set ok")).to_envelope()


def main() -> None:
    test_value_grammar_accepts_and_rejects()
    test_widened_cross_check()
    test_widened_check_rejects_nonpersisting_mode()
    test_hook_reraises_original_and_passes_through()
    test_no_fire_on_suspended_or_cleared()
    test_end_to_end_undo_and_record()
    test_cheap_only_default_and_include_costly()
    test_partial_undo_reraises_with_pointer()
    test_idempotent_exclusion_on_retried_resume()
    test_looped_stage_occurrences()
    test_compensation_failed_skips_loudly()
    test_corr_guard_skips()
    test_entrypoints_route_through_settle_terminal()
    test_run_root_armed_end_to_end()
    test_unarmed_graph_is_noop()
    print("ok")


if __name__ == "__main__":
    main()
