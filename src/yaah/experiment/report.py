"""build_matrix — the campaign comparison matrix (AB-2a).

Used by: `yaah ab <experiment.json> --report` and programmatic callers.
Where: yaah.experiment — reads what the runner wrote (rows via the
ExperimentStore, cost via the campaign trace file) and reduces; runs nothing.
Why: the matrix is the DECISION surface of the cost-vs-performance loop — and
it must be statistically honest or it decides wrong:

- cells are (variant, fingerprint) POPULATIONS, not variant names — a
  mid-campaign edit splits a variant into two cells and the matrix says so
  (mixing them silently is how a "winner" gets crowned on stale data);
- run-level ONLY: variants may differ in agent count and prompts (the
  sonnet-vs-haiku case), so the only honest comparison units are per-run
  totals (cost, duration) and final-outcome quality — cells never mention
  stages;
- NO winner field, ever — the matrix presents; the human (or a judge
  pipeline) decides. N<2 cells are flagged `insufficient_n` and warned
  (single-run "wins" are noise; the documented convention is N=20/cell);
- cost is MEASURED (joined from trace records by corr, priced by the same
  rate-card dialect `yaah trace` uses); rows whose corr has no trace record
  (suspended runs, v1) are counted in `n_unpriced`, never silently $0.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import statistics
from typing import Any, Dict, List, Optional

from .experiment_store import ExperimentStore


def _stats(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0}
    return {"n": len(values),
            "mean": statistics.fmean(values),
            "min": min(values), "max": max(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0}


def _extract(payload: Any, path: str) -> Optional[float]:
    """A declared metric is a dotted path into the row's output payload; only
    numeric leaves count (a non-numeric hit is a miss, tallied not guessed)."""
    cur = payload
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    if isinstance(cur, bool) or not isinstance(cur, (int, float)):
        return None
    return float(cur)


async def build_matrix(cfg: Dict[str, Any], base: str, *,
                       store: Optional[ExperimentStore] = None) -> Dict[str, Any]:
    """Reduce a campaign's rows + trace into {cells, warnings}. Pure read —
    safe to run mid-campaign; it reports whatever has landed so far."""
    from ..runtime_factories import _rel
    from ..trace.aggregate import aggregate, load_jsonl
    from .runner import _check_experiment

    _check_experiment(cfg)   # same loud shape check as the run verb (eval Y2:
    # a malformed config gave raw KeyError/AttributeError tracebacks here)
    exp_id = cfg["id"]
    store_dir = _rel(base, (cfg.get("store") or {}).get("dir", ".ab"))
    if store is None:
        from ..adapters.experiment_stores import JsonlExperimentStore
        store = JsonlExperimentStore(store_dir)
    rows = await store.rows(exp_id)

    trace_path = os.path.join(store_dir, "{}.trace.jsonl".format(exp_id))
    per_run: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(trace_path):
        per_run = aggregate(load_jsonl(trace_path),
                            price_map=cfg.get("price_map"))["runs"]

    metric_paths: Dict[str, str] = cfg.get("metrics") or {}
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["variant"], row["fingerprint"]), []).append(row)

    cells: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for (variant, fingerprint), grows in sorted(groups.items()):
        outcomes: Dict[str, int] = {}
        costs: List[float] = []
        durations: List[float] = []
        unpriced = 0
        zero_token = 0   # model calls happened but NO tokens were reported —
        # a non-usage-reporting backend reads as a confident $0.00 otherwise
        # (eval Y1: the silent-$0 class arriving via the token axis)
        metric_values: Dict[str, List[float]] = {m: [] for m in metric_paths}
        metric_missing: Dict[str, int] = {m: 0 for m in metric_paths}
        for r in grows:
            outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
            durations.append(float(r.get("t_end", 0) - r.get("t_start", 0)))
            run = per_run.get(r.get("corr") or "")
            if run is None:
                unpriced += 1   # suspended rows (no corr, v1) or trace gap — named, not $0
            else:
                if (run.get("model_calls", 0) > 0
                        and run.get("tokens_in", 0) + run.get("tokens_out", 0) == 0):
                    zero_token += 1
                costs.append(float(run.get("cost_usd", 0.0)))
            for m, path in metric_paths.items():
                v = _extract(r.get("output"), path)
                if v is None:
                    metric_missing[m] += 1
                else:
                    metric_values[m].append(v)
        cell: Dict[str, Any] = {
            "variant": variant, "fingerprint": fingerprint, "n": len(grows),
            "outcomes": outcomes,
            "cost_usd": {**_stats(costs), "n_priced": len(costs),
                         "n_unpriced": unpriced, "n_zero_token": zero_token},
            "duration_s": _stats(durations),
            "metrics": {m: {**_stats(vs), "missing": metric_missing[m]}
                        for m, vs in metric_values.items()},
            "insufficient_n": len(grows) < 2,
        }
        cells.append(cell)
        if cell["insufficient_n"]:
            warnings.append(
                "cell ({!r}, {}…) has N<2 — a single run is noise, not a "
                "comparison (convention: N=20/cell)".format(variant, fingerprint[:12]))
        if zero_token:
            warnings.append(
                "cell ({!r}, {}…): {} run(s) made model calls but reported ZERO "
                "tokens — the backend doesn't report usage, so their $0.00 is "
                "blindness, not thrift; don't compare cost on this cell".format(
                    variant, fingerprint[:12], zero_token))
    by_variant: Dict[str, int] = {}
    for c in cells:
        by_variant[c["variant"]] = by_variant.get(c["variant"], 0) + 1
    for variant, n in sorted(by_variant.items()):
        if n > 1:
            warnings.append(
                "variant {!r} has {} populations (config edited mid-campaign) — "
                "compare cells, not the variant name".format(variant, n))
    return {"experiment": exp_id, "cells": cells, "warnings": warnings}
