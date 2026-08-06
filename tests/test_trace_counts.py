"""trace counts report: `yaah trace --counts` invocation-count table.

The client (s_factory) reads these columns per (stage, model, ladder rung):
stage · model_ref · calls · tokens_in · tokens_out · cost · p50/p95 duration.
Ladder second-rung records (M7 escalation — carry `ladder_from`) must aggregate
SEPARATELY from rung-1, never merged.

Run: cd yaah && PYTHONPATH=src python3 tests/test_trace_counts.py
"""
from __future__ import annotations

import json
import os
import tempfile

from yaah.trace.aggregate import (
    count_by_stage_model,
    load_jsonl,
    nearest_rank_percentile,
    record_cost_usd,
)
from yaah.trace.pretty import counts_table


def _model_call(corr, stage, model_ref, *, tin=100, tout=50, dur=100.0,
                model=None, ladder_from=None, cache_read=0, cache_write=0):
    """Build a model_call record the shape PhaseContributor + CostContributor
    emit (see record.py): duration_ms + status from phase, tokens/model/model_ref
    from cost, ladder_from from phase-projected span.attrs on the escalation rung.
    The two cache keys are ALWAYS present (zeros included) — their absence is
    what marks a pre-split record."""
    r = {"id": "m", "corr": corr, "name": "model_call", "parent": "s",
         "duration_ms": dur, "status": "ok",
         "tokens_in": tin, "tokens_out": tout,
         "tokens_cache_read": cache_read, "tokens_cache_write": cache_write,
         "model": model or model_ref, "model_ref": model_ref, "stage": stage}
    if ladder_from is not None:
        r["ladder_from"] = ladder_from
        r["ladder_trigger"] = "help"
    return r


# ---------------------------------------------------------------- percentiles

def scenario_nearest_rank_percentile_math() -> None:
    """Nearest-rank (NOT interpolated): a reported p50/p95 is always an OBSERVED
    duration. Verified at n=1, n=2, odd, even, and the q=0 edge."""
    assert nearest_rank_percentile([], 50) == 0.0
    # n=1: every percentile is the single value
    assert nearest_rank_percentile([10.0], 50) == 10.0
    assert nearest_rank_percentile([10.0], 95) == 10.0
    # n=2: p50 -> ceil(.5*2)=1 -> s[0]; p95 -> ceil(.95*2)=2 -> s[1]
    assert nearest_rank_percentile([10.0, 20.0], 50) == 10.0
    assert nearest_rank_percentile([20.0, 10.0], 95) == 20.0   # sorts first
    # odd n=3: p50 -> ceil(1.5)=2 -> s[1]; p95 -> ceil(2.85)=3 -> s[2]
    assert nearest_rank_percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert nearest_rank_percentile([10.0, 20.0, 30.0], 95) == 30.0
    # even n=4: p50 -> ceil(2)=2 -> s[1]; p95 -> ceil(3.8)=4 -> s[3]
    assert nearest_rank_percentile([10.0, 20.0, 30.0, 40.0], 50) == 20.0
    assert nearest_rank_percentile([10.0, 20.0, 30.0, 40.0], 95) == 40.0
    # q<=0 -> the minimum observed
    assert nearest_rank_percentile([5.0, 9.0], 0) == 5.0
    # q=100 -> the maximum observed
    assert nearest_rank_percentile([10.0, 20.0, 30.0], 100) == 30.0
    # float-artifact regression (eval finding): 0.07 * 100 is
    # 7.000000000000001 in binary floats — a bare ceil would return the 8th
    # value; nearest-rank says ceil(7) = rank 7. Exhaustive integer-q sweep
    # at N=100: rank must equal q exactly for every q.
    vals = [float(i) for i in range(1, 101)]      # 1..100, sorted
    for q in range(1, 101):
        assert nearest_rank_percentile(vals, q) == float(q), q


# ---------------------------------------------------------------- grouping

def scenario_multi_stage_multi_model() -> None:
    """Groups by (stage, model_ref): a run touching 2 stages x 2 models yields
    4 rows, each with truthful call counts and token sums."""
    recs = [
        _model_call("r1", "draft", "claude:sonnet", tin=1000, tout=200, dur=200.0),
        _model_call("r1", "draft", "claude:sonnet", tin=500, tout=100, dur=400.0),
        _model_call("r1", "draft", "claude:haiku", tin=50, tout=10, dur=50.0),
        _model_call("r1", "verify", "claude:sonnet", tin=300, tout=80, dur=120.0),
    ]
    rows = count_by_stage_model(recs)
    by = {(g["stage"], g["model_ref"]): g for g in rows}
    assert len(rows) == 3  # (draft,sonnet) (draft,haiku) (verify,sonnet)
    dsonnet = by[("draft", "claude:sonnet")]
    assert dsonnet["calls"] == 2
    assert dsonnet["tokens_in"] == 1500 and dsonnet["tokens_out"] == 300
    # p50 nearest-rank over [200,400] -> s[0]=200; p95 -> s[1]=400
    assert dsonnet["p50_ms"] == 200.0 and dsonnet["p95_ms"] == 400.0
    assert by[("draft", "claude:haiku")]["calls"] == 1
    assert by[("verify", "claude:sonnet")]["calls"] == 1


