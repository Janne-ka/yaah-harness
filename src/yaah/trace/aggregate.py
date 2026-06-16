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

Price-map shape: {model: {"input": usd_per_1k_in, "output": usd_per_1k_out}}.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


def cost_usd(model: Optional[str], tokens_in: int, tokens_out: int,
             price_map: Optional[Dict[str, Any]]) -> float:
    """Token cost for one model call via the price-map (per-1k rates). An unknown
    model (or no map) contributes 0.0 — cost is opt-in, never guessed."""
    if not price_map or model not in price_map:
        return 0.0
    p = price_map[model]
    return tokens_in / 1000.0 * p.get("input", 0.0) + tokens_out / 1000.0 * p.get("output", 0.0)


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
        run = runs.setdefault(corr, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
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
            model = r.get("model")
            c = cost_usd(model, ti, to, price_map)
            run["tokens_in"] += ti
            run["tokens_out"] += to
            run["cost_usd"] += c
            m = models.setdefault(model or "?", {"calls": 0, "tokens_in": 0,
                                                 "tokens_out": 0, "cost_usd": 0.0})
            m["calls"] += 1
            m["tokens_in"] += ti
            m["tokens_out"] += to
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
