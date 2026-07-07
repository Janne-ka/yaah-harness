"""ADR-0008 slice 1 (config + record): the `rollback` node key, the `effects_from`
stage key, the completion-span effect record, and the file-sink cross-check.

These FALSIFY the spec on the real downstream shape:
  - D1: rollback block rejects each bad shape (non-dict, missing/blank target,
    node: target, non-fn/http target, bad cost, unknown key); accepts the good one.
  - D2: effects_from rejects non-string and fork/fanin stages; accepts a linear one.
  - D2 record: a PASSING stage's completion span carries `effects` (the pulled
    handle); a stage without effects_from carries none (backward compat); oversize
    → effects:null + effects_truncated + a plain-string head (never clipped JSON);
    non-serializable → same truncated shape, never raises; fork branch-child and
    foreach (the MERGED key) both record through the shared seam.
  - D2 phase whitelist: the trio reaches the PROJECTED record, not just the span.
  - D2 cross-check: fires from validate_config (author time) AND the runtime
    assembly path (run time) — proven by driving the assembly, not a subprocess.
  - Lint: rollback declared but no stage runs it with effects_from → WARNING.

Run: cd yaah && PYTHONPATH=src python3 tests/test_rollback_record.py
"""
from __future__ import annotations

import asyncio
import json
import tempfile

from yaah import Done, Envelope, Graph, Harness, InProcessComms, NodeConfig, Stage
from yaah.core import Kind
from yaah.trace import RecordingTracer
from yaah.trace.contributors.phase import PhaseContributor
from yaah.validate import (
    check_rollback_trace_sink,
    lint_pipeline,
    validate_config,
    validate_pipeline,
)
import yaah.runtime as r


# --- nodes -------------------------------------------------------------------

class Effector:
    """Emits a chosen effect handle under a chosen payload key."""
    def __init__(self, key: str, value):
        self.key, self.value = key, value

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply_with(Kind.RESULT, {self.key: self.value})


class Item:
    """foreach per-item worker: echoes the item's id back."""
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply_with(Kind.RESULT, {"seen": input.payload.get("item")})


def _expect(fn, needle: str) -> None:
    try:
        fn()
    except ValueError as e:
        assert needle in str(e), "wrong error: {!r} (wanted {!r})".format(str(e), needle)
        return
    raise AssertionError("expected ValueError containing {!r}".format(needle))


def _pipe(node_extra=None, stage_extra=None) -> dict:
    node = {"type": "transform", "target": "fn:m:f"}
    node.update(node_extra or {})
    stage = {"node": "x"}
    stage.update(stage_extra or {})
    return {"nodes": {"x": node},
            "graph": {"start": "s", "stages": {"s": stage}}}


# --- D1: rollback block validation -------------------------------------------

def test_rollback_bad_shapes_rejected() -> None:
    _expect(lambda: validate_pipeline(_pipe({"rollback": "fn:x"})),
            "rollback must be an object")
    _expect(lambda: validate_pipeline(_pipe({"rollback": {}})),
            "needs a non-empty `target`")
    _expect(lambda: validate_pipeline(_pipe({"rollback": {"target": ""}})),
            "needs a non-empty `target`")
    _expect(lambda: validate_pipeline(_pipe({"rollback": {"target": "node:undo"}})),
            "node: target")
    _expect(lambda: validate_pipeline(_pipe({"rollback": {"target": "undo"}})),
            "must be an fn:/http:")
    _expect(lambda: validate_pipeline(
        _pipe({"rollback": {"target": "fn:u", "cost": "free"}})),
            'must be "cheap" or "costly"')
    _expect(lambda: validate_pipeline(
        _pipe({"rollback": {"target": "fn:u", "typo": 1}})),
            "unknown rollback key")