def scenario_ladder_rows_never_merge() -> None:
    """The client's hard invariant: a rung-2 record (ladder_from set) on the SAME
    (stage, model_ref) as rung-1 stays a SEPARATE row — never merged."""
    recs = [
        _model_call("r1", "draft", "claude:sonnet", tin=100, tout=20),
        _model_call("r1", "draft", "claude:sonnet", tin=200, tout=40),
        # escalation second rung, resolved to the SAME model_ref by config
        _model_call("r1", "draft", "claude:sonnet", tin=900, tout=300,
                    ladder_from="claude:haiku"),
    ]
    rows = count_by_stage_model(recs)
    assert len(rows) == 2, rows            # rung-1 group + ladder group, NOT 1
    rung1 = [g for g in rows if not g["ladder"]]
    ladder = [g for g in rows if g["ladder"]]
    assert len(rung1) == 1 and len(ladder) == 1
    assert rung1[0]["calls"] == 2 and rung1[0]["tokens_in"] == 300
    assert ladder[0]["calls"] == 1 and ladder[0]["tokens_in"] == 900
    assert ladder[0]["ladder_from"] == "claude:haiku"
    # table marks the ladder row distinguishably and keeps both stage rows
    out = counts_table(recs)
    assert "(ladder)" in out, out
    # two data rows for the same stage+model — one plain, one ladder
    assert out.count("claude:sonnet") == 2, out


def scenario_zero_token_calls_kept() -> None:
    """Zero-token model_calls are the client's forensic signal — keep the row."""
    recs = [_model_call("r1", "draft", "claude:sonnet", tin=0, tout=0, dur=10.0)]
    rows = count_by_stage_model(recs)
    assert len(rows) == 1
    assert rows[0]["calls"] == 1
    assert rows[0]["tokens_in"] == 0 and rows[0]["tokens_out"] == 0
    # rendered, not dropped
    assert "draft" in counts_table(recs)


def scenario_empty_trace() -> None:
    """No model_calls -> empty rows / a clear placeholder, never a crash."""
    assert count_by_stage_model([]) == []
    # stage-only records (no model_call) also yield no rows
    stage_only = [{"id": "s", "corr": "r", "name": "stage", "parent": "p",
                   "status": "ok", "duration_ms": 5.0, "stage": "noop"}]
    assert count_by_stage_model(stage_only) == []
    assert counts_table([]).strip() == "no model calls"


# ---------------------------------------------------------------- cost honesty

def scenario_unpriced_never_shows_zero_dollars() -> None:
    """An unpriced model shows the honest '-' (cost unknown), NEVER $0.00 — the
    same 'cost is opt-in, never guessed' convention as the --cost path."""
    recs = [_model_call("r1", "draft", "mystery:model", tin=100, tout=50)]
    # no price_map at all
    out = counts_table(recs)
    assert "$0.00" not in out, out
    assert "-" in out, out
    rows = count_by_stage_model(recs)
    assert rows[0]["priced"] is False and rows[0]["cost_usd"] == 0.0


def scenario_priced_cost_matches_seam() -> None:
    """A priced row's cost equals the record_cost_usd seam sum — the counts
    report can never disagree with --cost / aggregate."""
    price = {"claude:sonnet": {"input": 3.0, "output": 15.0}}
    recs = [
        _model_call("r1", "draft", "claude:sonnet", tin=1000, tout=500),
        _model_call("r1", "draft", "claude:sonnet", tin=200, tout=80),
    ]
    expected = sum(record_cost_usd(r, price) for r in recs)
    rows = count_by_stage_model(recs, price_map=price)
    assert abs(rows[0]["cost_usd"] - expected) < 1e-9
    assert rows[0]["priced"] is True
    out = counts_table(recs, price_map=price)
    assert "$" in out, out


