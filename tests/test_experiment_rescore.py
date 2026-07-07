"""rescore_rows — zero-model-call re-scoring of a campaign's stored outputs (AB-2b).

The use-case (the client's measurement-gated contract changes, done by hand
twice before this existed): you tightened an output contract; before shipping
it, re-score the RAW outputs you already collected against the NEW schema —
zero model calls, deterministic, free — and gate the change on "no healthy
row newly rejected".

Contracts under test:
- rows are re-scored with the ENGINE'S OWN oracles (json.loads strict tier →
  extract_json recovery tier → reject; check_schema conform/mismatch) — the
  same functions the runtime enforces with, so the rescore can't drift from
  production behavior
- cells group by (variant, fingerprint) like the matrix; rows without a raw
  output (suspended/error) are counted `no_raw`, never guessed
- mismatch cells carry the failure detail (top schema errors) — a count alone
  is not actionable
- zero model calls: the whole thing runs offline on the rows file

Run: cd yaah && PYTHONPATH=src python3 tests/test_experiment_rescore.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah.experiment import rescore_rows, run_experiment

PIPELINE = {
    "nodes": {
        "role:draft": {"type": "agent", "prompt": "file:draft",
                       "model": "fake:m", "parse": False},
    },
    "graph": {"start": "s", "stages": {"s": {"node": "role:draft"}}},
}


def _write(d: str, name: str, obj) -> str:
    path = os.path.join(d, name)
    with open(path, "w") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f)
    return path


def _setup(d: str, reply: str) -> dict:
    os.makedirs(os.path.join(d, "prompts"), exist_ok=True)
    _write(d, os.path.join("prompts", "draft.md"), "draft\n")
    _write(d, "pipe.json", PIPELINE)
    _write(d, "base.json", {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": reply}},
        "default_provider": "fake",
        "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
        "default_prompt_source": "file",
        "pipeline": "pipe.json",
        "run": True,
    })
    return {
        "id": "rescore-t",
        "variants": {"A": "base.json"},
        "inputs": [{"x": 1}],
        "repetitions": 2,
        "price_map": {"fake:m": {"input": 0, "output": 0}},
        "store": {"dir": ".ab"},
    }


LOOSE = {"type": "object", "required": ["verdict"],
         "properties": {"verdict": {"type": "string"}}}
STRICT = {"type": "object", "required": ["verdict", "score"],
          "properties": {"verdict": {"enum": ["ship", "hold"]},
                         "score": {"type": "integer"}}}


def main() -> None:
    # STRICT-tier output: plain JSON, conforms to LOOSE, mismatches STRICT
    with tempfile.TemporaryDirectory() as d:
        exp = _setup(d, '{"verdict": "ship it"}')
        asyncio.run(run_experiment(exp, d))
        r = asyncio.run(rescore_rows(exp, d, LOOSE))
        c = r["cells"][0]
        assert c["n"] == 2 and c["tiers"]["strict"] == 2, c
        assert c["conform"]["pass"] == 2 and c["conform"]["fail"] == 0, c
        # the SAME rows against the tightened contract: the gate says NOT yet
        r2 = asyncio.run(rescore_rows(exp, d, STRICT))
        c2 = r2["cells"][0]
        assert c2["conform"]["fail"] == 2 and c2["conform"]["pass"] == 0, c2
        assert any("score" in e for e in c2["conform"]["top_errors"]), c2
        # zero model calls: the campaign trace did not grow during rescore
        trace = os.path.join(d, ".ab", "rescore-t.trace.jsonl")
        n_before = sum(1 for _ in open(trace))
        asyncio.run(rescore_rows(exp, d, STRICT))
        assert sum(1 for _ in open(trace)) == n_before

    # RECOVERED tier: fenced JSON parses only via extract_json
    with tempfile.TemporaryDirectory() as d:
        exp = _setup(d, 'Sure!\n```json\n{"verdict": "ship"}\n```')
        asyncio.run(run_experiment(exp, d))
        r = asyncio.run(rescore_rows(exp, d, LOOSE))
        c = r["cells"][0]
        assert c["tiers"]["strict"] == 0 and c["tiers"]["recovered"] == 2, c
        assert c["conform"]["pass"] == 2, c

    # REJECT tier: prose, no JSON anywhere — and the reject is counted, not guessed
    with tempfile.TemporaryDirectory() as d:
        exp = _setup(d, "I cannot help with that.")
        asyncio.run(run_experiment(exp, d))
        r = asyncio.run(rescore_rows(exp, d, LOOSE))
        c = r["cells"][0]
        assert c["tiers"]["reject"] == 2 and c["conform"]["pass"] == 0, c

    # no_raw contract + raw_key override + shape errors — against a hand-built
    # store so the row shapes are exact (a suspended row stores output: None)
    with tempfile.TemporaryDirectory() as d:
        from yaah.adapters.experiment_stores import JsonlExperimentStore
        store = JsonlExperimentStore(d)
        exp = {"id": "hand", "variants": {"A": "unused.json"}, "inputs": [{}],
               "price_map": {}, "store": {"dir": "."}}
        base_row = {"experiment_id": "hand", "variant": "A", "fingerprint": "f1",
                    "input_id": "i", "rep": 0, "t_start": 0, "t_end": 1}
        asyncio.run(store.append_row("hand", dict(base_row, outcome="suspended",
                                                  output=None)))
        asyncio.run(store.append_row("hand", dict(base_row, outcome="done",
                                                  output={"text": '{"verdict": "ship"}'})))
        r = asyncio.run(rescore_rows(exp, d, LOOSE, store=store))
        c = r["cells"][0]
        assert c["no_raw"] == 2, c   # suspended row AND relocated raw both uncounted...
        assert any("no raw output" in w for w in r["warnings"]), r["warnings"]
        r2 = asyncio.run(rescore_rows(exp, d, LOOSE, store=store, raw_key="text"))
        c2 = r2["cells"][0]
        assert c2["no_raw"] == 1 and c2["conform"]["pass"] == 1, c2  # ...raw_key finds it
        try:
            asyncio.run(rescore_rows(exp, d, "not-a-schema", store=store))
            raise AssertionError("non-dict schema must be rejected")
        except ValueError as e:
            assert "OBJECT" in str(e), e

    # the DECOY corner (eval catch): an all-decoy-key object parses under bare
    # json.loads but the RUNTIME rejects it — accept/reject must follow the
    # runtime's oracle, so this row is a REJECT, not a strict conform-pass
    with tempfile.TemporaryDirectory() as d:
        from yaah.adapters.experiment_stores import JsonlExperimentStore
        store = JsonlExperimentStore(d)
        exp = {"id": "decoy", "variants": {"A": "unused.json"}, "inputs": [{}],
               "price_map": {}, "store": {"dir": "."}}
        asyncio.run(store.append_row("decoy", {
            "experiment_id": "decoy", "variant": "A", "fingerprint": "f1",
            "input_id": "i", "rep": 0, "t_start": 0, "t_end": 1,
            "outcome": "done", "output": {"raw": '{"n_o_verdict": "ship"}'}}))
        r = asyncio.run(rescore_rows(exp, d, {"type": "object"}, store=store))
        c = r["cells"][0]
        assert c["tiers"]["reject"] == 1 and c["conform"]["pass"] == 0, \
            "the decoy guard must apply — the runtime would reject this row"

    # CLI: --rescore renders the tier table; --json emits the object;
    # --report + --rescore together are rejected at parse
    import io
    import sys as _sys
    from yaah.cli import _dispatch_ab, _parse_ab
    with tempfile.TemporaryDirectory() as d:
        exp = _setup(d, '{"verdict": "ship"}')
        exp_path = _write(d, "exp.json", exp)
        schema_path = _write(d, "loose-schema.json", LOOSE)
        asyncio.run(run_experiment(exp, d))
        buf, old = io.StringIO(), _sys.stdout
        _sys.stdout = buf
        try:
            _dispatch_ab({"experiment": exp_path, "rescore": schema_path})
        finally:
            _sys.stdout = old
        out = buf.getvalue()
        assert "2 strict" in out and "2 pass" in out, out
        spec = _parse_ab([exp_path, "--rescore", schema_path, "--json"])
        assert spec["rescore"] == schema_path and spec["json"]
        try:
            _parse_ab([exp_path, "--report", "--rescore", schema_path])
            raise AssertionError("--report + --rescore must be rejected")
        except SystemExit:
            pass
        try:
            _parse_ab([exp_path, "--rescore"])
            raise AssertionError("--rescore without a schema must be rejected")
        except SystemExit:
            pass

    print("PASS rescore_rows: strict/recovered/reject tiers + conform gate, zero model calls")


if __name__ == "__main__":
    main()