def test_rollback_good_shapes_accepted() -> None:
    for rb in ({"target": "fn:undo:go"},
               {"target": "http:hook", "cost": "costly"},
               {"target": "fn:u", "cost": "cheap", "note": "why"}):
        validate_pipeline(_pipe({"rollback": rb}))  # must not raise
    # a node with NO rollback still validates (absence is legal / irreversible)
    validate_pipeline(_pipe())


# --- D2: effects_from stage validation ---------------------------------------

def test_effects_from_rejected_bad_and_on_fork_fanin() -> None:
    _expect(lambda: validate_pipeline(_pipe(stage_extra={"effects_from": ""})),
            "effects_from must be a non-empty")
    # on a fork stage
    forkp = {
        "nodes": {"x": {"type": "transform", "target": "fn:m:f"}},
        "graph": {"start": "spread", "stages": {
            "spread": {"node": "", "fork": ["a"], "effects_from": "h"},
            "a": {"node": "x"},
        }},
    }
    _expect(lambda: validate_pipeline(forkp), "not allowed on a fork/fanin")
    # on a fanin stage
    faninp = {
        "nodes": {"x": {"type": "transform", "target": "fn:m:f"}},
        "graph": {"start": "spread", "stages": {
            "spread": {"node": "", "fork": ["a", "b"]},
            "a": {"node": "x", "then": "join"},
            "b": {"node": "x", "then": "join"},
            "join": {"node": "", "fanin": {"expect": ["a", "b"]},
                     "effects_from": "h"},
        }},
    }
    _expect(lambda: validate_pipeline(faninp), "not allowed on a fork/fanin")


def test_effects_from_accepted_on_linear() -> None:
    validate_pipeline(_pipe(stage_extra={"effects_from": "amendment_id"}))


# --- D2: the completion-span record ------------------------------------------

def _run(node, stage, tracer):
    comms = InProcessComms()
    comms.register("role:x", node)
    return asyncio.run(Harness(comms, Graph.of(stage), tracer=tracer).run(
        Envelope(Kind.TASK, {})))


def _stage_span(tr):
    return next(s for s in tr.spans if s.name == "stage")


def test_completion_span_records_effects() -> None:
    tr = RecordingTracer()
    out = _run(Effector("amendment_id", "AMD-42"),
               Stage("s", node="role:x", effects_from="amendment_id"), tr)
    assert isinstance(out, Done), out
    sp = _stage_span(tr)
    assert sp.attrs.get("effects") == "AMD-42", sp.attrs
    assert "effects_truncated" not in sp.attrs, sp.attrs


def test_no_effects_from_records_nothing() -> None:
    tr = RecordingTracer()
    _run(Effector("amendment_id", "AMD-42"), Stage("s", node="role:x"), tr)
    sp = _stage_span(tr)
    assert "effects" not in sp.attrs, sp.attrs
    assert "effects_truncated" not in sp.attrs, sp.attrs


def test_oversize_descriptor_truncated_not_clipped() -> None:
    big = "x" * 3000
    tr = RecordingTracer()
    _run(Effector("h", big), Stage("s", node="role:x", effects_from="h"), tr)
    sp = _stage_span(tr)
    assert sp.attrs.get("effects") is None, sp.attrs
    assert sp.attrs.get("effects_truncated") is True, sp.attrs
    head = sp.attrs.get("effects_head")
    assert isinstance(head, str) and len(head) == 256, head
    assert head == json.dumps(big)[:256]           # a HEAD of the serialized form
    # crucially NOT a clipped-then-parseable JSON value
    try:
        json.loads(head)
        raise AssertionError("effects_head parsed as JSON — it must be a plain head")
    except ValueError:
        pass


def test_nonserializable_descriptor_truncated_no_raise() -> None:
    class Weird:
        def __repr__(self): return "<weird effect handle>"
    tr = RecordingTracer()
    out = _run(Effector("h", Weird()),
               Stage("s", node="role:x", effects_from="h"), tr)
    assert isinstance(out, Done), out            # never raised
    sp = _stage_span(tr)
    assert sp.attrs.get("effects") is None, sp.attrs
    assert sp.attrs.get("effects_truncated") is True, sp.attrs
    assert sp.attrs.get("effects_head") == "<weird effect handle>", sp.attrs