def scenario_model_ref_preferred_for_pricing() -> None:
    """Pricing prefers the config ref (model_ref) over the resolved model name —
    the same one-dialect seam aggregate uses. A record whose model_ref is priced
    but resolved `model` is not still prices correctly."""
    price = {"claude:sonnet": {"input": 3.0, "output": 15.0}}
    recs = [_model_call("r1", "draft", "claude:sonnet", tin=1000, tout=0,
                        model="anthropic/claude-3-5-sonnet-20241022")]
    rows = count_by_stage_model(recs, price_map=price)
    assert rows[0]["priced"] is True
    assert abs(rows[0]["cost_usd"] - 3.0) < 1e-9   # 1000/1000 * 3.0


def scenario_cache_columns_appear_only_when_cached() -> None:
    """The shape that made --counts useless: 1k fresh input against 100k of cache
    read. A `tokens_in` column alone reports 1% of the traffic behind the row's
    cost, so a cached run grows cache_read/cache_write columns — RAW integers
    like the other token columns, and the two classes kept apart because they
    price apart. A non-caching trace keeps the narrow table it always had."""
    cached = [_model_call("r1", "draft", "claude:sonnet", tin=1000, tout=500,
                          cache_read=100000, cache_write=2000)]
    rows = count_by_stage_model(cached)
    assert rows[0]["tokens_cache_read"] == 100000, rows[0]
    assert rows[0]["tokens_cache_write"] == 2000, rows[0]
    out = counts_table(cached)
    assert "cache_read" in out and "cache_write" in out, out
    assert "100000" in out and "2000" in out, out
    # ...and the fresh-input column still says FRESH, not the sum
    assert "1000" in out, out

    # no cached tokens anywhere -> the columns stay away entirely
    plain = counts_table([_model_call("r1", "draft", "claude:sonnet")])
    assert "cache_read" not in plain and "cache_write" not in plain, plain


# ---------------------------------------------------------------- I/O behavior

def scenario_malformed_line_raises_like_existing() -> None:
    """--counts reads via load_jsonl, so a malformed line raises exactly like
    every other trace view (no silent skip that would drop forensic records)."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(_model_call("r1", "draft", "claude:sonnet")) + "\n")
            f.write("{not valid json\n")
        raised = False
        try:
            load_jsonl(path)
        except json.JSONDecodeError:
            raised = True
        assert raised, "load_jsonl must raise on a malformed line (existing behavior)"
    finally:
        os.remove(path)


# ---------------------------------------------------------------- JSON shape

def scenario_json_stable_shape() -> None:
    """--json composes to a machine shape: a list of row dicts with the exact
    columns the client reads. json.dumps must round-trip it."""
    recs = [
        _model_call("r1", "draft", "claude:sonnet", tin=100, tout=50, dur=100.0),
        _model_call("r1", "draft", "claude:sonnet", tin=100, tout=50, dur=100.0,
                    ladder_from="claude:haiku"),
    ]
    rows = count_by_stage_model(recs)
    dumped = json.loads(json.dumps(rows))           # must be JSON-serializable
    assert isinstance(dumped, list) and len(dumped) == 2
    required = {"stage", "model_ref", "ladder", "ladder_from", "calls",
                "tokens_in", "tokens_cache_read", "tokens_cache_write",
                "tokens_out", "cost_usd", "priced",
                "p50_ms", "p95_ms", "n_durations"}
    for row in dumped:
        assert required.issubset(row.keys()), (required - set(row.keys()))


def scenario_duration_from_truthful_field_only() -> None:
    """Percentiles come only from model_call records that actually carry
    duration_ms; a record missing it contributes nothing (never a fabricated 0
    that would skew p50)."""
    recs = [
        _model_call("r1", "draft", "claude:sonnet", dur=300.0),
        {"id": "m2", "corr": "r1", "name": "model_call", "parent": "s",
         "status": "ok", "tokens_in": 10, "tokens_out": 5,
         "model": "claude:sonnet", "model_ref": "claude:sonnet", "stage": "draft"},
    ]
    rows = count_by_stage_model(recs)
    assert rows[0]["calls"] == 2               # both calls counted
    assert rows[0]["n_durations"] == 1         # only one carried a duration
    assert rows[0]["p50_ms"] == 300.0          # not diluted by a phantom 0


def main() -> None:
    scenario_nearest_rank_percentile_math()
    scenario_multi_stage_multi_model()
    scenario_ladder_rows_never_merge()
    scenario_zero_token_calls_kept()
    scenario_empty_trace()
    scenario_unpriced_never_shows_zero_dollars()
    scenario_priced_cost_matches_seam()
    scenario_model_ref_preferred_for_pricing()
    scenario_cache_columns_appear_only_when_cached()
    scenario_malformed_line_raises_like_existing()
    scenario_json_stable_shape()
    scenario_duration_from_truthful_field_only()
    print("ok")


if __name__ == "__main__":
    main()
