"""check_variant_contracts — experiment-level contract pre-flight (AB-T1).

Used by: `run_experiment`'s per-variant pre-flight (`_preflight_variant`).
Where: yaah.experiment — this is experiment POLICY over the engine's data-flow
lattice (`yaah.dataflow`), which stays pure graph math.
Why: a campaign multiplies one config mistake by variants x inputs x repetitions
of real model spend. Two mistakes are checkable BEFORE any run because the
experiment, unlike the load-time lint, knows the concrete inputs:

- (a) shared-input compatibility. The experiment's inputs feed EVERY variant
  (the runner overrides each variant's `input` with them). When an input's key
  set is knowable (an inline dict; a fixture whose JSON reads as an object),
  seed the lattice with it as the entry payload. A RENDER that provably reads
  a key the input doesn't provide means every run reaching that stage fails
  with render_unfilled_placeholders → abort naming variant + input + key. A
  BRANCH key provably absent is NOT a failure (the harness routes absent →
  `branch.default`, the run completes) — but every run with that input taking
  the default lane may not be the comparison the author meant, so it WARNS.
  Unknowable input keys → skip silently (lattice honesty: never a false
  positive).
- (b) metric-path plausibility. Each declared metric is a dotted path into the
  terminal payload. If NO terminal payload can ever carry its top-level key
  (every reachable terminal's provides-out is closed and lacks it) → abort
  naming variant + metric. Declared-but-unproven → one WARNING per metric
  (returned; the runner prints to stderr) — the two-severity philosophy
  (ADR-0005/0006): provable mismatch fails loud, a contract gap only nags.

Conservatism (what keeps a LEGITIMATE experiment from being rejected) — each
mechanism below adds keys at runtime that the lattice can't see, so each is
WIDENED before analysis (widening only ever weakens claims: a skip or a
warning, never a false abort):
- fork/fanout/fanin stages hand forward a merged/reduced payload → opaque;
- human seams (a `human_gate` node; a stage with `escalate: "human"`) resume
  by MERGING the human's reply onto the payload (harness._merge_decision) →
  `closed` dropped past them, inbound keys kept;
- `concerns` (folded onto the Done payload) and every `concerns_into` key
  (engine-set on a stage's input) join the entry seed and sticky for (a), and
  are exempt from metric absence proofs in (b) (still unproven → warn).
Deeper metric-path segments (`review.score` past `review`) are beyond static
knowledge and not checked.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from ..dataflow import analyze_dataflow, stage_outflows, terminal_stages
from ..node_contract import Flow

# Synthetic node roles _widen_unmodeled_stages swaps in. An unknown TYPE resolves
# to the opaque contract (resolve_contract's sound skip); a declared envelope-
# transform resolves to preserve-without-`closed` (node_contract.preserve_declared).
_OPAQUE_ROLE = "__experiment_gather_opaque__"
_GATE_SHIM_ROLE = "__experiment_gate_shim__"
_ESCALATE_SHIM_ROLE = "__experiment_escalate_shim__"

# The stable machine-readable tag of the ONE analyze_dataflow error whose runtime
# meaning is "this run fails" (see module docstring on branch-key-absent).
_FATAL_TAG = "[dataflow: render-key-absent]"

EntryKeys = Optional[frozenset]   # None = unknowable (skip, never guess)


def entry_key_sets(inputs: List[Any], base: str) -> "List[Tuple[str, EntryKeys]]":
    """One (input_id, keys) pair per experiment input, ids matching the runner's
    row `input_id`s. keys is the entry payload's top-level key set when KNOWABLE —
    an inline dict, or a fixture path whose JSON reads as an object — else None.
    An unreadable/invalid fixture is None too: the runner's own existence check
    and the run itself own that failure; this check never doubles it."""
    from ..runtime_factories import _rel
    out: List[Tuple[str, EntryKeys]] = []
    for i, inp in enumerate(inputs):
        if isinstance(inp, dict):
            out.append(("inline-{}".format(i), frozenset(inp)))
        elif isinstance(inp, str):
            keys: EntryKeys = None
            try:
                with open(_rel(base, inp), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    keys = frozenset(data)
            except (OSError, ValueError):
                pass
            out.append((inp, keys))
        else:
            out.append(("inline-{}".format(i), None))
    return out


def _widen_unmodeled_stages(nodes: Dict[str, Any],
                            stages: Dict[str, Any]) -> "Tuple[Dict[str, Any], Dict[str, Any]]":
    """Swap the node of every stage whose runtime output the lattice models too
    tightly for an ENTRY-SEEDED analysis (see module docstring: fork/fanout/fanin
    → opaque; human seams → preserve that drops `closed`, keeping the gate's
    `decision`). The load-time lint tolerates the tight model because its entry
    is unknowable; a closed seed would otherwise carry proofs THROUGH these
    stages and reject working pipelines. Widening only ever weakens claims."""
    out_stages: Dict[str, Any] = {}
    shims: Dict[str, Dict[str, Any]] = {}
    for name, s in stages.items():
        if isinstance(s, dict):
            node_ref = s.get("node")
            node = nodes.get(node_ref) if isinstance(node_ref, str) else None
            if any(k in s for k in ("fork", "fanout", "fanin")):
                s = dict(s, node=_OPAQUE_ROLE)
                shims[_OPAQUE_ROLE] = {"type": _OPAQUE_ROLE}
            elif isinstance(node, dict) and node.get("type") == "human_gate":
                s = dict(s, node=_GATE_SHIM_ROLE)
                shims[_GATE_SHIM_ROLE] = {"type": "transform", "call": "envelope",
                                          "provides": ["decision"]}
            elif s.get("escalate") == "human":
                s = dict(s, node=_ESCALATE_SHIM_ROLE)
                shims[_ESCALATE_SHIM_ROLE] = {"type": "transform", "call": "envelope",
                                              "provides": []}
        out_stages[name] = s
    if not shims:
        return nodes, stages
    return dict(nodes, **shims), out_stages


def check_variant_contracts(variant: str, pipeline: Dict[str, Any],
                            base_path: Optional[str],
                            entries: "List[Tuple[str, EntryKeys]]",
                            metrics: Dict[str, str]) -> List[str]:
    """Run both experiment-level contract checks for one variant. Raises ValueError
    (the pre-flight abort) on a PROVABLE mismatch; returns the WARNING lines for the
    caller to print (kept pure for testability — the runner owns stderr).

    Preconditions (the runner's earlier pre-flight owns them): the pipeline passed
    validate_config, and `metrics` passed _check_experiment (dotted paths with
    non-empty segments — an empty head here would 'prove' nonsense)."""
    g = pipeline.get("graph") or {}
    stages = g.get("stages") or {}
    if not isinstance(stages, dict) or not stages:
        return []
    start = g.get("start")
    nodes, stages = _widen_unmodeled_stages(pipeline.get("nodes") or {}, stages)
    sticky = [k for k in (g.get("sticky") or []) if isinstance(k, str)]
    # engine-SET keys the lattice can't see: `concerns_into` puts a key on a stage's
    # input, `concerns` is folded onto the Done payload. For (a) they join the entry
    # seed and sticky (claiming presence only SUPPRESSES a provable-absence finding,
    # never makes one); for (b) they are exempt from absence PROOFS but still count
    # as unproven (warn).
    engine_set: Set[str] = {"concerns"}
    for s in stages.values():
        ci = s.get("concerns_into") if isinstance(s, dict) else None
        if isinstance(ci, str):
            engine_set.add(ci)
    engine_present = frozenset(k for k in engine_set if k)
    sticky_present = sticky + sorted(k for k in engine_present if k not in sticky)

    warnings: List[str] = []
    seeds: "Set[EntryKeys]" = set()
    for input_id, keys in entries:
        first_seen = keys not in seeds
        seeds.add(keys)
        if keys is None or not first_seen:
            continue   # unknowable → skip silently; duplicate key set → already checked
        errors, _ = analyze_dataflow(nodes, stages, sticky_present, start, base_path,
                                     entry=Flow(keys | engine_present,
                                                complete=True, closed=True))
        fatal = [e for e in errors if _FATAL_TAG in e]
        if fatal:
            raise ValueError(
                "variant {!r}: experiment input {!r} (provides keys {}) provably "
                "cannot drive this pipeline — every campaign run reaching the failing "
                "stage burns its cost on render_unfilled_placeholders. Fix the input, "
                "or the pipeline's entry contract:\n  - {}".format(
                    variant, input_id, sorted(keys), "\n  - ".join(fatal)))
        for e in errors:   # branch-key-absent: the run COMPLETES via branch.default
            warnings.append(
                "variant {!r}: experiment input {!r} provably makes a branch fall "
                "through to its default on every run — fine if the default lane is "
                "the intent for this input, otherwise fix the input. Detail: {} "
                "[ab: branch-default-only]".format(variant, input_id, e))

    if not metrics:
        return warnings
    terminals = terminal_stages(stages)
    flows: List[Flow] = []
    for keys in seeds:
        entry = None if keys is None else Flow(keys, complete=True, closed=True)
        outs = stage_outflows(nodes, stages, sticky, start, entry=entry)
        flows.extend(f for f in (outs.get(t) for t in terminals) if f is not None)
    if not flows:
        return warnings   # no reachable terminal to reason about — nothing provable
    for mname in sorted(metrics):
        head = metrics[mname].split(".")[0]
        if all(head in f.known for f in flows):
            continue   # every terminal payload carries the top-level key
        if head not in engine_set and all(f.closed and head not in f.known for f in flows):
            shapes = sorted({"{{{}}}".format(", ".join(sorted(f.known))) for f in flows})
            raise ValueError(
                "variant {!r}: metric {!r} reads payload path {!r}, but the terminal "
                "payload provably NEVER carries {!r} (terminal payloads are exactly "
                "{}) — every row's metric would be missing. Fix the metric path, or "
                "make a terminal-path node provide {!r} (an agent output_schema, a "
                "transform `provides`, or graph `sticky`).".format(
                    variant, mname, metrics[mname], head, ", ".join(shapes), head))
        warnings.append(
            "variant {!r}: metric {!r} (path {!r}) is declared but not proven — no "
            "contract on the terminal path guarantees {!r}, so rows may land with the "
            "metric missing. Declare it (an agent output_schema or a transform "
            "`provides`) to let pre-flight check it. [ab: metric-unproven]".format(
                variant, mname, metrics[mname], head))
    return warnings