def test_effects_reach_projected_record_via_phase() -> None:
    tr = RecordingTracer([PhaseContributor()])
    _run(Effector("h", {"id": "AMD-9"}),
         Stage("s", node="role:x", effects_from="h"), tr)
    rec = next(r for r in tr.records if r.get("name") == "stage")
    assert rec.get("effects") == {"id": "AMD-9"}, rec


def test_recorded_effects_is_a_fresh_structure_not_the_live_object() -> None:
    # aliasing hardening (eval YELLOW): the descriptor is COPIED off the payload,
    # so a downstream in-place mutation must not corrupt the emitted record.
    handle = {"id": "AMD-1", "files": ["a.txt"]}
    tr = RecordingTracer()
    out = _run(Effector("h", handle),
               Stage("s", node="role:x", effects_from="h"), tr)
    assert isinstance(out, Done), out
    recorded = _stage_span(tr).attrs["effects"]
    assert recorded == {"id": "AMD-1", "files": ["a.txt"]}, recorded
    # mutate the payload-side object AFTER emit — the record must not follow
    out.output.payload["h"]["files"].append("b.txt")
    assert recorded["files"] == ["a.txt"], recorded


def test_fork_branch_child_records_effects() -> None:
    comms = InProcessComms()
    comms.register("role:x", Effector("findings", ["f1"]))
    graph = Graph.of(
        Stage("spread", node="", fork=["a", "b"], then=None),
        Stage("a", node="role:x", effects_from="findings", then=None),
        Stage("b", node="role:x", then=None),
    )
    tr = RecordingTracer()
    out = asyncio.run(Harness(comms, graph, tracer=tr).run(Envelope(Kind.TASK, {})))
    assert isinstance(out, Done), out
    a_span = next(s for s in tr.spans
                  if s.name == "stage" and s.attrs.get("stage") == "a")
    assert a_span.attrs.get("effects") == ["f1"], a_span.attrs


def test_foreach_records_merged_key() -> None:
    comms = InProcessComms()
    comms.register("role:x", Item())
    graph = Graph.of(Stage("swarm", node="role:x",
                           foreach={"items": "reqs"}, effects_from="results"))
    tr = RecordingTracer()
    out = asyncio.run(Harness(comms, graph, tracer=tr).run(
        Envelope(Kind.TASK, {"reqs": ["p", "q"]})))
    assert isinstance(out, Done), out
    sp = _stage_span(tr)
    eff = sp.attrs.get("effects")
    assert isinstance(eff, list) and [e["item_index"] for e in eff] == [0, 1], sp.attrs


# --- D2: the cross-file file-sink check (helper + both call paths) -----------

def _root(sinks) -> dict:
    return {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "x"}},
        "default_provider": "fake",
        "state": {"type": "memory"},
        "trace": {"mode": "tracer", "capture": ["phase"], "sinks": sinks},
        "pipeline": {
            "nodes": {"push": {"type": "transform", "target": "fn:m:f",
                               "rollback": {"target": "fn:u:undo"}},
                      "e": {"type": "transform", "target": "fn:m:f"}},
            "graph": {"start": "s", "stages": {
                "s": {"node": "push", "effects_from": "amendment_id", "then": "t"},
                "t": {"node": "e"}}},
        },
    }


def test_helper_fires_only_without_file_sink() -> None:
    nodes = {"push": {"type": "transform", "rollback": {"target": "fn:u"}}}
    # no file sink -> error naming the node + "rollback input"
    errs = check_rollback_trace_sink({"trace": {"sinks": [{"type": "console"}]}}, nodes)
    assert errs and "push" in errs[0] and "rollback input" in errs[0], errs
    # a bare single-dict sink shape (factory accepts it) is handled
    errs = check_rollback_trace_sink({"trace": {"sinks": {"type": "console"}}}, nodes)
    assert errs, errs
    # with a file sink -> satisfied
    assert check_rollback_trace_sink(
        {"trace": {"sinks": [{"type": "console"}, {"type": "file"}]}}, nodes) == []
    # no node declares rollback -> no requirement even with no sink at all
    assert check_rollback_trace_sink({}, {"push": {"type": "transform"}}) == []


