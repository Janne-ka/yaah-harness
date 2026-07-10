"""Experiment-level contract pre-flight (AB-T1) — stop a campaign from burning
money on provable garbage, without ever rejecting a legitimate experiment.

Contracts under test (each a falsifier):
- (a) shared-input compatibility: an experiment input that provably breaks a
  RENDER (reads an entry key no input provides, on a provable path) ABORTS
  pre-flight naming variant + input + key; ZERO rows land. A provably-absent
  BRANCH key is NOT a failure (absent routes to `branch.default`, the run
  completes) → WARNING, never abort.
- fixture-path inputs participate: their key set is read from the JSON file.
- lattice honesty: an UNKNOWABLE case (opaque transform upstream, or an input
  whose keys can't be read) never aborts — skip silently, never a false positive.
- LEGITIMATE pipelines are never rejected: runtime key sources the lattice
  can't see (`concerns_into` engine-set at the start stage; a human gate's
  resume merge; `escalate: "human"`) must not produce a false abort.
- (b) metric-path plausibility: a metric path provably NEVER carried by any
  terminal payload ABORTS naming variant + metric; declared-but-unproven prints
  a stderr WARNING (not fatal); a proven metric is silent; a malformed path
  (empty segment) is rejected by the experiment shape check.
- fork/fanout/fanin stages never contribute an absence PROOF (their merged/
  reduced output is unmodeled — widened to unknown: warn at most, never abort).
- the SHIPPED example experiment (examples/hello-yaah) still passes pre-flight.

Run: cd yaah && PYTHONPATH=src python3 tests/test_experiment_contracts.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile

from yaah.adapters.experiment_stores import JsonlExperimentStore
from yaah.experiment import run_experiment
from yaah.experiment.contracts import check_variant_contracts, entry_key_sets
from yaah.dataflow import terminal_stages


def _write(d: str, name: str, obj) -> str:
    path = os.path.join(d, name)
    with open(path, "w") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f)
    return path


def _expect_abort(fn, *needles):
    try:
        fn()
    except ValueError as e:
        for n in needles:
            assert n in str(e), (n, str(e))
        return
    raise AssertionError("expected pre-flight abort naming {}".format(needles))


# ---------------------------------------------------------------- direct policy checks

RENDER_TASK = {
    "nodes": {"role:tmpl": {"type": "render", "template_text": "do: {{task}}"}},
    "graph": {"start": "s", "stages": {"s": {"node": "role:tmpl"}}},
}

AGENT_RAW = {   # parse:false terminal — output payload is provably exactly {raw}
    "nodes": {"role:draft": {"type": "agent", "prompt": "file:draft",
                             "model": "fake:m", "parse": False}},
    "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
}


def direct_checks() -> None:
    inline = [("inline-0", frozenset({"request"}))]

    # (a) a provable input mismatch aborts, naming variant + input + key
    _expect_abort(lambda: check_variant_contracts("V", RENDER_TASK, None, inline, {}),
                  "V", "inline-0", "task")

    # (a) the same input WITH the key passes
    ok = check_variant_contracts("V", RENDER_TASK, None,
                                 [("inline-0", frozenset({"task"}))], {})
    assert ok == [], ok

    # (a) ALL inputs must satisfy the variant — one good input doesn't excuse a bad one
    _expect_abort(lambda: check_variant_contracts(
        "V", RENDER_TASK, None,
        [("good", frozenset({"task"})), ("bad", frozenset({"request"}))], {}),
        "V", "bad", "task")

    # (a) unknowable input keys → skip silently (lattice honesty, never a false positive)
    assert check_variant_contracts("V", RENDER_TASK, None, [("inline-0", None)], {}) == []

    # (a) a provably-absent BRANCH key is NOT a failure — the harness routes
    # absent → branch.default and the run completes (an optional routing key is
    # a legitimate heterogeneous-input design) → WARNING naming input + key,
    # never an abort. (The branching stage needs a node — a bare routing stage
    # is opaque to the lattice's branch check, an existing behavior.)
    branch_pipe = {
        "nodes": {"role:tmpl": {"type": "render", "template_text": "x",
                                "allow_unfilled": True}},
        "graph": {"start": "s", "stages": {
            "s": {"node": "role:tmpl",
                  "branch": {"on": "task", "routes": {"go": "t"}, "default": "t"}},
            "t": {"node": "role:tmpl"}}},
    }
    warns = check_variant_contracts("V", branch_pipe, None, inline, {})
    assert len(warns) == 1 and "task" in warns[0] and "inline-0" in warns[0] \
        and "default" in warns[0], warns

    # (a) an opaque transform between entry and the consumer → unknowable → no abort
    opaque_pipe = {
        "nodes": {"role:mk": {"type": "transform", "call": "envelope",
                              "target": "fn:mod:fn"},
                  "role:tmpl": {"type": "render", "template_text": "do: {{task}}"}},
        "graph": {"start": "s1", "stages": {"s1": {"node": "role:mk", "then": "s2"},
                                            "s2": {"node": "role:tmpl"}}},
    }
    assert check_variant_contracts("V", opaque_pipe, None, inline, {}) == []

    # LEGITIMATE runtime key sources must never abort (each ran Done in anger):
    # `concerns_into` engine-sets a key on the START stage's own input
    concerns_pipe = {
        "nodes": {"role:tmpl": {"type": "render",
                                "template_text": "notes: {{run_concerns}}"}},
        "graph": {"start": "s", "stages": {
            "s": {"node": "role:tmpl", "concerns_into": "run_concerns"}}},
    }
    assert check_variant_contracts("V", concerns_pipe, None, inline, {}) == []

    # a human gate's resume MERGES the human's reply keys onto the payload
    gate_pipe = {
        "nodes": {"role:gate": {"type": "human_gate", "ask": "ok?"},
                  "role:tmpl": {"type": "render", "template_text": "{{fix_notes}}"}},
        "graph": {"start": "g", "stages": {"g": {"node": "role:gate", "then": "r"},
                                           "r": {"node": "role:tmpl"}}},
    }
    assert check_variant_contracts("V", gate_pipe, None, inline, {}) == []

    # `escalate: "human"` suspends any stage; resume merges human keys the same way
    esc_pipe = {
        "nodes": {"role:a": {"type": "render", "template_text": "{{request}}"},
                  "role:tmpl": {"type": "render", "template_text": "{{fix_notes}}"}},
        "graph": {"start": "a", "stages": {
            "a": {"node": "role:a", "escalate": "human", "then": "r"},
            "r": {"node": "role:tmpl"}}},
    }
    assert check_variant_contracts("V", esc_pipe, None, inline, {}) == []

    # (b) metric provably never produced (closed terminal without the key) → abort
    _expect_abort(lambda: check_variant_contracts("V", AGENT_RAW, None, inline,
                                                  {"score": "score"}),
                  "V", "score")

    # (b) the proof survives UNKNOWABLE inputs: a parse:false terminal's payload is
    # exactly {raw} no matter what the entry was
    _expect_abort(lambda: check_variant_contracts("V", AGENT_RAW, None,
                                                  [("inline-0", None)],
                                                  {"score": "score"}),
                  "V", "score")

    # (b) an UNREACHABLE terminal contributes nothing (it never runs)
    orphaned = {
        "nodes": dict(AGENT_RAW["nodes"],
                      **{"role:tmpl": {"type": "render", "template_text": "x",
                                       "allow_unfilled": True}}),
        "graph": {"start": "s", "stages": {"s": {"node": "role:draft"},
                                           "orphan": {"node": "role:tmpl"}}},
    }
    _expect_abort(lambda: check_variant_contracts("V", orphaned, None, inline,
                                                  {"score": "score"}),
                  "V", "score")

    # (b) declared-but-unproven (parse:true agent, no schema) → ONE warning, no abort
    loose = {
        "nodes": {"role:draft": {"type": "agent", "prompt": "file:draft",
                                 "model": "fake:m"}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
    }
    warns = check_variant_contracts("V", loose, None, inline, {"score": "score"})
    assert len(warns) == 1 and "score" in warns[0] and "'V'" in warns[0], warns

    # (b) a PROVEN metric (declared in output_schema) is silent — dotted paths check
    # only the top-level segment (deeper shape is beyond static knowledge)
    scored = {
        "nodes": {"role:draft": {"type": "agent", "prompt": "file:draft", "model": "fake:m",
                                 "output_schema": {"type": "object",
                                                   "properties": {"review": {}}}}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
    }
    assert check_variant_contracts("V", scored, None, inline,
                                   {"score": "review.score"}) == []

    # (b) a fork terminal's payload is the fan-in REDUCE — unmodeled, so it may
    # never PROVE absence: warn at most (a false abort here rejects a legit experiment)
    fork_pipe = {
        "nodes": {"role:draft": dict(AGENT_RAW["nodes"]["role:draft"])},
        "graph": {"start": "f", "stages": {
            "f": {"fork": ["b"], "then": None},
            "b": {"node": "role:draft"},
            "j": {"fanin": {"expect": ["b"]}}}},
    }
    warns = check_variant_contracts("V", fork_pipe, None, inline, {"score": "score"})
    assert warns and all("score" in w for w in warns), warns

    # (b) `concerns` is engine-added at Done outside the lattice's sight — never
    # provably absent (warn at most)
    warns = check_variant_contracts("V", AGENT_RAW, None, inline,
                                    {"soft": "concerns.0"})
    assert len(warns) == 1 and "soft" in warns[0], warns

    # terminal detection mirrors the harness walk: branch default null can end the run
    stages = {
        "a": {"then": "b"},
        "b": {"branch": {"on": "k", "routes": {"x": "a", "stop": None}}},
        "c": {"branch": {"on": "k", "routes": {"x": "a"}, "default": "a"}},
    }
    assert terminal_stages(stages) == ["b"], terminal_stages(stages)

    print("PASS direct policy checks")


def entry_key_sets_check() -> None:
    with tempfile.TemporaryDirectory() as d:
        fx = _write(d, "in.json", {"text": "hi"})
        _write(d, "list.json", [1, 2])
        _write(d, "broken.json", "{nope")
        assert os.path.basename(fx) == "in.json"
        got = entry_key_sets([{"a": 1}, "in.json", "list.json", "broken.json", 7], d)
        assert got[0] == ("inline-0", frozenset({"a"})), got[0]
        assert got[1] == ("in.json", frozenset({"text"})), got[1]
        assert got[2] == ("list.json", None) and got[3] == ("broken.json", None), got
        assert got[4] == ("inline-4", None), got[4]
    print("PASS entry_key_sets: inline / fixture / unknowable")


# ---------------------------------------------------------------- end-to-end pre-flight

def _base_root(d: str, pipeline_name: str, default: str = "base-reply") -> None:
    os.makedirs(os.path.join(d, "prompts"), exist_ok=True)
    with open(os.path.join(d, "prompts", "draft.md"), "w") as f:
        f.write("draft it\n")
    _write(d, "base.json", {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": default}},
        "default_provider": "fake",
        "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
        "default_prompt_source": "file",
        "pipeline": pipeline_name,
        "run": True,
    })


def _experiment(exp_id: str, **over) -> dict:
    cfg = {"id": exp_id, "variants": {"A": "base.json"},
           "inputs": [{"request": "one"}], "repetitions": 1,
           "price_map": {"fake:m": {"input": 0, "output": 0}},
           "store": {"dir": ".ab"}}
    cfg.update(over)
    return cfg


def _run_capturing_stderr(cfg: dict, d: str):
    buf, old = io.StringIO(), sys.stderr
    sys.stderr = buf
    try:
        summary = asyncio.run(run_experiment(cfg, d))
    finally:
        sys.stderr = old
    return summary, buf.getvalue()


def end_to_end() -> None:
    # (a) input mismatch aborts BEFORE any run — no rows, message names it all
    with tempfile.TemporaryDirectory() as d:
        _base_root(d, "pipe.json")
        _write(d, "pipe.json", RENDER_TASK)
        exp = _experiment("mismatch", price_map={})
        _expect_abort(lambda: asyncio.run(run_experiment(exp, d)),
                      "'A'", "task", "inline-0")
        store = JsonlExperimentStore(os.path.join(d, ".ab"))
        assert asyncio.run(store.rows("mismatch")) == [], "no rows before the abort"

        # fixture inputs participate: keys read from the JSON file
        _write(d, "fx.json", {"request": "from-fixture"})
        _expect_abort(lambda: asyncio.run(run_experiment(
            dict(exp, id="mismatch-fx", inputs=["fx.json"]), d)),
            "'A'", "task", "fx.json")

    # (a) unknowable end-to-end: an opaque envelope-transform upstream provides
    # `task` at runtime — the check must NOT abort, and the run must complete
    with tempfile.TemporaryDirectory() as d:
        _base_root(d, "pipe.json")
        _write(d, "abt1_helpers.py",
               "def provide_task(envelope, config):\n"
               "    return {\"task\": \"from-transform\"}\n")
        _write(d, "pipe.json", {
            "nodes": {"role:mk": {"type": "transform", "call": "envelope",
                                  "target": "fn:abt1_helpers:provide_task"},
                      "role:tmpl": {"type": "render", "template_text": "do: {{task}}"}},
            "graph": {"start": "s1",
                      "stages": {"s1": {"node": "role:mk", "then": "s2"},
                                 "s2": {"node": "role:tmpl"}}},
        })
        summary, _ = _run_capturing_stderr(_experiment("opaque", price_map={}), d)
        assert summary["by_variant"]["A"]["done"] == 1, summary

    # (b) metric provably never produced → abort naming variant + metric; no rows
    with tempfile.TemporaryDirectory() as d:
        _base_root(d, "pipe.json")
        _write(d, "pipe.json", AGENT_RAW)
        exp = _experiment("dead-metric", metrics={"score": "score"})
        _expect_abort(lambda: asyncio.run(run_experiment(exp, d)),
                      "'A'", "'score'")
        store = JsonlExperimentStore(os.path.join(d, ".ab"))
        assert asyncio.run(store.rows("dead-metric")) == []

        # a malformed metric path (empty segment) is a SHAPE error, not a bogus
        # "provably never carries ''" proof
        _expect_abort(lambda: asyncio.run(run_experiment(
            dict(exp, id="bad-path", metrics={"score": ".score"}), d)),
            "metrics")

    # (b) declared-but-unproven → stderr WARNING, campaign runs to completion
    with tempfile.TemporaryDirectory() as d:
        _base_root(d, "pipe.json", default='{"ok": true}')
        _write(d, "pipe.json", {
            "nodes": {"role:draft": {"type": "agent", "prompt": "file:draft",
                                     "model": "fake:m"}},
            "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
        })
        exp = _experiment("soft-metric", metrics={"score": "score"})
        summary, err = _run_capturing_stderr(exp, d)
        assert summary["by_variant"]["A"]["done"] == 1, summary
        assert "score" in err and "'A'" in err, err

    print("PASS end-to-end: abort/skip/warn through run_experiment")


def shipped_example() -> None:
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "examples", "hello-yaah")
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, "hello-yaah")
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns(".ab", "__pycache__", "diagrams"))
        with open(os.path.join(dst, "experiment.json")) as f:
            cfg = json.load(f)
        summary, err = _run_capturing_stderr(cfg, dst)
        assert summary["rows"] == 6, summary          # 2 variants x 1 input x 3 reps
        assert summary["by_variant"]["A"]["done"] == 3, summary
        # its `score` metric is real-but-undeclared (variant B's model emits it,
        # no output_schema states it) — that is exactly the WARNING tier, not an abort
        assert "score" in err, err
    print("PASS shipped example: passes pre-flight (metric warned, not aborted)")


def main() -> None:
    direct_checks()
    entry_key_sets_check()
    end_to_end()
    shipped_example()
    print("PASS experiment contracts: input compatibility + metric plausibility")


if __name__ == "__main__":
    main()
