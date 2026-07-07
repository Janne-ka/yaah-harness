"""rollback — the `yaah rollback` verb's engine: read a run's trace, resolve
which completed stages declare an undo, show a menu, and (on --execute) call the
undo targets in reverse order and report honestly.

Used by: `cli.py::_dispatch_rollback` (the operator verb). NOT used inside a
running harness — this is a CLI reading files AFTER a run, so it never has a
comms bus. That is why it only ever calls `fn:`/`http:` targets (a `node:`
target needs comms; validate rejects it per ADR-0008 D1, and `_refuse` here is
the belt-and-braces refusal so a mis-authored config gives a named outcome, not
a crash).
Where: the seam between a persisted JSONL trace (the record of what ran + the
author-declared effect descriptor) and each node's declared `rollback` capability.
Why: `clear` drops engine state and `compensate` undoes the FAILING stage at
failure time; neither walks back the COMPLETED work of a whole run. Rollback is
that third, separate, human-triggered function (ADR-0008).

The ordering key is FILE APPEND POSITION (JSONL line index), never span
`t_start`: trace times are process-local monotonic, and a run RESUMED in a fresh
process — the rollback scenario — emits post-resume spans on a new zero-point, so
a t_start sort would interleave/invert the undo order. Append order is
chronological across processes by construction (ADR-0008 D3, design-eval #1).

Honest limits (ADR-0008 D4):
  - No rollback-of-rollback tracking. The verb records nothing; undo targets
    SHOULD be idempotent because running the verb twice re-calls them.
  - No dependency-aware ordering. Reverse completion order + the what-ran-after
    view is the whole ordering story; the engine does not model domain deps.
  - No transactional guarantee. Stop-on-first-failure + explicit
    `--accept-partial` is the entire consistency story; a half-unwound run is
    never silent, but it is possible if the operator opts in.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .external_call import call_target
from .runtime_factories import _read_json, _rel

_STAGE = "stage"        # span.name for a stage span (vs model_call / tool_call)
_COSTLY = "costly"
_CHEAP = "cheap"


class RollbackError(ValueError):
    """A rollback precondition the operator must fix (no persisted trace sink,
    missing trace file, unknown run, a `--only` name with no candidate). Subclasses
    ValueError so `cli.main`'s error boundary renders it as `error: <msg>` (exit 2)
    without a traceback — the message IS the fix."""


# ---------- loading the config + the trace ----------------------------------

def load_pipeline(root: Dict[str, Any], base: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """From a loaded root config, resolve its pipeline (a base-relative FILE path
    or an INLINE dict) and return `(nodes, stages)` — the node-id->config map and
    the graph's stage-name->wiring map. A candidate join walks
    stage-name -> stages[name]["node"] -> nodes[node_id]["rollback"]."""
    ref = root.get("pipeline")
    if isinstance(ref, dict):
        pipeline = dict(ref)
    elif isinstance(ref, str):
        pipeline = _read_json(_rel(base, ref))
    else:
        raise RollbackError(
            "root config has no `pipeline` (a file path or inline dict) — cannot "
            "map trace stages to their nodes' rollback declarations")
    nodes = pipeline.get("nodes") or {}
    stages = (pipeline.get("graph") or {}).get("stages") or {}
    return nodes, stages


def read_trace(root: Dict[str, Any], base: str) -> List[Dict[str, Any]]:
    """Locate the run's persisted JSONL trace from `root.trace.sinks` (the FIRST
    `{"type": "file"}` sink) and read its records. Loud, named failures — never a
    silent empty read — when there is no file sink (progress/stats/console sinks
    cannot feed the tool: they write human tails / aggregate snapshots, not the
    per-span record with the effect descriptor) or the file does not exist yet."""
    trace = root.get("trace") or {}
    sinks = trace.get("sinks")
    if sinks is None:
        specs: List[Any] = []
    elif isinstance(sinks, list):
        specs = sinks
    else:
        specs = [sinks]
    files = [s for s in specs if isinstance(s, dict) and s.get("type") == "file"]
    if not files:
        raise RollbackError(
            "rollback needs a persisted JSONL trace, but this root declares no file "
            "trace sink. Add a `trace.sinks` entry {\"type\": \"file\", \"path\": "
            "\"trace.jsonl\"} (console/progress/stats sinks cannot feed rollback — "
            "they record no per-stage effect descriptor).")
    path = _rel(base, files[0].get("path", "trace.jsonl"))
    if not os.path.exists(path):
        raise RollbackError(
            "trace file not found: {} — no run has been recorded here yet (or the "
            "sink path differs from this root's).".format(path))
    return _read_records(path)


def _read_records(path: str) -> List[Dict[str, Any]]:
    """Parse the append-only JSONL into records. Blank lines and any line that is
    not a JSON object are SKIPPED, not fatal: an append-only trace can end in a
    half-written line if the process died mid-write, and one corrupt line must not
    hide every good record before it."""
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


# ---------- candidate resolution --------------------------------------------

def _corr_records(records: List[Dict[str, Any]], corr: str) -> List[Dict[str, Any]]:
    """The run's records in FILE APPEND ORDER. Filtering preserves list order, so
    for a single corr this is exactly the global line order restricted to that run
    — the property that survives interleaved runs sharing one trace file."""
    return [r for r in records if r.get("corr") == corr]


def _is_completion(rec: Dict[str, Any]) -> bool:
    """A REAL stage-completion span — the only thing a candidate may come from
    (ADR-0008 D3, design-eval #4). Excludes: non-stage spans (model_call /
    tool_call); non-ok status; POINT spans (`t_start == t_end`); and resume notes
    (`resumed` attr) — a gate's resume note is a status-ok point span that would
    otherwise masquerade as a completed, undoable stage."""
    return (rec.get("name") == _STAGE
            and rec.get("status") == "ok"
            and rec.get("t_start") != rec.get("t_end")
            and "resumed" not in rec)


def _lookup_rollback(stage: Any, stages: Dict[str, Any],
                     nodes: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Join a trace stage name to its node's `rollback` block. Returns
    `(node_id, rollback_or_None)`. A stage the current pipeline no longer defines,
    or a node with no `rollback`, yields `None` for the block — reported as
    `impossible` (honestly unknown = irreversible, ADR-0008 D1), never guessed."""
    scfg = stages.get(stage)
    if not isinstance(scfg, dict):
        return None, None
    node_id = scfg.get("node")
    ncfg = nodes.get(node_id) if node_id else None
    if not isinstance(ncfg, dict):
        return node_id, None
    rb = ncfg.get("rollback")
    if not isinstance(rb, dict):
        return node_id, None
    return node_id, rb


