"""run_experiment — the `yaah ab` batch campaign runner (AB-1, minimal honest cut).

Contracts under test (each a design-eval falsifier):
- variants are ROOT CONFIG PATHS (the `_extends` overlay-pair mechanism);
  every run appends a durable row {experiment_id, variant, fingerprint,
  input_id, rep, outcome, corr, raw output payload, timestamps}
- a FAILED run writes a row (outcome "failed", failure codes) — failures are
  data, the campaign continues
- editing only a PROMPT FILE between sessions changes the rows' fingerprint
  (populations stay distinct across the iterate loop)
- a model missing from price_map ABORTS up front (a confident $0.00 matrix is
  the reliability killer)
- live_config in any variant is REJECTED up front (the fingerprint can't be
  honest about per-invocation mutable re-reads)
- the runner FORCES cost capture into a per-experiment trace file so the
  report can attribute cost by corr

Run: cd yaah && PYTHONPATH=src python3 tests/test_experiment_runner.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah.adapters.experiment_stores import JsonlExperimentStore
from yaah.experiment import run_experiment

PIPELINE = {
    "nodes": {
        "role:draft": {"type": "agent", "prompt": "file:draft",
                       "model": "fake:weak", "parse": False},
    },
    "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
}


def _write(d: str, name: str, obj: dict) -> str:
    path = os.path.join(d, name)
    with open(path, "w") as f:
        json.dump(obj, f)
    return path


def _setup(d: str) -> dict:
    """Base root + a B variant overriding the model via a pipeline overlay —
    the two-file `_extends` pair the design names as the variant mechanic."""
    os.makedirs(os.path.join(d, "prompts"), exist_ok=True)
    with open(os.path.join(d, "prompts", "draft.md"), "w") as f:
        f.write("draft it\n")
    _write(d, "pipe.json", PIPELINE)
    _write(d, "base.json", {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "base-reply"}},
        "default_provider": "fake",
        "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
        "default_prompt_source": "file",
        "pipeline": "pipe.json",
        "run": True,
    })
    _write(d, "pipe-b.json", {"_extends": "pipe.json",
                              "nodes": {"role:draft": {"model": "fake:strong"}}})
    _write(d, "b.json", {"_extends": "base.json", "pipeline": "pipe-b.json"})
    return {
        "id": "model-ab",
        "variants": {"A": "base.json", "B": "b.json"},
        "inputs": [{"request": "one"}, {"request": "two"}],
        "repetitions": 2,
        "price_map": {"fake:weak": {"input": 0.0001, "output": 0.0002},
                      "fake:strong": {"input": 0.001, "output": 0.002}},
        "store": {"dir": ".ab"},
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        exp = _setup(d)

        summary = asyncio.run(run_experiment(exp, d))
        store = JsonlExperimentStore(os.path.join(d, ".ab"))
        rows = asyncio.run(store.rows("model-ab"))
        assert len(rows) == 8, len(rows)   # 2 variants x 2 inputs x 2 reps
        assert summary["rows"] == 8 and summary["by_variant"]["A"]["done"] == 4
        a_rows = [r for r in rows if r["variant"] == "A"]
        b_rows = [r for r in rows if r["variant"] == "B"]
        assert all(r["outcome"] == "done" for r in rows), rows
        assert all(r["output"]["raw"] == "base-reply" for r in a_rows)
        assert a_rows[0]["fingerprint"] != b_rows[0]["fingerprint"]
        assert all(r["corr"] for r in rows), "cost attribution needs corr on every row"
        assert {r["input_id"] for r in a_rows} == {"inline-0", "inline-1"}

        # the forced trace file exists and carries cost-capture model_call records
        trace_path = os.path.join(d, ".ab", "model-ab.trace.jsonl")
        assert os.path.exists(trace_path), os.listdir(os.path.join(d, ".ab"))
        recs = [json.loads(l) for l in open(trace_path) if l.strip()]
        assert any(r.get("name") == "model_call" and "tokens_in" in r for r in recs), \
            "cost capture must be FORCED into the experiment trace"

        # ITERATE loop: edit only the prompt file -> same variant name, NEW population
        with open(os.path.join(d, "prompts", "draft.md"), "a") as f:
            f.write("terser\n")
        asyncio.run(run_experiment(exp, d))
        rows2 = asyncio.run(store.rows("model-ab"))
        fps_a = {r["fingerprint"] for r in rows2 if r["variant"] == "A"}
        assert len(fps_a) == 2, "prompt edit must split the population by fingerprint"

        # a FAILED run is a row, not a crash: agent output fails a validator
        _write(d, "pipe-fail.json", {
            "nodes": {"role:draft": {"type": "agent", "prompt": "file:draft",
                                     "model": "fake:weak", "parse": False},
                      "role:chk": {"type": "expect_field", "key": "nope", "equals": True}},
            "graph": {"start": "s", "stages": {
                "s": {"node": "role:draft", "validators": ["role:chk"],
                      "max_attempts": 1}}},
        })
        _write(d, "fail.json", {"_extends": "base.json", "pipeline": "pipe-fail.json"})
        exp_fail = dict(exp, id="fail-ab", variants={"F": "fail.json"},
                        inputs=[{"x": 1}], repetitions=1)
        s2 = asyncio.run(run_experiment(exp_fail, d))
        frows = asyncio.run(store.rows("fail-ab"))
        assert len(frows) == 1 and frows[0]["outcome"] == "failed", frows
        assert frows[0]["failure"], "the why must travel onto the row"
        assert s2["by_variant"]["F"]["failed"] == 1

        # PRE-FLIGHT aborts: price-map gap; live_config variant
        exp_gap = dict(exp, id="gap",
                       price_map={"fake:weak": {"input": 1, "output": 1}})
        try:
            asyncio.run(run_experiment(exp_gap, d))
            raise AssertionError("price-map gap must abort before any run")
        except ValueError as e:
            assert "fake:strong" in str(e) and "price" in str(e).lower(), e
        assert asyncio.run(store.rows("gap")) == [], "no rows before the abort"

        _write(d, "live.json", {"_extends": "base.json", "live_config": True})
        exp_live = dict(exp, id="live", variants={"L": "live.json"})
        try:
            asyncio.run(run_experiment(exp_live, d))
            raise AssertionError("live_config variant must be rejected")
        except ValueError as e:
            assert "live_config" in str(e), e

        # a FIXTURE input path runs (experiment-relative) — and a typo'd one
        # aborts pre-flight instead of writing N junk error rows (eval R3)
        fx = _write(d, "fixture-in.json", {"request": "from-fixture"})
        exp_fx = dict(exp, id="fx", variants={"A": "base.json"},
                      inputs=["fixture-in.json"], repetitions=1)
        asyncio.run(run_experiment(exp_fx, d))
        fx_rows = asyncio.run(store.rows("fx"))
        assert fx_rows[0]["outcome"] == "done" and fx_rows[0]["input_id"] == "fixture-in.json"
        try:
            asyncio.run(run_experiment(dict(exp_fx, id="fx2",
                                            inputs=["no-such.json"]), d))
            raise AssertionError("missing fixture must abort pre-flight")
        except ValueError as e:
            assert "no-such.json" in str(e), e
        assert asyncio.run(store.rows("fx2")) == []

        # experiment-config shape errors are loud (incl. the typo'd-key class, eval R2)
        for bad, expect in [
            (dict(exp, variants={}), "variants"),
            (dict(exp, repetitions=0), "repetitions"),
            (dict(exp, inputs=[]), "inputs"),
            (dict(exp, repetitons=3), "unknown key"),
            ({k: v for k, v in exp.items() if k != "id"}, "id"),
            ({k: v for k, v in exp.items() if k != "price_map"}, "price_map"),
        ]:
            try:
                asyncio.run(run_experiment(bad, d))
                raise AssertionError("bad experiment config must be rejected: " + expect)
            except ValueError as e:
                assert expect in str(e), (expect, str(e))

    # the CLI verb drives the same runner and prints the summary
    import io
    import sys as _sys
    from yaah.cli import _dispatch_ab
    with tempfile.TemporaryDirectory() as d2:
        exp2 = _setup(d2)
        exp_path = _write(d2, "exp.json", dict(exp2, repetitions=1))
        buf, old = io.StringIO(), _sys.stdout
        _sys.stdout = buf
        try:
            _dispatch_ab({"experiment": exp_path})
        finally:
            _sys.stdout = old
        out = buf.getvalue()
        assert "'model-ab': 4 rows appended" in out, out
        assert "A" in out and "B" in out and "done" in out, out

    print("PASS run_experiment: rows/failures/fingerprint-iterate/pre-flight aborts")


if __name__ == "__main__":
    main()