def test_helper_rejects_nonpersisting_trace_modes() -> None:
    # adversarial-eval RED: a file sink under mode "none"/"envelope" is DEAD
    # (_build_tracer short-circuits before the sink-subscribe loop) — sink
    # declaration alone must not satisfy the check.
    nodes = {"push": {"type": "transform", "rollback": {"target": "fn:u"}}}
    for mode in ("none", "envelope"):
        errs = check_rollback_trace_sink(
            {"trace": {"mode": mode, "sinks": [{"type": "file"}]}}, nodes)
        assert errs and "push" in errs[0] and mode in errs[0], (mode, errs)
    # absent mode defaults to "tracer" -> file sink satisfies (unchanged)
    assert check_rollback_trace_sink(
        {"trace": {"sinks": [{"type": "file"}]}}, nodes) == []


def test_validate_config_author_time_refuses() -> None:
    with tempfile.TemporaryDirectory() as d:
        _expect(lambda: validate_config(_root([{"type": "console"}]), d),
                "rollback input")
        # with a file sink it passes root+pipeline validation (returns lint warnings)
        warns = validate_config(_root([{"type": "file", "path": "trace.jsonl"}]), d)
        assert isinstance(warns, list)


def test_run_path_refuses_before_committing() -> None:
    # prove `yaah run`'s ASSEMBLY refuses (validate_config never runs on run).
    async def assemble(root, base):
        async with r.opened_store(root.get("state"), base) as store:
            await r._assemble_harness(root, base, store=store)

    with tempfile.TemporaryDirectory() as d:
        root = _root([{"type": "console"}])
        try:
            asyncio.run(assemble(root, d))
            raise AssertionError("assembly should have refused")
        except ValueError as e:
            assert "push" in str(e) and "rollback input" in str(e), str(e)
        # a file sink lets assembly proceed (no ValueError from the cross-check)
        root_ok = _root([{"type": "file", "path": "trace.jsonl"}])
        asyncio.run(assemble(root_ok, d))  # must not raise


# --- Lint: rollback-without-effects WARNING ----------------------------------

def test_lint_rollback_without_effects_warns() -> None:
    # node declares rollback, stage running it has NO effects_from -> warning
    cfg = _pipe({"rollback": {"target": "fn:u"}})
    ws = lint_pipeline(cfg)
    assert any("rollback-without-effects" in w for w in ws), ws
    # add effects_from on the stage -> warning gone
    cfg2 = _pipe({"rollback": {"target": "fn:u"}},
                 stage_extra={"effects_from": "h"})
    ws2 = lint_pipeline(cfg2)
    assert not any("rollback-without-effects" in w for w in ws2), ws2


def main() -> None:
    test_rollback_bad_shapes_rejected()
    test_rollback_good_shapes_accepted()
    test_effects_from_rejected_bad_and_on_fork_fanin()
    test_effects_from_accepted_on_linear()
    test_completion_span_records_effects()
    test_no_effects_from_records_nothing()
    test_oversize_descriptor_truncated_not_clipped()
    test_nonserializable_descriptor_truncated_no_raise()
    test_effects_reach_projected_record_via_phase()
    test_recorded_effects_is_a_fresh_structure_not_the_live_object()
    test_fork_branch_child_records_effects()
    test_foreach_records_merged_key()
    test_helper_fires_only_without_file_sink()
    test_helper_rejects_nonpersisting_trace_modes()
    test_validate_config_author_time_refuses()
    test_run_path_refuses_before_committing()
    test_lint_rollback_without_effects_warns()
    print("ok")


if __name__ == "__main__":
    main()
