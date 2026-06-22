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

from .aggregate import cost_usd


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


def _status_glyph(status: Optional[str]) -> str:
    if status == "ok":
        return "✓"
    if status == "suspended":
        return "⏸"
    if status is None:
        return " "
    return "✗"


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
    total_cost = sum(cost_usd(m.get("model"), m.get("tokens_in", 0),
                              m.get("tokens_out", 0), price_map)
                     for m in model_calls)

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
                ccost = _fmt_cost(cost_usd(child.get("model"),
                                           child.get("tokens_in", 0),
                                           child.get("tokens_out", 0), price_map))
                if ccost:
                    cbits.append(ccost)
            elif cname == "tool_call":
                cbits = ["tool_call", child.get("tool") or "?",
                         _fmt_ms(child.get("duration_ms", 0.0))]
            else:
                cbits = [cname or "?", _fmt_ms(child.get("duration_ms", 0.0))]
            lines.append("{} {}".format(child_stem, " · ".join(cbits)))
    return lines


def _render_errors(records: List[Dict[str, Any]]) -> List[str]:
    """One-line-per-error rollup at the end. An error is any span whose status
    isn't ok/suspended/None; the message names the run, stage, and detail so
    the operator knows where to look without scrolling back."""
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
        lines.append('  - run {} stage "{}": {}'.format(corr, stage, detail))
    return lines


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
