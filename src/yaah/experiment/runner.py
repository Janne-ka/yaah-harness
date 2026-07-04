"""run_experiment — the `yaah ab` batch campaign runner (AB-1).

Used by: the `yaah ab` CLI verb; programmatic callers get the same function.
Where: yaah.experiment — composes ONLY public engine behavior: variant roots
load through the same `_extends`-resolving reader the runtime uses, validate
through validate_config, run through runtime.run_root, and land rows through
the ExperimentStore port.
Why: the product loop the maintainer named — balance cost vs performance by
running variants (on real providers when the campaign says so), RELIABLY
collecting a row per run, editing the setup, and collecting more — with the
winning variant becoming production by merging its overlay.

The reliability stance (each point test-pinned):
- PRE-FLIGHT aborts before any model call: experiment-config shape, per-variant
  validate_config, `live_config` rejection (per-invocation mutable re-reads
  would make the fingerprint a lie), explicit `model` on every model-calling
  node + full price_map coverage (a silent $0.00 in the matrix is the failure
  mode that kills trust in the data), and the experiment-level CONTRACT checks
  (contracts.py): an input that provably breaks a variant's render, or a
  metric path provably never produced, aborts; an input that only forces a
  branch to its default, and a declared-but-unproven metric, warn on stderr
  (the ADR-0005/0006 two-severity split).
- EVERY run lands a row — done, suspended (parked at a gate), failed (the
  verdict codes travel), errored — and the campaign continues; failures are
  data. Rows carry the variant's config FINGERPRINT (root + pipeline + prompt
  file bytes) so mid-campaign edits split populations instead of mixing them.
- Cost capture is FORCED: the effective root's `trace` is replaced with a
  cost-capturing file sink into the campaign's own trace file; the report
  joins rows to cost by corr. (The variant's own trace config does not apply
  during a campaign — the experiment owns observability; run production
  configs outside `yaah ab` to use their own sinks.)

Suspended rows carry baton_id but corr=None — resuming a parked experiment
run happens outside the campaign and its post-park cost is not attributed
(v1 limit; the live-assignment design owns this later).

Targets Python 3.9+.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from ..harness import Done, StageFailed, Suspended
from .fingerprint import config_fingerprint
from .experiment_store import ExperimentStore

_MODEL_NODE_TYPES = ("agent", "agent_loop")


# Every key the runner reads. An unknown key is a silent no-op — a typo'd
# `repetitons: 20` quietly running 1 rep is the silent-misconfig class
# (eval catch R2). `note`/`_*` are the config-comment conventions.
_EXPERIMENT_KEYS = frozenset({
    "id", "variants", "inputs", "repetitions", "price_map", "store", "note",
    "metrics",   # {name: dotted payload path} — read by the report AND by the
                 # pre-flight plausibility check (contracts.py); validated in _check
})


def _check_experiment(cfg: Dict[str, Any]) -> None:
    """Loud shape check for the experiment config itself. Same philosophy as
    validate_root: every key the runner reads is checked, unknown keys are
    rejected, and the message is the fix."""
    errs: List[str] = []
    for k in cfg:
        if k not in _EXPERIMENT_KEYS and not k.startswith("_"):
            errs.append("unknown key {!r}; known: {}".format(
                k, ", ".join(sorted(_EXPERIMENT_KEYS))))
    if not (isinstance(cfg.get("id"), str) and cfg.get("id")):
        errs.append("`id` must be a non-empty string — rows are keyed by it")
    variants = cfg.get("variants")
    if not (isinstance(variants, dict) and variants
            and all(isinstance(k, str) and isinstance(v, str) and v
                    for k, v in variants.items())):
        errs.append("`variants` must be a non-empty map of name -> root-config path "
                    "(each a normal yaah root; use _extends overlays off the base)")
    inputs = cfg.get("inputs")
    if not (isinstance(inputs, list) and inputs):
        errs.append("`inputs` must be a non-empty list (inline payload objects "
                    "and/or fixture paths)")
    reps = cfg.get("repetitions", 1)
    if not (isinstance(reps, int) and not isinstance(reps, bool) and reps >= 1):
        errs.append("`repetitions` must be an int >= 1 (LLM variance needs N; "
                    "the report refuses N<2 winners)")
    if not isinstance(cfg.get("price_map"), dict):
        # SAME dialect as the trace layer (aggregate.cost_usd / yaah trace):
        # {model: {input, output}} in $ per 1k tokens — one rate-card shape
        # everywhere, or the report joins would silently price $0.00 (eval R1)
        errs.append("`price_map` is required (model -> {input, output} $/1k "
                    "tokens — the same shape `yaah trace` prices with); without "
                    "it the cost axis would silently read $0.00")
    store = cfg.get("store", {})
    if not isinstance(store, dict):
        errs.append("`store` must be an object (e.g. {\"dir\": \".ab\"})")
    else:
        # STORE_TYPES/STORE_KEYS live in store_factory — the construction site —
        # so validation here can never drift from what the factory accepts.
        from .store_factory import STORE_KEYS, STORE_TYPES
        stype = store.get("type", "jsonl")
        if stype not in STORE_TYPES:
            errs.append(
                "`store.type` {!r} is unknown — known: {}".format(
                    stype, ", ".join(repr(t) for t in sorted(STORE_TYPES))))
        else:
            unknown_store = [k for k in store if k not in STORE_KEYS[stype]]
            if unknown_store:
                errs.append(
                    "unknown key(s) in `store` for type {!r}: {} — "
                    "known: {}".format(
                        stype,
                        ", ".join(repr(k) for k in unknown_store),
                        ", ".join(repr(k) for k in sorted(STORE_KEYS[stype]))))
            if stype == "postgres" and not store.get("dsn"):
                errs.append(
                    '`store.dsn` is required for type "postgres" — '
                    'add {"dsn": "postgresql://user:pass@host/db"}')
    metrics = cfg.get("metrics", {})
    if not (isinstance(metrics, dict)
            and all(isinstance(k, str) and k and isinstance(v, str) and v
                    and all(v.split("."))   # no empty segment: ".score"/"a..b" are typos
                    for k, v in metrics.items())):
        errs.append("`metrics` must map metric name -> dotted payload path with "
                    "non-empty segments (e.g. {\"score\": \"review.score\"}; "
                    "\".score\" or \"a..b\" is a typo)")
    if errs:
        raise ValueError("invalid experiment config:\n  - " + "\n  - ".join(errs))


def _model_refs(pipeline: Dict[str, Any]) -> List[str]:
    """Every explicit model a variant's pipeline can call — node `model` plus the
    M7 `escalate_model` rung (the ladder's second call costs too)."""
    refs: List[str] = []
    for role, node in (pipeline.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get("type") in _MODEL_NODE_TYPES:
            if not (isinstance(node.get("model"), str) and node.get("model")):
                raise ValueError(
                    "experiment variants need an EXPLICIT `model` on every "
                    "model-calling node — node {!r} has none, so its cost could "
                    "not be attributed (a silent $0.00 in the matrix)".format(role))
            refs.append(node["model"])
            em = node.get("escalate_model")
            if isinstance(em, str) and em:
                refs.append(em)
    return refs


def _preflight_variant(name: str, path: str, exp_base: str,
                       price_map: Dict[str, Any], entries: List[Any],
                       metrics: Dict[str, str]) -> Tuple[Dict[str, Any], str, str]:
    """Load + validate one variant; returns (effective_root, variant_base,
    fingerprint). Raises ValueError naming the variant on any problem.
    `entries` are the experiment inputs' (id, key-set) pairs and `metrics` the
    declared metric paths — the experiment-level contract checks (contracts.py)
    run here because inputs are SHARED across variants by design (the run loop
    overrides each variant's `input` with them)."""
    from ..runtime_factories import _read_json, _rel
    from ..validate import validate_config
    root_path = _rel(exp_base, path)
    import os
    root = _read_json(root_path)
    base = os.path.dirname(os.path.abspath(root_path))
    if base not in sys.path:
        sys.path.insert(0, base)   # fn:/plugins beside the variant resolve, as the CLI does
    from ..plugins import load_plugins
    load_plugins(root.get("plugins"), base)
    try:
        validate_config(root, base)
    except ValueError as e:
        raise ValueError("variant {!r} ({}): {}".format(name, path, e)) from e
    if root.get("live_config"):
        raise ValueError(
            "variant {!r}: live_config is not allowed in an experiment — "
            "per-invocation mutable re-reads would make the row fingerprint a "
            "lie; pin the config for the campaign".format(name))
    pipeline_ref = root.get("pipeline")
    pipeline = (pipeline_ref if isinstance(pipeline_ref, dict)
                else _read_json(_rel(base, pipeline_ref)))
    missing = sorted(set(m for m in _model_refs(pipeline) if m not in price_map))
    if missing:
        raise ValueError(
            "variant {!r}: price_map has no entry for model(s) {} — the cost "
            "axis would silently read $0.00; add them ({{\"input\": $, "
            "\"output\": $}} per 1k tokens; an explicit 0 rate for free/fake "
            "models)".format(name, ", ".join(missing)))
    from .contracts import check_variant_contracts
    for warning in check_variant_contracts(name, pipeline, base, entries, metrics):
        print(warning, file=sys.stderr)
    return root, base, config_fingerprint(root, base)


def _outcome_row(out: Any) -> Dict[str, Any]:
    """Classify one run result into the row's outcome fields."""
    if isinstance(out, Done):
        return {"outcome": "done", "corr": out.output.correlation_id,
                "baton_id": out.baton_id, "output": out.output.payload}
    if isinstance(out, Suspended):
        return {"outcome": "suspended", "corr": None, "baton_id": out.baton_id,
                "awaiting": out.awaiting, "output": None}
    return {"outcome": type(out).__name__.lower(), "corr": None,
            "baton_id": getattr(out, "baton_id", None), "output": None}


async def run_experiment(cfg: Dict[str, Any], base: str, *,
                         store: Optional[ExperimentStore] = None) -> Dict[str, Any]:
    """Run the full campaign matrix (variants x inputs x repetitions),
    appending one durable row per run. Returns a summary
    {experiment, rows, trace, by_variant: {name: {done/suspended/failed/error}}}.
    `store` overrides the config's store block (tests, embedding apps)."""
    import os
    from ..runtime import run_root
    from ..runtime_factories import _rel

    _check_experiment(cfg)
    exp_id = cfg["id"]
    price_map = cfg["price_map"]
    # store.dir is always the campaign directory regardless of row-store type —
    # the trace file (cost sink) always lands on the local filesystem.
    store_dir = _rel(base, (cfg.get("store") or {}).get("dir", ".ab"))
    trace_path = os.path.join(store_dir, "{}.trace.jsonl".format(exp_id))

    # PRE-FLIGHT: every variant loads, validates, prices — and every fixture
    # input exists — before any run. A typo'd fixture path otherwise produced
    # N junk "error" rows per variant instead of one loud abort (eval R3).
    missing_inputs = [p for p in cfg["inputs"]
                      if isinstance(p, str) and not os.path.exists(_rel(base, p))]
    if missing_inputs:
        raise ValueError("input fixture(s) not found: {}".format(
            ", ".join(repr(p) for p in missing_inputs)))
    from .contracts import entry_key_sets
    entries = entry_key_sets(cfg["inputs"], base)
    variants: Dict[str, Tuple[Dict[str, Any], str, str]] = {}
    for name, path in cfg["variants"].items():
        variants[name] = _preflight_variant(name, path, base, price_map,
                                            entries, cfg.get("metrics") or {})

    by_variant: Dict[str, Dict[str, int]] = {}
    total = 0
    # store built AFTER pre-flight: a config that aborts above never opens a
    # DB connection; opened_store closes on exit only what it built.
    from .store_factory import opened_store
    async with opened_store(cfg, base, store) as st:
        for name, (root, vbase, fingerprint) in variants.items():
            counts = by_variant.setdefault(
                name, {"done": 0, "suspended": 0, "failed": 0, "error": 0})
            for i, inp in enumerate(cfg["inputs"]):
                if isinstance(inp, str):
                    input_id, input_val = inp, _rel(base, inp)  # fixture: experiment-relative
                else:
                    input_id, input_val = "inline-{}".format(i), inp
                for rep in range(cfg.get("repetitions", 1)):
                    effective = dict(root)
                    effective["input"] = input_val
                    effective["run"] = True   # a campaign run is always a one-shot run
                    # the experiment owns observability: force cost capture into
                    # the campaign's trace file (report joins by corr)
                    effective["trace"] = {
                        "mode": "tracer", "capture": ["phase", "cost"],
                        "sinks": [{"type": "file", "path": trace_path}],
                    }
                    t0 = time.time()
                    try:
                        out = await run_root(effective, vbase)
                        fields = _outcome_row(out)
                    except StageFailed as e:
                        failed_env = e.output
                        fields = {"outcome": "failed",
                                  "corr": (failed_env.correlation_id
                                           if failed_env is not None else None),
                                  "baton_id": None, "output": None,
                                  "failure": [f.code for f in e.verdict.failures] or [str(e)]}
                    except Exception as e:  # a campaign survives one bad run; the row says why
                        fields = {"outcome": "error", "corr": None, "baton_id": None,
                                  "output": None, "error": repr(e)}
                    row = {"experiment_id": exp_id, "variant": name,
                           "fingerprint": fingerprint, "input_id": input_id,
                           "rep": rep, "t_start": t0, "t_end": time.time(), **fields}
                    await st.append_row(exp_id, row)
                    counts[fields["outcome"]] = counts.get(fields["outcome"], 0) + 1
                    total += 1
    return {"experiment": exp_id, "rows": total, "trace": trace_path,
            "by_variant": by_variant}
