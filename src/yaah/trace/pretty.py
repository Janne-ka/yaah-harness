"""pretty — render a trace record stream as a human-readable per-run tree.

Used by: `yaah trace <jsonl> --pretty` (the operator's "show me what happened"
view). Complements `aggregate.py`, which reduces the same records into JSON
metrics — pretty is for debugging one run; aggregate is for measuring across
runs.
Where: the engine tracing core — PURE projection (no I/O), like aggregate.
Why: usability-gap #5 — the existing trace JSON answered "what was the cost"
but not "show me the stages, in order, with timing and errors." Reading raw
JSONL with `jq` was the workaround; this is the answer.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from .aggregate import count_by_stage_model, record_cost_usd


def _fmt_ms(ms: float) -> str:
    if ms >= 1000.0:
        return "{:.1f}s".format(ms / 1000.0)
    return "{:.0f}ms".format(ms)


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return "{:.1f}k".format(n / 1000.0)
    return str(n)


def _fmt_cost(usd: float) -> str:
    if usd <= 0.0:
        return ""
    if usd < 0.001:
        return "<$0.001"
    return "${:.3f}".format(usd)


def _cached(r: Dict[str, Any]) -> int:
    """A record's (or a counts row's) total cached input tokens — read + write.
    The two classes are priced apart (aggregate does that); for the human views
    one "how much of the input was cached" number is what an operator scans."""
    return (r.get("tokens_cache_read", 0) or 0) + (r.get("tokens_cache_write", 0) or 0)


def _cached_segment(n: int) -> str:
    """The cache side of a token count, rendered as `(+40.0k cached)` — empty
    when there was none. Kept as a separate SEGMENT, never folded into in→out:
    cached input is real traffic behind the $ number, but it is not fresh input
    and must never read as if it were."""
    if n <= 0:
        return ""
    return "(+{} cached)".format(_fmt_tokens(n))


def _status_glyph(status: Optional[str]) -> str:
    if status == "ok":
        return "✓"
    if status == "suspended":
        return "⏸"
    if status is None:
        return " "
    return "✗"


def keep_corr(records: Iterable[Dict[str, Any]], corr: str) -> List[Dict[str, Any]]:
    """Filter records to one correlation id (one run). Operator workflow:
    `yaah list` shows parked batons + their corr ids; `yaah trace x.jsonl
    --pretty --corr abc...` zooms in on that one run. Empty list if no match
    — the caller emits a clear "no records" placeholder via pretty()."""
    target = corr
    return [r for r in records if r.get("corr") == target]


def keep_last_runs(records: Iterable[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """Filter records to keep only the last N runs (by correlation id, in
    first-appearance order). When a long-lived trace.jsonl accumulates, the
    operator usually cares about the most recent runs; without this they pipe
    through `tail -<huge>` and guess at the boundary. n <= 0 returns records
    unchanged (the "no limit" sentinel)."""
    if n <= 0:
        return list(records)
    rec_list = list(records)
    seen_order: List[str] = []
    seen: set = set()
    for r in rec_list:
        c = r.get("corr", "?")
        if c not in seen:
            seen.add(c)
            seen_order.append(c)
    keep = set(seen_order[-n:])
    return [r for r in rec_list if r.get("corr", "?") in keep]


def _group_by_corr(records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group records by correlation id (one run = one corr), preserving order."""
    runs: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        runs.setdefault(r.get("corr", "?"), []).append(r)
    return runs


