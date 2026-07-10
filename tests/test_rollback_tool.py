"""rollback tool (ADR-0008 slice 2): candidate resolution from a FileTraceSink
JSONL + a pipeline's `rollback` declarations, the dry-run menu, and --execute.

These falsify the load-bearing claims: the menu orders by FILE APPEND POSITION
(a scrambled t_start would give the WRONG order — asserted); resume notes and
point/error spans are NOT candidates; a twice-run stage yields two occurrences;
undeclared stages land in `impossible`; --execute runs undos in reverse order,
honors include-costly / --only / stop-on-failure / --accept-partial, passes the
rollback ctx (NOT compensate's payload ctx); the menu calls NOTHING (a tripwire
target fails the test if invoked); loud named errors for a missing file sink /
absent file / unknown run.

Run: cd yaah && PYTHONPATH=src python3 tests/test_rollback_tool.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types

from yaah import rollback as rb


# ---------- fixture helpers -------------------------------------------------

def _stage(corr, stage, *, t0=1.0, t1=2.0, status="ok", effects="__unset__",
           resumed=False, **extra):
    """A stage-completion record the FileTraceSink writes (project() structural
    keys + PhaseContributor's status/stage, plus the D2 effects attr)."""
    r = {"id": stage + "-" + str(t0), "corr": corr, "name": "stage",
         "parent": "p", "t_start": t0, "t_end": t1, "status": status,
         "duration_ms": (t1 - t0) * 1000.0, "stage": stage}
    if effects != "__unset__":
        r["effects"] = effects
    if resumed:
        r["resumed"] = True
    r.update(extra)
    return r


def _pipeline(**stage_to_rollback):
    """Build (nodes, stages) where each named stage maps to a node; a rollback
    value of None means the node declares no rollback (an `impossible` candidate)."""
    nodes = {}
    stages = {}
    for stage, rb_block in stage_to_rollback.items():
        node_id = "node:" + stage
        stages[stage] = {"node": node_id}
        nodes[node_id] = {"type": "transform"}
        if rb_block is not None:
            nodes[node_id]["rollback"] = rb_block
    return nodes, stages


# A live module the fn: targets resolve through (call_target does
# importlib.import_module). Registered in sys.modules so no file I/O is needed.
_MOD = types.ModuleType("rbtest_targets")
_CALLS: list = []


def _record_undo(ctx):
    _CALLS.append(ctx)
    return {"undone": ctx.get("stage")}


def _boom_undo(ctx):
    _CALLS.append(ctx)
    raise RuntimeError("undo endpoint down for {}".format(ctx.get("stage")))


def _tripwire(ctx):
    raise AssertionError("MENU CALLED A TARGET — invariant 3 violated: {}".format(ctx))


_MOD._record_undo = _record_undo
_MOD._boom_undo = _boom_undo
_MOD._tripwire = _tripwire
sys.modules["rbtest_targets"] = _MOD

_REC = "fn:rbtest_targets:_record_undo"
_BOOM = "fn:rbtest_targets:_boom_undo"
_TRIP = "fn:rbtest_targets:_tripwire"


def _reset():
    _CALLS.clear()


# ---------- menu / ordering -------------------------------------------------

def scenario_menu_orders_by_append_position_not_tstart() -> None:
    """The load-bearing claim: ordering is FILE APPEND POSITION, never t_start.
    The fixture is built so a t_start sort would give the WRONG answer — the
    LATER line has an EARLIER t_start (a resumed process re-based its clock)."""
    nodes, stages = _pipeline(first={"target": _REC}, second={"target": _REC})
    records = [
        _stage("r1", "first", t0=100.0, t1=101.0),   # ran first (line 0)
        _stage("r1", "second", t0=5.0, t1=6.0),       # ran second (line 1) but EARLIER t_start
    ]
    menu = rb.build_menu(records, stages, nodes, "r1")
    order = [c["stage"] for c in menu["candidates"]]
    # reverse completion order = latest LINE first -> [second, first].
    # A t_start sort would have put `second` (t=5) before `first` (t=100),
    # then reversed to [first, second] — the WRONG undo order.
    assert order == ["second", "first"], order


def scenario_resume_note_excluded() -> None:
    """A resume note is a status-ok POINT span carrying `resumed: true`; it must
    NOT masquerade as a completed, undoable stage (ADR-0008 D3 #4)."""
    nodes, stages = _pipeline(gate={"target": _REC})
    records = [
        _stage("r1", "gate", t0=3.0, t1=3.0, resumed=True),  # resume note (point + resumed)
    ]
    menu = rb.build_menu(records, stages, nodes, "r1")
    assert menu["candidates"] == [], menu


def scenario_point_and_error_spans_excluded() -> None:
    """Point spans (t_start == t_end) and non-ok spans are not real completions."""
    nodes, stages = _pipeline(a={"target": _REC}, b={"target": _REC}, c={"target": _REC})
    records = [
        _stage("r1", "a", t0=1.0, t1=1.0),           # point span
        _stage("r1", "b", t0=1.0, t1=2.0, status="error"),  # failed
        _stage("r1", "c", t0=1.0, t1=2.0),           # real completion
    ]
    menu = rb.build_menu(records, stages, nodes, "r1")
    assert [c["stage"] for c in menu["candidates"]] == ["c"], menu


def scenario_twice_run_occurrences() -> None:
    """A stage that ran twice (a branch-backward loop) yields TWO candidates with
    their own occurrence numbers keyed by file position."""
    nodes, stages = _pipeline(build={"target": _REC})
    records = [
        _stage("r1", "build", t0=1.0, t1=2.0, effects={"id": "A"}),
        _stage("r1", "build", t0=3.0, t1=4.0, effects={"id": "B"}),
    ]
    menu = rb.build_menu(records, stages, nodes, "r1")
    # reverse order: occurrence 2 (later line, effect B) first.
    assert [(c["stage"], c["occurrence"]) for c in menu["candidates"]] == \
        [("build", 2), ("build", 1)], menu
    assert menu["candidates"][0]["effects"] == {"id": "B"}
    assert menu["candidates"][1]["effects"] == {"id": "A"}


def scenario_impossible_bucket() -> None:
    """A completed stage whose node declares no rollback is `impossible` — never
    guessed, never silently dropped."""
    nodes, stages = _pipeline(push={"target": _REC}, notify=None)
    records = [
        _stage("r1", "push", t0=1.0, t1=2.0),
        _stage("r1", "notify", t0=3.0, t1=4.0),
    ]
    menu = rb.build_menu(records, stages, nodes, "r1")
    assert [c["stage"] for c in menu["candidates"]] == ["push"], menu
    assert menu["impossible"] == [{"stage": "notify", "node": "node:notify"}], menu


def scenario_ran_after() -> None:
    """Each candidate lists the distinct stages that ran AFTER it (later lines)."""
    nodes, stages = _pipeline(a={"target": _REC}, b={"target": _REC}, c=None)
    records = [
        _stage("r1", "a", t0=1.0, t1=2.0),
        _stage("r1", "b", t0=3.0, t1=4.0),
        _stage("r1", "c", t0=5.0, t1=6.0),
    ]
    _, imp = rb._resolve(records, stages, nodes, "r1")  # sanity: c is impossible
    cands, _ = rb._resolve(records, stages, nodes, "r1")
    by = {c["stage"]: c["ran_after"] for c in cands}
    assert by["a"] == ["b", "c"], by
    assert by["b"] == ["c"], by
    assert imp == [{"stage": "c", "node": "node:c"}]


def scenario_ran_after_real_completions_only() -> None:
    """ran_after applies the SAME completion gate as candidate selection (eval
    finding): a candidate's own later resume note must not make it appear to
    depend on itself, and errored / point spans are not runs of a stage."""
    nodes, stages = _pipeline(s1={"target": _REC}, s2={"target": _REC},
                              b={"target": _REC}, d={"target": _REC})
    records = [
        _stage("r1", "s1", t0=1.0, t1=2.0),                    # real completion
        _stage("r1", "s1", t0=3.0, t1=3.0, resumed=True),      # its own resume note
        _stage("r1", "b", t0=4.0, t1=5.0, status="error"),     # errored — never ran to completion
        _stage("r1", "s2", t0=6.0, t1=6.0),                    # point span — not a run
        _stage("r1", "d", t0=7.0, t1=8.0),                     # real completion
    ]
    cands, _ = rb._resolve(records, stages, nodes, "r1")
    by = {c["stage"]: c["ran_after"] for c in cands}
    assert by["s1"] == ["d"], by     # not itself, not b (error), not s2 (point)
    assert by["d"] == [], by


def scenario_menu_calls_nothing() -> None:
    """Invariant 3: the menu is a pure read. A tripwire target raises if called;
    building the menu (and rendering it) must never invoke it."""
    _reset()
    nodes, stages = _pipeline(danger={"target": _TRIP})
    records = [_stage("r1", "danger", t0=1.0, t1=2.0)]
    menu = rb.build_menu(records, stages, nodes, "r1")
    _ = rb.render_menu(menu)          # rendering must not call either
    assert menu["candidates"][0]["target"] == _TRIP
    # (if _tripwire had run, an AssertionError would have propagated already)


def scenario_interleaved_runs_ordering() -> None:
    """Two runs interleaved in ONE file: per-corr filtering preserves each run's
    relative append order, so ordering stays correct despite the interleave."""
    nodes, stages = _pipeline(x={"target": _REC}, y={"target": _REC})
    records = [
        _stage("rA", "x", t0=1.0, t1=2.0),
        _stage("rB", "x", t0=1.0, t1=2.0),
        _stage("rA", "y", t0=3.0, t1=4.0),
        _stage("rB", "y", t0=3.0, t1=4.0),
    ]
    menu = rb.build_menu(records, stages, nodes, "rA")
    assert [c["stage"] for c in menu["candidates"]] == ["y", "x"], menu
    runs = rb.list_runs(records, stages, nodes)
    assert {r["run"]: r["candidates"] for r in runs} == {"rA": 2, "rB": 2}, runs


# ---------- execute ---------------------------------------------------------

def scenario_execute_reverse_order_and_ctx() -> None:
    """--execute calls undos in reverse completion order with the rollback ctx
    {correlation_id, stage, node, effects, cost} — and NOT compensate's `payload`
    key (ADR-0008 D3 #7)."""
    _reset()
    nodes, stages = _pipeline(first={"target": _REC},
                              second={"target": _REC, "cost": "cheap"})
    records = [
        _stage("r1", "first", t0=1.0, t1=2.0, effects={"h": 1}),
        _stage("r1", "second", t0=3.0, t1=4.0, effects={"h": 2}),
    ]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1"))
    assert [c["stage"] for c in _CALLS] == ["second", "first"], _CALLS
    # ctx shape
    ctx = _CALLS[0]
    assert set(ctx) == {"correlation_id", "stage", "node", "effects", "cost"}, ctx
    assert "payload" not in ctx
    assert ctx["correlation_id"] == "r1"
    assert ctx["stage"] == "second" and ctx["node"] == "node:second"
    assert ctx["effects"] == {"h": 2} and ctx["cost"] == "cheap"
    assert [e["stage"] for e in report["rolled_back"]] == ["second", "first"]
    assert report["failed"] == [] and report["not_attempted"] == []


def scenario_execute_include_costly() -> None:
    """A `costly` candidate is skipped unless --include-costly; the report keeps
    exactly the five buckets."""
    _reset()
    nodes, stages = _pipeline(cheap_s={"target": _REC},
                              costly_s={"target": _REC, "cost": "costly"})
    records = [
        _stage("r1", "cheap_s", t0=1.0, t1=2.0),
        _stage("r1", "costly_s", t0=3.0, t1=4.0),
    ]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1"))
    assert [c["stage"] for c in _CALLS] == ["cheap_s"], _CALLS
    assert [e["stage"] for e in report["skipped_costly"]] == ["costly_s"]
    assert set(report) == {"run", "rolled_back", "skipped_costly", "impossible",
                           "failed", "not_attempted"}, set(report)
    _reset()
    report2 = asyncio.run(rb.execute(records, stages, nodes, "r1", include_costly=True))
    assert [c["stage"] for c in _CALLS] == ["costly_s", "cheap_s"], _CALLS
    assert report2["skipped_costly"] == []


def scenario_execute_only_addresses_all_occurrences() -> None:
    """--only restricts to the named stage(s), targeting ALL occurrences."""
    _reset()
    nodes, stages = _pipeline(build={"target": _REC}, ship={"target": _REC})
    records = [
        _stage("r1", "build", t0=1.0, t1=2.0, effects={"occ": 1}),
        _stage("r1", "ship", t0=3.0, t1=4.0),
        _stage("r1", "build", t0=5.0, t1=6.0, effects={"occ": 2}),   # ran twice
    ]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1", only=["build"]))
    # both build occurrences, reverse order (later effect first); ship untouched.
    assert [(c["stage"], c["effects"]) for c in _CALLS] == \
        [("build", {"occ": 2}), ("build", {"occ": 1})], _CALLS
    assert [(e["stage"], e["occurrence"]) for e in report["rolled_back"]] == \
        [("build", 2), ("build", 1)], report["rolled_back"]


def scenario_execute_only_unknown_raises() -> None:
    """--only naming a stage with no candidate is a loud, named error."""
    nodes, stages = _pipeline(build={"target": _REC})
    records = [_stage("r1", "build", t0=1.0, t1=2.0)]
    try:
        asyncio.run(rb.execute(records, stages, nodes, "r1", only=["nope"]))
    except rb.RollbackError as e:
        assert "nope" in str(e), str(e)
    else:
        raise AssertionError("expected RollbackError for unknown --only stage")


def scenario_execute_stop_on_failure() -> None:
    """STOP on the first failed undo (default). The failing candidate lands in
    `failed`; everything past it in `not_attempted`; nothing after it is called."""
    _reset()
    nodes, stages = _pipeline(a={"target": _REC}, b={"target": _BOOM},
                              c={"target": _REC})
    records = [
        _stage("r1", "a", t0=1.0, t1=2.0),
        _stage("r1", "b", t0=3.0, t1=4.0),
        _stage("r1", "c", t0=5.0, t1=6.0),
    ]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1"))
    # reverse order c,b,a -> c ok, b fails, a never attempted.
    assert [c["stage"] for c in _CALLS] == ["c", "b"], _CALLS
    assert [e["stage"] for e in report["rolled_back"]] == ["c"]
    assert [e["stage"] for e in report["failed"]] == ["b"]
    assert "RuntimeError" in report["failed"][0]["error"]
    assert [e["stage"] for e in report["not_attempted"]] == ["a"]


def scenario_execute_accept_partial_continues() -> None:
    """--accept-partial continues past a failed undo (explicit consent to a
    half-unwound run)."""
    _reset()
    nodes, stages = _pipeline(a={"target": _REC}, b={"target": _BOOM},
                              c={"target": _REC})
    records = [
        _stage("r1", "a", t0=1.0, t1=2.0),
        _stage("r1", "b", t0=3.0, t1=4.0),
        _stage("r1", "c", t0=5.0, t1=6.0),
    ]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1", accept_partial=True))
    assert [c["stage"] for c in _CALLS] == ["c", "b", "a"], _CALLS
    assert [e["stage"] for e in report["rolled_back"]] == ["c", "a"]
    assert [e["stage"] for e in report["failed"]] == ["b"]
    assert report["not_attempted"] == []


def scenario_execute_node_target_refused() -> None:
    """A `node:` target (validate should reject it, but defend anyway) yields a
    NAMED refusal in `failed` — not a crash — and stops the run."""
    _reset()
    nodes, stages = _pipeline(x={"target": "node:undo-service"})
    records = [_stage("r1", "x", t0=1.0, t1=2.0)]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1"))
    assert _CALLS == [], "a node: target must never reach call_target"
    assert [e["stage"] for e in report["failed"]] == ["x"]
    assert "node" in report["failed"][0]["error"], report["failed"][0]


def scenario_execute_impossible_reported() -> None:
    """`impossible` rides through to the execute report as a first-class outcome."""
    _reset()
    nodes, stages = _pipeline(push={"target": _REC}, notify=None)
    records = [
        _stage("r1", "push", t0=1.0, t1=2.0),
        _stage("r1", "notify", t0=3.0, t1=4.0),
    ]
    report = asyncio.run(rb.execute(records, stages, nodes, "r1"))
    assert report["impossible"] == [{"stage": "notify", "node": "node:notify"}]
    assert [e["stage"] for e in report["rolled_back"]] == ["push"]
    # render_report must not crash on the occurrence-less `impossible` entries.
    text = rb.render_report(report)
    assert "notify" in text and "push#1" in text, text


def scenario_truncated_effects_surfaced() -> None:
    """An oversize descriptor (recorded as effects_truncated + head, no effects)
    shows the dropped warning in the menu and hands the undo effects=None."""
    _reset()
    nodes, stages = _pipeline(big={"target": _REC})
    records = [
        {"id": "big", "corr": "r1", "name": "stage", "parent": "p",
         "t_start": 1.0, "t_end": 2.0, "status": "ok", "stage": "big",
         "effects": None, "effects_truncated": True, "effects_head": "{\"id\": \"AAA"},
    ]
    menu = rb.build_menu(records, stages, nodes, "r1")
    c = menu["candidates"][0]
    assert c["effects_truncated"] is True and c["effects"] is None
    assert "DROPPED" in rb.render_menu(menu)
    report = asyncio.run(rb.execute(records, stages, nodes, "r1"))
    assert _CALLS[0]["effects"] is None, _CALLS
    assert [e["stage"] for e in report["rolled_back"]] == ["big"]


# ---------- loud named errors ----------------------------------------------

def scenario_no_file_sink_raises() -> None:
    """A root with no file trace sink cannot feed rollback — loud, named."""
    for trace in ({}, {"sinks": []}, {"sinks": [{"type": "console"}]},
                  {"sinks": [{"type": "progress_file", "path": "p.log"}]}):
        try:
            rb.read_trace({"trace": trace}, "/tmp")
        except rb.RollbackError as e:
            assert "file trace sink" in str(e), str(e)
        else:
            raise AssertionError("expected RollbackError for trace={!r}".format(trace))


def scenario_missing_trace_file_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = {"trace": {"sinks": [{"type": "file", "path": "trace.jsonl"}]}}
        try:
            rb.read_trace(root, td)
        except rb.RollbackError as e:
            assert "not found" in str(e), str(e)
        else:
            raise AssertionError("expected RollbackError for a missing trace file")


def scenario_unknown_corr_raises() -> None:
    nodes, stages = _pipeline(a={"target": _REC})
    records = [_stage("r1", "a", t0=1.0, t1=2.0)]
    try:
        rb.build_menu(records, stages, nodes, "does-not-exist")
    except rb.RollbackError as e:
        assert "does-not-exist" in str(e), str(e)
    else:
        raise AssertionError("expected RollbackError for an unknown corr")


def scenario_garbage_lines_tolerated() -> None:
    """A crash-truncated / corrupt line is skipped, not fatal; good records before
    and after it still resolve."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "trace.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps(_stage("r1", "a", t0=1.0, t1=2.0)) + "\n")
            f.write("{ this is not valid json\n")     # corrupt
            f.write("\n")                               # blank
            f.write("[1,2,3]\n")                        # valid JSON but not an object
            f.write(json.dumps(_stage("r1", "b", t0=3.0, t1=4.0)) + "\n")
        recs = rb._read_records(path)
        assert [r["stage"] for r in recs] == ["a", "b"], recs


# ---------- end-to-end: real fn: targets deleting real files ----------------

def scenario_e2e_effects_files_deleted() -> None:
    """End-to-end against a fixture-generated trace: two stages 'wrote' temp files
    (their effect descriptors carry the paths); --execute calls a real fn: target
    that deletes each file, in reverse order. Assert the files are gone and the
    report is complete. (This proves the tool half against a fixture trace;
    the joint real-engine run — effects_from recording feeding this verb — is
    covered by examples/rollback-demo/, coordinator-verified end to end.)"""
    with tempfile.TemporaryDirectory() as td:
        # a real deleter module for this test
        mod = types.ModuleType("rbtest_e2e")
        order: list = []

        def delete_effect(ctx):
            order.append(ctx["stage"])
            os.remove(ctx["effects"]["path"])
            return {"deleted": ctx["effects"]["path"]}

        mod.delete_effect = delete_effect
        sys.modules["rbtest_e2e"] = mod

        f1 = os.path.join(td, "amendment-1.txt")
        f2 = os.path.join(td, "amendment-2.txt")
        for p in (f1, f2):
            with open(p, "w") as fh:
                fh.write("committed effect")

        nodes, stages = _pipeline(
            write_one={"target": "fn:rbtest_e2e:delete_effect"},
            write_two={"target": "fn:rbtest_e2e:delete_effect"})
        records = [
            _stage("run-9", "write_one", t0=1.0, t1=2.0, effects={"path": f1}),
            _stage("run-9", "write_two", t0=3.0, t1=4.0, effects={"path": f2}),
        ]
        report = asyncio.run(rb.execute(records, stages, nodes, "run-9"))
        assert order == ["write_two", "write_one"], order
        assert not os.path.exists(f1) and not os.path.exists(f2)
        assert [e["stage"] for e in report["rolled_back"]] == ["write_two", "write_one"]
        assert report["failed"] == [] and report["not_attempted"] == []


# ---------- CLI parser ------------------------------------------------------

def scenario_cli_parser() -> None:
    from yaah.cli import _parse_rollback

    assert _parse_rollback(["root.json"]) == {
        "action": "rollback", "root": "root.json", "corr": None, "json": False,
        "execute": False, "include_costly": False, "only": [], "accept_partial": False,
        "fake": False, "debug": False}
    assert _parse_rollback(["root.json", "R1"])["corr"] == "R1"
    assert _parse_rollback(["root.json", "--json"])["json"] is True
    got = _parse_rollback(["root.json", "R1", "--execute", "--include-costly",
                           "--only", "a", "--only", "b", "--accept-partial"])
    assert got["execute"] and got["include_costly"] and got["accept_partial"]
    assert got["only"] == ["a", "b"], got
    # --execute + --json (machine report) is allowed
    assert _parse_rollback(["root.json", "R1", "--execute", "--json"])["json"] is True

    # bad forms all exit 2
    for bad in [[],                                   # no root
                ["root.json", "R1", "X"],             # too many positionals
                ["root.json", "--execute"],           # execute needs a corr
                ["root.json", "--include-costly"],    # execute-only flag w/o execute
                ["root.json", "--only", "a"],         # execute-only flag w/o execute
                ["root.json", "--accept-partial"],    # execute-only flag w/o execute
                ["root.json", "--only"],              # --only needs a value
                ["root.json", "--bogus"]]:            # unknown flag
        try:
            _parse_rollback(bad)
        except SystemExit as e:
            assert e.code == 2, bad
            continue
        raise AssertionError("expected SystemExit for {!r}".format(bad))


def scenario_verb_registered() -> None:
    """The verb is wired in both registries (parser + self-contained dispatch) and
    the completion mirror, so `yaah rollback` dispatches and tab-completes."""
    from yaah.cli import _VERB_PARSERS, _SELF_CONTAINED_DISPATCH
    from yaah.shell_completion import _VERBS
    assert "rollback" in _VERB_PARSERS
    assert "rollback" in _SELF_CONTAINED_DISPATCH
    assert "rollback" in _VERBS


def main() -> None:
    scenario_menu_orders_by_append_position_not_tstart()
    scenario_resume_note_excluded()
    scenario_point_and_error_spans_excluded()
    scenario_twice_run_occurrences()
    scenario_impossible_bucket()
    scenario_ran_after()
    scenario_ran_after_real_completions_only()
    scenario_menu_calls_nothing()
    scenario_interleaved_runs_ordering()
    scenario_execute_reverse_order_and_ctx()
    scenario_execute_include_costly()
    scenario_execute_only_addresses_all_occurrences()
    scenario_execute_only_unknown_raises()
    scenario_execute_stop_on_failure()
    scenario_execute_accept_partial_continues()
    scenario_execute_node_target_refused()
    scenario_execute_impossible_reported()
    scenario_truncated_effects_surfaced()
    scenario_no_file_sink_raises()
    scenario_missing_trace_file_raises()
    scenario_unknown_corr_raises()
    scenario_garbage_lines_tolerated()
    scenario_e2e_effects_files_deleted()
    scenario_cli_parser()
    scenario_verb_registered()
    print("ok")


if __name__ == "__main__":
    main()
