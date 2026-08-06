"""aggregate — reduce a trace record stream into cross-run metrics (R8).

Used by: a report stage / the A/B-recall experiment / a CLI, fed the JSONL a
FileTraceSink wrote. Run directly: `python -m yaah.trace.aggregate trace.jsonl
[price-map.json]`.
Where: the engine tracing core — PURE reduce logic (no I/O, no ports, no hot
path); the only I/O is the thin file-reading CLI at the bottom.
Why: the continuous-improvement payoff that motivated tracing — turn raw spans
into cost/task, per-stage latency percentiles, model mix, a tool histogram, and
a retry signal. The token->$ conversion is a CONFIG PRICE-MAP applied here in the
consumer, so history can be re-priced by editing config, never the engine. (The
Langfuse sink computes cost itself; this is the file-path equivalent.)

Price-map shape: {model: {"input": usd_per_1k_in, "output": usd_per_1k_out}},
with OPTIONAL "cache_read"/"cache_write" per-1k rates — omitted, they derive
from "input" via the standard multipliers below.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional

# Prompt-cache rate multipliers, relative to a model's plain input rate.
# Cached input is billed differently from fresh input: a cache READ is ~0.1x,
# a cache WRITE ~1.25x (the 5-minute-TTL write premium; a 1h-TTL write is 2x —
# the trace records no TTL, so a map that uses long-TTL caching should state an
# explicit "cache_write" rate rather than rely on this default).
CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def cost_usd(model: Optional[str], tokens_in: int, tokens_out: int,
             price_map: Optional[Dict[str, Any]], *,
             tokens_cache_read: int = 0, tokens_cache_write: int = 0) -> float:
    """Token cost for one model call via the price-map (per-1k rates). An unknown
    model (or no map) contributes 0.0 — cost is opt-in, never guessed.

    The three INPUT classes are priced SEPARATELY: `tokens_in` (fresh input) at
    the "input" rate, cache reads at `CACHE_READ_MULT` x that, cache writes at
    `CACHE_WRITE_MULT` x. A price-map entry may override either derived rate
    with an explicit "cache_read"/"cache_write" per-1k rate.

    BACK-COMPAT with traces written before the split (2026-08): those records
    carry only `tokens_in`, in which it is the SUM of all three classes, and the
    cache args default to 0 — so an old trace prices exactly as it always did,
    i.e. everything at the full input rate. Such totals are an UPPER BOUND, not
    a correction: they cannot be re-priced, because the split the arithmetic
    needs was never recorded."""
    if not price_map or model not in price_map:
        return 0.0
    p = price_map[model]
    in_rate = p.get("input", 0.0)
    read_rate = p.get("cache_read", in_rate * CACHE_READ_MULT)
    write_rate = p.get("cache_write", in_rate * CACHE_WRITE_MULT)
    return (tokens_in / 1000.0 * in_rate
            + tokens_cache_read / 1000.0 * read_rate
            + tokens_cache_write / 1000.0 * write_rate
            + tokens_out / 1000.0 * p.get("output", 0.0))


def record_cost_usd(r: Dict[str, Any],
                    price_map: Optional[Dict[str, Any]]) -> float:
    """Price ONE model_call record — THE pricing seam, shared by aggregate and
    the pretty/--cost renderers so they can never disagree. Prefers the CONFIG
    ref (`model_ref`, "provider:model" — what price maps are authored against)
    when the map knows it, else the backend-RESOLVED `model` name (older
    records / maps keyed by API names). No silent $0 from the dialect gap.

    `tokens_cache_read`/`tokens_cache_write` are OPTIONAL on the record (a
    backend that reports no caching, or a pre-split trace, omits them) and
    default to 0 — see cost_usd on what that means for old traces."""
    ref = r.get("model_ref")
    model = r.get("model")
    key = ref if (price_map and ref in price_map) else model
    return cost_usd(key, r.get("tokens_in", 0), r.get("tokens_out", 0), price_map,
                    tokens_cache_read=r.get("tokens_cache_read", 0) or 0,
                    tokens_cache_write=r.get("tokens_cache_write", 0) or 0)


def priced_key(r: Dict[str, Any],
               price_map: Optional[Dict[str, Any]]) -> Optional[str]:
    """The price-map key that would price this record, or None if the map has no
    entry for it. Same ref-preferred resolution as record_cost_usd, exposed so a
    renderer can tell "priced $0.00" (a real zero-token call) apart from
    "unpriced" (cost unknown) and never print a silent $0.00 for the latter."""
    if not price_map:
        return None
    ref = r.get("model_ref")
    if ref in price_map:
        return ref
    model = r.get("model")
    if model in price_map:
        return model
    return None


def percentile(values: List[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..100), stdlib-only so there's no
    numpy dependency. Empty -> 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q / 100.0 * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


def nearest_rank_percentile(values: List[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100), stdlib-only. Unlike `percentile`
    (linear-interpolated, used for stage latency), this returns an ACTUALLY
    OBSERVED value: sort ascending, take the value at rank ceil(q/100 * N)
    (1-indexed, clamped to [1, N]). Chosen for the --counts report so a reported
    p50/p95 is a real call duration the operator can go find in the trace, never
    a synthetic number between two calls. Empty -> 0.0; q<=0 -> the minimum.

    The rank product is rounded (9 dp) before ceil: binary floats make e.g.
    0.07 * 100 == 7.000000000000001, and a bare ceil would bump that to rank 8
    — an off-by-one for any (q, N) whose product carries such an artifact
    (never hit by the 50/95 the report uses, but wrong for a reused general q)."""
    if not values:
        return 0.0
    s = sorted(values)
    if q <= 0:
        return s[0]
    rank = math.ceil(round(q / 100.0 * len(s), 9))
    rank = max(1, min(rank, len(s)))
    return s[rank - 1]