def _effects(rec: Dict[str, Any]) -> Tuple[Any, bool, Optional[str]]:
    """The recorded effect descriptor for a completion span: `(effects, truncated,
    head)`. When the author's descriptor was too large to store the record carries
    `effects_truncated: true` + a plain-string `effects_head` and NO descriptor —
    the undo then receives `effects: None` and the menu shows a loud dropped
    warning (ADR-0008 D2)."""
    if rec.get("effects_truncated"):
        return None, True, rec.get("effects_head")
    return rec.get("effects"), False, None


def _ran_after(seq: List[Dict[str, Any]], idx: int) -> List[str]:
    """The distinct stage names that COMPLETED after this candidate (later line
    positions, same run) — the dependency-visibility a human needs to judge undo
    order, since the engine deliberately does not model domain dependencies.
    Filtered through `_is_completion` (the same gate as candidate selection): a
    later resume note / errored / point span is not a run of that stage, and
    listing it would pollute the one judgment aid the spec makes load-bearing —
    a candidate could even appear to depend on ITSELF via its own resume note."""
    out: List[str] = []
    seen = set()
    for r in seq[idx + 1:]:
        if _is_completion(r) and r.get("stage") is not None:
            s = r["stage"]
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _resolve(records: List[Dict[str, Any]], stages: Dict[str, Any],
             nodes: Dict[str, Any], corr: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split a run's real completions into `(candidates, impossible)` in FILE
    ORDER. Candidates carry occurrence numbers (a stage that ran twice — a
    branch-backward loop — yields two candidates keyed by file position, occurrence
    1 then 2). `_pos` is the internal line index used only for ordering."""
    seq = _corr_records(records, corr)
    candidates: List[Dict[str, Any]] = []
    impossible: List[Dict[str, Any]] = []
    counts: Dict[Any, int] = {}
    for idx, rec in enumerate(seq):
        if not _is_completion(rec):
            continue
        stage = rec.get("stage")
        node_id, rb = _lookup_rollback(stage, stages, nodes)
        if rb is None:
            impossible.append({"stage": stage, "node": node_id})
            continue
        counts[stage] = counts.get(stage, 0) + 1
        eff, trunc, head = _effects(rec)
        candidates.append({
            "stage": stage,
            "occurrence": counts[stage],
            "node": node_id,
            "cost": rb.get("cost", _CHEAP),
            "target": rb.get("target"),
            "effects": eff,
            "effects_truncated": trunc,
            "effects_head": head,
            "ran_after": _ran_after(seq, idx),
            "_pos": idx,
        })
    return candidates, impossible


def _public(c: Dict[str, Any]) -> Dict[str, Any]:
    """A candidate without the internal `_pos` ordering key — the shape emitted in
    the menu (and its `--json`)."""
    return {k: v for k, v in c.items() if k != "_pos"}


# ---------- the menu (pure read — calls NOTHING) ----------------------------

def build_menu(records: List[Dict[str, Any]], stages: Dict[str, Any],
               nodes: Dict[str, Any], corr: str) -> Dict[str, Any]:
    """The dry-run menu for one run: candidates in REVERSE completion order
    (latest line first — the order --execute would undo in) plus the `impossible`
    bucket. Pure read: this path never touches `call_target` (ADR-0008 invariant
    3). Raises if the run has no records in the trace."""
    if not _corr_records(records, corr):
        raise RollbackError(
            "no records for run {!r} in the trace — check the correlation id (run "
            "`yaah rollback <root>` with no id to list runs that have "
            "candidates).".format(corr))
    candidates, impossible = _resolve(records, stages, nodes, corr)
    return {
        "run": corr,
        "candidates": [_public(c) for c in reversed(candidates)],
        "impossible": impossible,
    }


def list_runs(records: List[Dict[str, Any]], stages: Dict[str, Any],
              nodes: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every run in the trace that has >=1 rollback candidate, in first-seen order,
    with its candidate count. The no-corr landing view."""
    order: List[str] = []
    seen = set()
    for r in records:
        c = r.get("corr")
        if c is not None and c not in seen:
            seen.add(c)
            order.append(c)
    out: List[Dict[str, Any]] = []
    for c in order:
        cands, _imp = _resolve(records, stages, nodes, c)
        if cands:
            out.append({"run": c, "candidates": len(cands)})
    return out


# ---------- execution -------------------------------------------------------

def _refuse(target: Any) -> Optional[str]:
    """A named refusal string if `target` is not an executable `fn:`/`http:`
    target, else None. Validate already restricts rollback targets to those two
    schemes (ADR-0008 D1); this is the defensive last line so a `node:` target
    (which needs a comms bus this CLI does not have) yields a reported failure, not
    a crash mid-run."""
    if not isinstance(target, str) or ":" not in target:
        return "rollback target {!r} is not a valid target string (want fn:/http:)".format(target)
    scheme = target.split(":", 1)[0]
    if scheme in ("fn", "http", "https"):
        return None
    return ("rollback target {!r} uses the {!r} scheme — the rollback CLI runs "
            "outside a harness and can only call fn:/http: targets".format(target, scheme))


def _entry(c: Dict[str, Any]) -> Dict[str, Any]:
    """The compact per-candidate identity used in every report bucket."""
    return {"stage": c["stage"], "occurrence": c["occurrence"], "node": c["node"]}


async def execute(records: List[Dict[str, Any]], stages: Dict[str, Any],
                  nodes: Dict[str, Any], corr: str, *, include_costly: bool = False,
                  only: Optional[List[str]] = None,
                  accept_partial: bool = False) -> Dict[str, Any]:
    """Walk the run's candidates in REVERSE completion order and call each undo
    `target` with ctx `{correlation_id, stage, node, effects, cost}` — DELIBERATELY
    NOT compensate's ctx (which hands the failing stage's full payload); rollback
    hands the bounded `effects` descriptor recorded at completion (ADR-0008 D3,
    design-eval #7). A `costly` candidate is skipped unless `include_costly`;
    `only` (stage names, all occurrences) restricts the set; execution STOPS on the
    first failed undo unless `accept_partial` (a half-unwound run is never silent).

    Returns the exactly-five-bucket report
    `{run, rolled_back, skipped_costly, impossible, failed, not_attempted}`.
    `impossible` is a first-class outcome, not an error. `not_attempted` holds the
    candidates past a stop; the report always lists the full `impossible` set even
    under `only`, so the operator never loses sight of what cannot be undone."""
    if not _corr_records(records, corr):
        raise RollbackError(
            "no records for run {!r} in the trace — nothing to roll back.".format(corr))
    candidates, impossible = _resolve(records, stages, nodes, corr)
    ordered = list(reversed(candidates))   # latest line first
    if only:
        names = {c["stage"] for c in candidates}
        missing = [s for s in only if s not in names]
        if missing:
            raise RollbackError(
                "--only names stage(s) with no rollback candidate for run {!r}: {} "
                "(candidates: {}).".format(
                    corr, ", ".join(missing), ", ".join(sorted(names)) or "none"))
        wanted = set(only)
        ordered = [c for c in ordered if c["stage"] in wanted]
    report: Dict[str, Any] = {
        "run": corr, "rolled_back": [], "skipped_costly": [],
        "impossible": impossible, "failed": [], "not_attempted": [],
    }
    for i, c in enumerate(ordered):
        if c["cost"] == _COSTLY and not include_costly:
            report["skipped_costly"].append(_entry(c))
            continue
        err = _refuse(c["target"])
        if err is None:
            ctx = {"correlation_id": corr, "stage": c["stage"], "node": c["node"],
                   "effects": c["effects"], "cost": c["cost"]}
            try:
                await call_target(c["target"], ctx)
            except Exception as e:   # the undo itself failed
                err = "{}: {}".format(type(e).__name__, e)
        if err is not None:
            failed = _entry(c)
            failed["error"] = err
            report["failed"].append(failed)
            if not accept_partial:
                report["not_attempted"] = [_entry(r) for r in ordered[i + 1:]]
                break
        else:
            report["rolled_back"].append(_entry(c))
    return report


# ---------- terminal rendering ----------------------------------------------

def _fmt_effects(c: Dict[str, Any]) -> str:
    if c.get("effects_truncated"):
        head = c.get("effects_head") or ""
        return "DESCRIPTOR DROPPED (too large) — head: {}".format(head)
    eff = c.get("effects")
    if eff is None:
        return "(none recorded)"
    s = json.dumps(eff, sort_keys=True)
    return s if len(s) <= 200 else s[:200] + " …"


def render_list(runs: List[Dict[str, Any]]) -> str:
    if not runs:
        return "(no runs with rollback candidates in the trace)\n"
    lines = ["runs with rollback candidates:"]
    for r in runs:
        n = r["candidates"]
        lines.append("  {}  ({} candidate{})".format(r["run"], n, "" if n == 1 else "s"))
    lines.append("")
    lines.append("Inspect one:  yaah rollback <root> <run>")
    return "\n".join(lines) + "\n"


def render_menu(menu: Dict[str, Any]) -> str:
    lines = ["rollback menu for run {}".format(menu["run"]),
             "  (dry run — nothing is called; add --execute to undo)", ""]
    cands = menu["candidates"]
    if not cands:
        lines.append("  (no rollback candidates — nothing declares an undo)")
    for i, c in enumerate(cands, 1):
        occ = "" if c["occurrence"] == 1 and _occ_count(cands, c["stage"]) == 1 \
            else " (occurrence {})".format(c["occurrence"])
        lines.append("  [{}] stage {}{}  node {}  cost {}".format(
            i, c["stage"], occ, c["node"], c["cost"]))
        lines.append("      effects: {}".format(_fmt_effects(c)))
        ran = c["ran_after"]
        lines.append("      ran after: {}".format(", ".join(ran) if ran else "(nothing)"))
    imp = menu["impossible"]
    if imp:
        lines.append("")
        lines.append("impossible (no rollback declared — treated as irreversible):")
        for e in imp:
            lines.append("  - stage {}  node {}".format(e["stage"], e["node"]))
    return "\n".join(lines) + "\n"


def _occ_count(cands: List[Dict[str, Any]], stage: Any) -> int:
    return sum(1 for c in cands if c["stage"] == stage)


def _fmt_entries(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return "(none)"
    parts = []
    for e in entries:
        # candidate buckets carry an occurrence; the `impossible` bucket (never a
        # candidate) carries stage + node only.
        label = "{}#{}".format(e["stage"], e["occurrence"]) if "occurrence" in e \
            else "{} (node {})".format(e["stage"], e.get("node"))
        if e.get("error"):
            label += " [{}]".format(e["error"])
        parts.append(label)
    return ", ".join(parts)


def render_report(report: Dict[str, Any]) -> str:
    lines = ["rollback report for run {}".format(report["run"])]
    for key, label in (("rolled_back", "rolled back"),
                       ("skipped_costly", "skipped (costly)"),
                       ("impossible", "impossible"),
                       ("failed", "failed"),
                       ("not_attempted", "not attempted")):
        lines.append("  {:<16}: {}".format(label, _fmt_entries(report[key])))
    return "\n".join(lines) + "\n"
