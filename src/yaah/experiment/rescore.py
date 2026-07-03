"""rescore_rows — re-score a campaign's stored raw outputs, zero model calls (AB-2b).

Used by: `yaah ab <experiment.json> --rescore <schema.json>` and programmatic
callers.
Where: yaah.experiment — pure read over the rows the runner collected; runs
nothing, spends nothing.
Why: the measurement-gated contract change. You tightened an output schema
(or want to); before shipping it, re-score the raw outputs ALREADY collected
against the new contract and gate on "no healthy row newly rejected" — the
discipline the client ran by hand (their rescore.py, twice) that this
standardizes. Deterministic and free: iterate on the CONTRACT as fast as you
can edit JSON, no model in the loop.

Accept/reject is decided by extract_json — the SAME call, guards included,
the runtime parse path makes — so the rescore cannot drift from what the
runtime would enforce. Tiers on accepted rows are annotations:
  - tier `strict`    — plain json.loads would ALSO have parsed it (no
                       recovery was needed — a model speaking clean JSON)
  - tier `recovered` — only extract_json's recovery took it (fences, the
                       Y4/Y5 weak-executor shapes, ANCHORED by the CANDIDATE
                       schema's required keys — deliberately the candidate's:
                       the question is "what would the runtime do if THIS
                       schema were deployed", so a tier shift vs collection
                       time measures the CONTRACT's effect, not the model's)
  - tier `reject`    — extract_json raised (prose, no JSON) or a non-object
  - conform pass/fail — check_schema over the parsed object (the json_schema
                       validator's checker; failures carry the actual errors)

Rows without a raw output (suspended/error rows) are counted `no_raw`, never
guessed. Cells group by (variant, fingerprint), like the matrix.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..jsonio import extract_json
from ..jsonschema import check_schema
from .experiment_store import ExperimentStore

_TOP_ERRORS = 5   # enough detail to act on, bounded so a bad schema can't flood


async def rescore_rows(cfg: Dict[str, Any], base: str, schema: Dict[str, Any], *,
                       store: Optional[ExperimentStore] = None,
                       raw_key: str = "raw") -> Dict[str, Any]:
    """Re-score every collected row's raw output against `schema`.
    Returns {experiment, schema_required, cells: [...], warnings}. `raw_key`
    names where the raw text lives in the row's output payload (the agent
    convention is "raw"; a transform-shaped pipeline may relocate it)."""
    from .runner import _check_experiment
    from ..runtime_factories import _rel

    _check_experiment(cfg)
    if not isinstance(schema, dict):
        raise ValueError("rescore needs a JSON-Schema-subset OBJECT "
                         "(type/enum/required/properties/items)")
    exp_id = cfg["id"]
    if store is None:
        from ..adapters.experiment_stores import JsonlExperimentStore
        store = JsonlExperimentStore(_rel(base, (cfg.get("store") or {}).get("dir", ".ab")))
    rows = await store.rows(exp_id)
    required = (schema.get("required") or None)

    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["variant"], row["fingerprint"]), []).append(row)

    cells: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for (variant, fingerprint), grows in sorted(groups.items()):
        tiers = {"strict": 0, "recovered": 0, "reject": 0}
        conform = {"pass": 0, "fail": 0}
        errors: Dict[str, int] = {}
        no_raw = 0
        for r in grows:
            output = r.get("output")
            raw = output.get(raw_key) if isinstance(output, dict) else None
            if not isinstance(raw, str):
                no_raw += 1
                continue
            # extract_json is the SOLE accept/reject oracle — the same call the
            # runtime parse path makes, decoy/ambiguity guards included (an
            # earlier bare-json.loads "strict" tier BYPASSED those guards and
            # accepted one input class the deployed runtime rejects — eval-
            # probed). "strict" is a post-hoc ANNOTATION on accepted rows:
            # would plain json.loads also have taken it (no recovery needed)?
            parsed: Any = None
            try:
                parsed = extract_json(raw, keys=required, schema=schema)
                try:
                    json.loads(raw)
                    tier = "strict"
                except (json.JSONDecodeError, ValueError):
                    tier = "recovered"
            except json.JSONDecodeError:
                tier = "reject"
            if tier != "reject" and not isinstance(parsed, dict):
                tier = "reject"   # the contract is an object; a scalar/list is a miss
            tiers[tier] += 1
            if tier == "reject":
                continue
            errs = check_schema(parsed, schema, "$")
            if errs:
                conform["fail"] += 1
                for e in errs:
                    errors[e] = errors.get(e, 0) + 1
            else:
                conform["pass"] += 1
        top = [e for e, _ in sorted(errors.items(), key=lambda kv: -kv[1])[:_TOP_ERRORS]]
        cells.append({"variant": variant, "fingerprint": fingerprint,
                      "n": len(grows), "no_raw": no_raw, "tiers": tiers,
                      "conform": {**conform, "top_errors": top}})
        if no_raw:
            warnings.append(
                "cell ({!r}, {}…): {} row(s) have no raw output under {!r} "
                "(suspended/error rows, or the pipeline relocated it — see "
                "raw_key) and were NOT scored".format(
                    variant, fingerprint[:12], no_raw, raw_key))
    return {"experiment": exp_id, "schema_required": sorted(required or []),
            "cells": cells, "warnings": warnings}
