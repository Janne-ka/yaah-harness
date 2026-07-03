"""build_matrix + the campaign budget gate (AB-2a).

Contracts under test:
- the MATRIX groups rows by (variant, fingerprint) population; each cell
  carries N, outcome counts, cost stats (joined from the campaign trace by
  corr), duration stats, and declared payload METRICS — with statistical
  honesty: no winner is declared, N<2 cells are flagged insufficient, a
  variant whose population split mid-campaign is flagged
- run-level only: variants may differ in agent count/prompts (the s_factory
  sonnet-vs-haiku case), so per-stage cross-variant comparison is refused by
  construction — cells never contain stage names
Cost is measured from the forced trace, so this test registers a
usage-reporting provider through the PLUGINS seam (built-in fakes report no
usage) — which also dog-foods plugins end-to-end inside a campaign.
(The budget gate and experiment-contract validation are LATER slices per the
maintainer's scoping — not tested here.)

Run: cd yaah && PYTHONPATH=src python3 tests/test_experiment_report.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import textwrap

from yaah.experiment import build_matrix, run_experiment

PIPELINE = {
    "nodes": {
        "role:draft": {"type": "agent", "prompt": "file:draft",
                       "model": "usage:weak"},
    },
    "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
}

PLUGIN = """\
from yaah.plugins import register_type

class UsageProvider:
    def __init__(self, reply):
        self._reply = reply
    async def complete(self, prompt, *, model=None, on_usage=None, **opts):
        if on_usage:
            on_usage({{"tokens_in": 1000, "tokens_out": 1000, "model": model}})
        return self._reply

register_type("provider", "{type_name}",
              lambda spec, base: UsageProvider(spec.get("reply", '{{"score": 3}}')),
              spec_keys=["reply"])