def _render_run(corr: str, records: List[Dict[str, Any]],
                price_map: Optional[Dict[str, Any]]) -> List[str]:
    """One run's section: header line + a parent→child tree of spans."""
    stages = [r for r in records if r.get("name") == "stage"]
    model_calls = [r for r in records if r.get("name") == "model_call"]
    tool_calls = [r for r in records if r.get("name") == "tool_call"]

    total_ms = sum(s.get("duration_ms", 0.0) for s in stages)
    tokens_in = sum(m.get("tokens_in", 0) for m in model_calls)
    tokens_out = sum(m.get("tokens_out", 0) for m in model_calls)
    tokens_cached = sum(_cached(m) for m in model_calls)
    total_cost = sum(record_cost_usd(m, price_map) for m in model_calls)

    def _plural(n: int, noun: str) -> str:
        return "{} {}{}".format(n, noun, "" if n == 1 else "s")

    header_bits = ["run {} — {}".format(corr, _fmt_ms(total_ms)),
                   _plural(len(stages), "stage")]
    if model_calls:
        header_bits.append(_plural(len(model_calls), "call"))
    if tool_calls:
        header_bits.append(_plural(len(tool_calls), "tool"))
    if tokens_in or tokens_out:
        header_bits.append("{}→{} tokens".format(_fmt_tokens(tokens_in),
                                                  _fmt_tokens(tokens_out)))
    if tokens_cached:
        header_bits.append(_cached_segment(tokens_cached))
    cost_str = _fmt_cost(total_cost)
    if cost_str:
        header_bits.append(cost_str)
    lines = [" · ".join(header_bits)]

    # children of a span (by parent id) — used to chain model/tool calls under
    # their stage. Stages themselves have a run-root parent we don't render.
    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        if r.get("name") == "stage":
            continue
        p = r.get("parent")
        if p is not None:
            by_parent.setdefault(p, []).append(r)

    for i, stage in enumerate(stages):
        is_last_stage = (i == len(stages) - 1)
        stem = "└─" if is_last_stage else "├─"
        stage_name = stage.get("stage") or stage.get("attrs", {}).get("stage", "?")
        status = stage.get("status")
        bits = ['stage "{}"'.format(stage_name),
                _fmt_ms(stage.get("duration_ms", 0.0)),
                _status_glyph(status)]
        if status not in (None, "ok", "suspended"):
            bits.append(str(status))   # "error" or a verdict label
        lines.append("{} {}".format(stem, " · ".join(bits)))

        children = by_parent.get(stage.get("id"), [])
        for j, child in enumerate(children):
            is_last_child = (j == len(children) - 1)
            child_stem = "   └─" if is_last_child else "   ├─"
            if not is_last_stage:
                child_stem = "│" + child_stem[1:]
            cname = child.get("name")
            if cname == "model_call":
                cbits = ["model_call",
                         child.get("model") or "?",
                         _fmt_ms(child.get("duration_ms", 0.0)),
                         "{}→{} tokens".format(_fmt_tokens(child.get("tokens_in", 0)),
                                               _fmt_tokens(child.get("tokens_out", 0)))]
                ccached = _cached_segment(_cached(child))
                if ccached:
                    cbits.append(ccached)
                ccost = _fmt_cost(record_cost_usd(child, price_map))
                if ccost:
                    cbits.append(ccost)
            elif cname == "tool_call":
                cbits = ["tool_call", child.get("tool") or "?",
                         _fmt_ms(child.get("duration_ms", 0.0))]
            else:
                cbits = [cname or "?", _fmt_ms(child.get("duration_ms", 0.0))]
            lines.append("{} {}".format(child_stem, " · ".join(cbits)))
    return lines


def _retry_cause(e: Dict[str, Any]) -> str:
    """The retry cause the phase capture projects onto a stage-error span,
    rendered as ` (feedback, attempt 2, error-retry 1)` — empty when it carried
    none.
    Without it four rejected attempts of one stage read as four anonymous
    failures, which is the first question of any postmortem. The two counters
    are kept SEPARATE (not "2/4"): `attempt` counts against `max_attempts`,
    the retry counter against the distinct `error_retries` budget."""
    bits: List[str] = []
    if e.get("retry"):
        bits.append(str(e["retry"]))
    if e.get("attempt") is not None:
        bits.append("attempt {}".format(e["attempt"]))
    # `error_retry_n` is the emitted key; bare `n` is the pre-2026-08 spelling,
    # read so an archived trace still renders its retry cause.
    retry_n = e.get("error_retry_n", e.get("n"))
    if retry_n is not None:
        bits.append("error-retry {}".format(retry_n))
    return " ({})".format(", ".join(bits)) if bits else ""


def _render_errors(records: List[Dict[str, Any]]) -> List[str]:
    """One-line-per-error rollup at the end. An error is any span whose status
    isn't ok/suspended/None; the message names the run, stage, detail and (when
    projected) the retry cause, so the operator knows where to look without
    scrolling back."""
    errs: List[Dict[str, Any]] = []
    for r in records:
        st = r.get("status")
        if st is not None and st not in ("ok", "suspended"):
            errs.append(r)
    if not errs:
        return []
    lines = ["", "errors:"]
    for e in errs:
        corr = e.get("corr", "?")
        stage = e.get("stage") or e.get("attrs", {}).get("stage") or e.get("name") or "?"
        detail = e.get("error") or e.get("detail") or e.get("status") or ""
        lines.append('  - run {} stage "{}": {}{}'.format(corr, stage, detail,
                                                          _retry_cause(e)))
    return lines