def count_by_stage_model(records: Iterable[Dict[str, Any]],
                         *, price_map: Optional[Dict[str, Any]] = None
                         ) -> List[Dict[str, Any]]:
    """Invocation-count report (M12): group model_call records by
    (stage, model_ref, ladder-rung) and reduce each group to the columns the
    client reads — calls, tokens_in/out, cost, p50/p95 duration.

    Ladder rungs stay DISTINGUISHABLE: a record carrying `ladder_from` (the M7
    escalation second rung) forms a SEPARATE row from the rung-1 calls, even on
    the same (stage, model_ref) — never merged. `model_ref` (the config
    "provider:model" ref) is preferred for row identity, falling back to the
    resolved `model` name for older records.

    Duration percentiles are computed over model_call `duration_ms` (recorded per
    call by PhaseContributor); a record missing it contributes nothing rather
    than a fabricated 0. Cost uses the record_cost_usd seam; `priced` flags
    whether the model was in the price-map so the renderer can show an honest '-'
    for unpriced instead of a silent $0.00. Zero-token calls are KEPT (forensic
    signal). PURE. Rows are sorted stage asc, rung-1 before ladder, model asc."""
    groups: Dict[Any, Dict[str, Any]] = {}
    order: List[Any] = []
    for r in records:
        if r.get("name") != "model_call":
            continue
        stage = r.get("stage") or "?"
        model_ref = r.get("model_ref") or r.get("model") or "?"
        is_ladder = "ladder_from" in r
        key = (stage, model_ref, is_ladder)
        g = groups.get(key)
        if g is None:
            g = {"stage": stage, "model_ref": model_ref, "ladder": is_ladder,
                 "ladder_from": r.get("ladder_from"),
                 "calls": 0, "tokens_in": 0, "tokens_out": 0,
                 "tokens_cache_read": 0, "tokens_cache_write": 0,
                 "cost_usd": 0.0, "priced": False, "_durations": []}
            groups[key] = g
            order.append(key)
        g["calls"] += 1
        g["tokens_in"] += r.get("tokens_in", 0)
        g["tokens_out"] += r.get("tokens_out", 0)
        g["tokens_cache_read"] += r.get("tokens_cache_read", 0) or 0
        g["tokens_cache_write"] += r.get("tokens_cache_write", 0) or 0
        g["cost_usd"] += record_cost_usd(r, price_map)
        if priced_key(r, price_map) is not None:
            g["priced"] = True
        if "duration_ms" in r:
            g["_durations"].append(r.get("duration_ms", 0.0))

    rows: List[Dict[str, Any]] = []
    for key in order:
        g = groups[key]
        durs = g.pop("_durations")
        g["p50_ms"] = nearest_rank_percentile(durs, 50)
        g["p95_ms"] = nearest_rank_percentile(durs, 95)
        g["n_durations"] = len(durs)
        rows.append(g)
    rows.sort(key=lambda g: (g["stage"], g["ladder"], g["model_ref"]))
    return rows