"""


def _write(d: str, name: str, obj) -> str:
    path = os.path.join(d, name)
    with open(path, "w") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f)
    return path


def _setup(d: str, mod: str = "usage_ext_a") -> dict:
    """`mod` must be UNIQUE per tempdir within one process: flat plugin module
    names are cached by import name, and registered TYPE names are process-
    global with a collision guard — both engine fail-louds this test tripped
    over before parametrizing (the documented flat-name caveat, working)."""
    type_name = "usage_fake_" + mod.rsplit("_", 1)[-1]
    os.makedirs(os.path.join(d, "prompts"), exist_ok=True)
    _write(d, os.path.join("prompts", "draft.md"), "draft {{request}}\n")
    _write(d, mod + ".py", PLUGIN.format(type_name=type_name))
    _write(d, "pipe.json", PIPELINE)
    _write(d, "base.json", {
        "transport": {"type": "inproc"},
        "plugins": [mod],
        "providers": {"usage": {"type": type_name, "reply": '{"score": 3}'}},
        "default_provider": "usage",
        "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
        "default_prompt_source": "file",
        "pipeline": "pipe.json",
        "run": True,
    })
    _write(d, "pipe-b.json", {"_extends": "pipe.json",
                              "nodes": {"role:draft": {"model": "usage:strong"}}})
    _write(d, "b.json", {"_extends": "base.json",
                         "providers": {"usage": {"type": type_name,
                                                 "reply": '{"score": 5}'}},
                         "pipeline": "pipe-b.json"})
    return {
        "id": "score-ab",
        "variants": {"A": "base.json", "B": "b.json"},
        "inputs": [{"request": "one"}],
        "repetitions": 2,
        "metrics": {"score": "score"},
        # $1/1k in+out on weak, $2/1k on strong -> $2.00 / $4.00 per run
        "price_map": {"usage:weak": {"input": 1.0, "output": 1.0},
                      "usage:strong": {"input": 2.0, "output": 2.0}},
        "store": {"dir": ".ab"},
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        exp = _setup(d)
        asyncio.run(run_experiment(exp, d))

        matrix = asyncio.run(build_matrix(exp, d))
        cells = matrix["cells"]
        assert len(cells) == 2, cells       # one population per variant
        a = next(c for c in cells if c["variant"] == "A")
        b = next(c for c in cells if c["variant"] == "B")
        for c in (a, b):
            assert c["n"] == 2 and c["outcomes"]["done"] == 2, c
            assert "stage" not in json.dumps(c), "run-level only — no stage names"
        # cost joined by corr from the forced trace: 2k tokens/run at the rates
        assert abs(a["cost_usd"]["mean"] - 2.0) < 1e-6, a["cost_usd"]
        assert abs(b["cost_usd"]["mean"] - 4.0) < 1e-6, b["cost_usd"]
        assert a["cost_usd"]["n_priced"] == 2, a["cost_usd"]
        # declared metric extracted from the parsed payload
        assert a["metrics"]["score"]["mean"] == 3.0, a["metrics"]
        assert b["metrics"]["score"]["mean"] == 5.0, b["metrics"]
        # statistical honesty: no winner key anywhere; N>=2 -> no insufficiency
        assert "winner" not in json.dumps(matrix), matrix
        assert not a.get("insufficient_n"), a

        # N=1 cell is flagged, and the matrix-level warning names it
        exp1 = dict(exp, id="thin", repetitions=1)
        asyncio.run(run_experiment(exp1, d))
        m1 = asyncio.run(build_matrix(exp1, d))
        assert all(c["insufficient_n"] for c in m1["cells"]), m1
        assert any("N<2" in w for w in m1["warnings"]), m1["warnings"]

        # a mid-campaign prompt edit splits a variant's population and is flagged
        with open(os.path.join(d, "prompts", "draft.md"), "a") as f:
            f.write("terser\n")
        asyncio.run(run_experiment(exp, d))
        m2 = asyncio.run(build_matrix(exp, d))
        a_cells = [c for c in m2["cells"] if c["variant"] == "A"]
        assert len(a_cells) == 2, "population split must be a separate cell"
        assert any("population" in w and "'A'" in w for w in m2["warnings"]), m2["warnings"]
        # suspended/unpriced rows are COUNTED, never silently $0 — every row
        # here has a trace record, so the tally is zero across all cells
        assert all(c["cost_usd"]["n_unpriced"] == 0 for c in m2["cells"]), m2
        # ...and a DELETED trace makes them all unpriced (counted, no crash)
        os.remove(os.path.join(d, ".ab", "score-ab.trace.jsonl"))
        m3 = asyncio.run(build_matrix(exp, d))
        assert all(c["cost_usd"]["n_unpriced"] == c["n"] for c in m3["cells"]), m3

        # metric-path misses and non-numeric leaves are TALLIED, never guessed
        exp_m = dict(exp, id="score-ab", metrics={"score": "score",
                                                  "nope": "not.there",
                                                  "text": "raw"})
        m4 = asyncio.run(build_matrix(exp_m, d))
        c0 = m4["cells"][0]
        assert c0["metrics"]["nope"]["n"] == 0 and c0["metrics"]["nope"]["missing"] == c0["n"]
        assert c0["metrics"]["text"]["n"] == 0, "a string leaf is a miss, not a value"

        # ZERO-TOKEN runs are flagged, not silently $0.00 (eval Y1): a variant
        # on the builtin usage-less fake makes model calls with no tokens
        _write(d, "quiet.json", {
            "transport": {"type": "inproc"},
            "providers": {"usage": {"type": "fake", "default": '{"score": 1}'}},
            "default_provider": "usage",
            "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
            "default_prompt_source": "file",
            "pipeline": "pipe.json",
            "run": True,
        })
        exp_q = dict(exp, id="quiet-ab", variants={"Q": "quiet.json"},
                     price_map={"usage:weak": {"input": 1.0, "output": 1.0}})
        asyncio.run(run_experiment(exp_q, d))
        mq = asyncio.run(build_matrix(exp_q, d))
        qc = mq["cells"][0]
        assert qc["cost_usd"]["n_zero_token"] == qc["n"] > 0, qc
        assert any("ZERO tokens" in w for w in mq["warnings"]), mq["warnings"]

        # --report validates the experiment config like the run verb (eval Y2)
        try:
            asyncio.run(build_matrix({k: v for k, v in exp.items() if k != "id"}, d))
            raise AssertionError("report must reject a malformed experiment config")
        except ValueError as e:
            assert "id" in str(e), e

    # the CLI report path renders the same matrix (prose + --json)
    import io
    import sys as _sys
    from yaah.cli import _dispatch_ab
    with tempfile.TemporaryDirectory() as d2:
        exp2 = _setup(d2, mod="usage_ext_b")
        exp_path = _write(d2, "exp.json", dict(exp2, repetitions=2))
        asyncio.run(run_experiment(dict(exp2, repetitions=2), d2))
        for as_json in (False, True):
            buf, old = io.StringIO(), _sys.stdout
            _sys.stdout = buf
            try:
                _dispatch_ab({"experiment": exp_path, "report": True, "json": as_json})
            finally:
                _sys.stdout = old
            out = buf.getvalue()
            if as_json:
                m = json.loads(out)
                assert len(m["cells"]) == 2, m
            else:
                assert "N=2" in out and "cost" in out and "winner" not in out, out

    print("PASS build_matrix: cells/cost-join/metrics/honesty/population-split")


if __name__ == "__main__":
    main()