def cost_summary(records: Iterable[Dict[str, Any]],
                 *, price_map: Optional[Dict[str, Any]] = None) -> str:
    """Compact human cost rollup: totals + per-model breakdown. Aggregate.py
    computes the same numbers in JSON; this is the view operators read at the
    terminal when they want "how much did this cost". $ shown only when a
    price-map is provided — tokens-only otherwise (cost is opt-in, never
    guessed). Cached input rides as its own "(+Nk cached)" segment, present only
    when there was some — the in→out figure counts FRESH input, so on a
    cache-heavy run it alone understates the traffic behind the $. PURE."""
    rec_list = list(records)
    calls = [r for r in rec_list if r.get("name") == "model_call"]
    if not calls:
        return "no model calls\n"

    total_in = sum(r.get("tokens_in", 0) for r in calls)
    total_out = sum(r.get("tokens_out", 0) for r in calls)
    total_cached = sum(_cached(r) for r in calls)
    total_cost = sum(record_cost_usd(r, price_map) for r in calls)

    head_bits = ["{} model call{}".format(len(calls),
                                           "" if len(calls) == 1 else "s"),
                 "{}→{} tokens".format(_fmt_tokens(total_in), _fmt_tokens(total_out))]
    # cached input shown only when there IS some — a non-caching run's rollup
    # keeps the line it always had (and on a cache-heavy run the in→out figure
    # alone under-reports the traffic ~100x)
    if total_cached:
        head_bits.append(_cached_segment(total_cached))
    cost_str = _fmt_cost(total_cost)
    if cost_str:
        head_bits.append(cost_str)
    elif price_map:
        head_bits.append("(no priced models)")    # the operator provided a price-map
                                                   # but no model in it matched — honest signal
    lines = [" · ".join(head_bits), ""]

    # Per-model: calls, tokens_in→out, optional $. Sort by cost descending when
    # priced, by call count otherwise — most expensive / busiest first reads
    # naturally as a "where did it go" view.
    per_model: Dict[str, Dict[str, Any]] = {}
    for r in calls:
        m = r.get("model") or "?"
        d = per_model.setdefault(m, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                     "cached": 0, "cost_usd": 0.0})
        d["calls"] += 1
        d["tokens_in"] += r.get("tokens_in", 0)
        d["tokens_out"] += r.get("tokens_out", 0)
        d["cached"] += _cached(r)
        d["cost_usd"] += record_cost_usd(r, price_map)
    sort_key = "cost_usd" if total_cost > 0 else "calls"
    rows = sorted(per_model.items(), key=lambda kv: kv[1][sort_key], reverse=True)

    lines.append("by model:")
    for model, d in rows:
        row_bits = [model,
                    "{} call{}".format(d["calls"], "" if d["calls"] == 1 else "s"),
                    "{}→{} tokens".format(_fmt_tokens(d["tokens_in"]),
                                          _fmt_tokens(d["tokens_out"]))]
        if d["cached"]:
            row_bits.append(_cached_segment(d["cached"]))
        c = _fmt_cost(d["cost_usd"])
        if c:
            row_bits.append(c)
        lines.append("  " + " · ".join(row_bits))
    return "\n".join(lines) + "\n"