def aggregate(records: Iterable[Dict[str, Any]],
              *, price_map: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Reduce trace records into metrics: per-run cost/tokens/duration, per-stage
    latency percentiles, per-model mix + cost, a tool histogram, and a retry
    signal (model_calls beyond stage spans = extra attempts). One pass; tolerant
    of partial records (missing captures just contribute nothing)."""
    runs: Dict[str, Dict[str, Any]] = {}
    stage_durations: Dict[str, List[float]] = {}
    models: Dict[str, Dict[str, Any]] = {}
    tools: Dict[str, int] = {}
    errors: List[Dict[str, Any]] = []  # "what went wrong" — spans whose status isn't ok/suspended
    n_stage_spans = 0
    n_model_calls = 0
    n_stage_failures = 0   # stage spans whose status isn't ok/suspended — used as the retry signal

    for r in records:
        name = r.get("name")
        corr = r.get("corr") or "?"
        status = r.get("status")
        if status is not None and status not in ("ok", "suspended"):
            errors.append({"name": name, "stage": r.get("stage") or r.get("role"),
                           "status": status, "detail": r.get("error") or r.get("detail")})
        run = runs.setdefault(corr, {"tokens_in": 0, "tokens_out": 0,
                                     "tokens_cache_read": 0, "tokens_cache_write": 0,
                                     "cost_usd": 0.0,
                                     "duration_ms": 0.0, "stages": 0, "model_calls": 0})
        if name == "stage":
            n_stage_spans += 1
            if status is not None and status not in ("ok", "suspended"):
                n_stage_failures += 1
            run["stages"] += 1
            run["duration_ms"] += r.get("duration_ms", 0.0)
            stage_durations.setdefault(r.get("stage", "?"), []).append(r.get("duration_ms", 0.0))
        elif name == "model_call":
            n_model_calls += 1
            run["model_calls"] += 1
            ti, to = r.get("tokens_in", 0), r.get("tokens_out", 0)
            # cached-input classes ride alongside tokens_in (absent on records
            # from a non-caching backend / a pre-split trace) — carried so the
            # operator can SEE the cache hit rate behind a cost figure
            cr = r.get("tokens_cache_read", 0) or 0
            cw = r.get("tokens_cache_write", 0) or 0
            model = r.get("model")
            c = record_cost_usd(r, price_map)
            run["tokens_in"] += ti
            run["tokens_out"] += to
            run["tokens_cache_read"] += cr
            run["tokens_cache_write"] += cw
            run["cost_usd"] += c
            m = models.setdefault(model or "?", {"calls": 0, "tokens_in": 0,
                                                 "tokens_out": 0,
                                                 "tokens_cache_read": 0,
                                                 "tokens_cache_write": 0,
                                                 "cost_usd": 0.0})
            m["calls"] += 1
            m["tokens_in"] += ti
            m["tokens_out"] += to
            m["tokens_cache_read"] += cr
            m["tokens_cache_write"] += cw
            m["cost_usd"] += c
        elif name == "tool_call":
            tools[r.get("tool", "?")] = tools.get(r.get("tool", "?"), 0) + 1

    stages = {name: {"count": len(ds),
                     "p50_ms": percentile(ds, 50), "p95_ms": percentile(ds, 95),
                     "max_ms": max(ds) if ds else 0.0}
              for name, ds in stage_durations.items()}

    totals = {
        "runs": len(runs),
        "tokens_in": sum(v["tokens_in"] for v in runs.values()),
        "tokens_out": sum(v["tokens_out"] for v in runs.values()),
        "tokens_cache_read": sum(v["tokens_cache_read"] for v in runs.values()),
        "tokens_cache_write": sum(v["tokens_cache_write"] for v in runs.values()),
        "cost_usd": sum(v["cost_usd"] for v in runs.values()),
        "stage_spans": n_stage_spans,
        "model_calls": n_model_calls,
        "tool_calls": sum(tools.values()),
        "errors": len(errors),  # "what went wrong" count
        # Retry signal (assessment cluster 5 #5): count error-status stage spans.
        # The old `n_model_calls - n_stage_spans` over-reported for tool-loop
        # stages (each loop turn is one model_call) and under-reported when a
        # retried attempt itself made no model call. Error-status stages = real
        # failed attempts. (A run-final hard failure also counts as one
        # "retry" — semantically close enough; the more useful distinction is
        # via the `errors` list, not the count.)
        "retries": n_stage_failures,
    }
    return {"runs": runs, "stages": stages, "models": models, "tools": tools,
            "errors": errors, "totals": totals}


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    """Read a FileTraceSink JSONL file into a list of records (the thin I/O at the
    edge; aggregate() itself is pure)."""
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def main() -> None:  # pragma: no cover - thin CLI wrapper over the tested core
    import sys
    args = sys.argv[1:]
    if not args:
        print("usage: python -m yaah.trace.aggregate <trace.jsonl> [price-map.json]")
        raise SystemExit(2)
    records = load_jsonl(args[0])
    price_map = None
    if len(args) > 1:
        with open(args[1], "r", encoding="utf-8") as f:
            price_map = json.load(f)
    print(json.dumps(aggregate(records, price_map=price_map), indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