def counts_table(records: Iterable[Dict[str, Any]],
                 *, price_map: Optional[Dict[str, Any]] = None) -> str:
    """Invocation-count report as an aligned table — `yaah trace --counts`. One
    row per (stage, model, ladder rung), the columns the client reads instead of
    hand-rolling stats.json + jq: stage · model · calls · tokens_in · tokens_out
    · cost · p50 · p95, plus cache_read · cache_write when any row has cached
    input (a cache-heavy stage's tokens_in alone under-reports its traffic ~100x;
    the two classes stay APART because they price apart, and the columns stay
    away entirely on a non-caching trace so the common table keeps its width).

    Ladder second-rung rows (M7 escalation, records carrying `ladder_from`) are
    marked ' (ladder)' on the model cell and kept as SEPARATE rows — never merged
    into rung-1 (the client's hard invariant). Cost is honest: '-' when the model
    is unpriced (cost unknown, never a silent $0.00), a $ amount when priced —
    the same 'cost is opt-in' convention as --cost. Token counts are RAW integers
    (forensic precision — the client sums/diffs them); durations use the trace's
    ms/s formatting. Zero-token rows are kept. PURE; the CLI wraps load_jsonl +
    this. --counts --json emits count_by_stage_model() as machine JSON instead."""
    rows = count_by_stage_model(records, price_map=price_map)
    if not rows:
        return "no model calls\n"

    def _cost_cell(g: Dict[str, Any]) -> str:
        # priced -> a $ amount (incl. an explicit $0.0000 for a real zero-token
        # call); unpriced -> '-' so a $0.00 is never silently invented.
        if not g["priced"]:
            return "-"
        return "${:.4f}".format(g["cost_usd"])

    def _model_cell(g: Dict[str, Any]) -> str:
        return g["model_ref"] + (" (ladder)" if g["ladder"] else "")

    show_cache = any(_cached(g) for g in rows)
    headers = ["stage", "model", "calls", "tokens_in"]
    if show_cache:
        headers += ["cache_read", "cache_write"]
    headers += ["tokens_out", "cost", "p50", "p95"]
    body: List[List[str]] = []
    for g in rows:
        cells = [g["stage"], _model_cell(g), str(g["calls"]), str(g["tokens_in"])]
        if show_cache:
            cells += [str(g.get("tokens_cache_read", 0) or 0),
                      str(g.get("tokens_cache_write", 0) or 0)]
        cells += [str(g["tokens_out"]), _cost_cell(g),
                  _fmt_ms(g["p50_ms"]), _fmt_ms(g["p95_ms"])]
        body.append(cells)

    # column widths from header + body; text cols (stage, model) left-aligned,
    # the rest right-aligned so numbers line up for scanning.
    widths = [len(h) for h in headers]
    for r in body:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    left = {0, 1}

    def _fmt_row(cells: List[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.ljust(widths[i]) if i in left else cell.rjust(widths[i]))
        return "  ".join(out).rstrip()

    lines = [_fmt_row(headers), _fmt_row(["-" * w for w in widths])]
    for r in body:
        lines.append(_fmt_row(r))
    if any(not g["priced"] for g in rows):
        # mirror cost_summary's honest signal when pricing is partial/absent
        lines.append("")
        lines.append("- = unpriced (no price-map entry for this model)")
    return "\n".join(lines) + "\n"


def errors_only(records: Iterable[Dict[str, Any]]) -> Tuple[int, str]:
    """The CI-shaped view: print just the error rollup, exit code matches the
    presence (1) or absence (0) of errors. Composes as `yaah trace x.jsonl
    --errors-only` in a pre-commit hook or release check; silent + exit 0 on a
    clean run. PURE; the CLI wraps it with load_jsonl + print + SystemExit."""
    rec_list = list(records)
    lines = _render_errors(rec_list)
    if not lines:
        return 0, "no errors\n"
    return 1, "\n".join(lines).lstrip() + "\n"


def pretty(records: Iterable[Dict[str, Any]],
           *, price_map: Optional[Dict[str, Any]] = None) -> str:
    """Render a trace record stream as a human-readable per-run tree. PURE: no
    I/O, no ports — the CLI wraps load_jsonl + this for the operator path."""
    rec_list = list(records)
    runs = _group_by_corr(rec_list)
    if not runs:
        return "(no records)"
    total_stages = sum(1 for r in rec_list if r.get("name") == "stage")
    total_calls = sum(1 for r in rec_list if r.get("name") == "model_call")
    total_errors = sum(1 for r in rec_list
                       if r.get("status") not in (None, "ok", "suspended"))

    head_bits = ["{} run{}".format(len(runs), "s" if len(runs) != 1 else ""),
                 "{} stage{}".format(total_stages,
                                      "s" if total_stages != 1 else "")]
    if total_calls:
        head_bits.append("{} model call{}".format(total_calls,
                                                   "s" if total_calls != 1 else ""))
    if total_errors:
        head_bits.append("{} error{}".format(total_errors,
                                              "s" if total_errors != 1 else ""))
    out: List[str] = [" · ".join(head_bits), ""]
    for corr in runs:                        # preserve insertion order (first record wins)
        out.extend(_render_run(corr, runs[corr], price_map))
        out.append("")
    out.extend(_render_errors(rec_list))
    return "\n".join(out).rstrip() + "\n"
