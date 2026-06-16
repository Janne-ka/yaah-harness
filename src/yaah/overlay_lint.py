"""overlay_lint — the deterministic gate on AI-authored `_extends` overlays.

Used by: the AI-run harness (the controlling session lints every overlay an AI
proposer emits BEFORE it is allowed near a run) and `yaah <overlay> --lint-overlay`.
Where: engine-level, domain-free — it reads config SHAPES (keys, types, numbers),
never app words.
Why: "AI operates ON the pipeline" is only trustable if a machine-checkable line
separates what an AI may change from what needs human promotion (why-yaah.md
§YAAH+AI). The proposer must be assumed PROMPT-INJECTED (it reads repo/model
text), so this lint is DENY-BY-DEFAULT and must run OUTSIDE the proposer's reach
— the proposer gets write access to the overlay directory only, never to this
module, the base configs, or the lint's invocation.

The line it draws (one place — the same leaf-vs-topology boundary that governs
what a parked-resume safely picks up and what a future config-push may carry):

  ALLOWED  (leaf, non-code-equivalent, on EXISTING nodes only — the shared
  `validate.MUTABLE_LEAF_KEYS` surface, also what `live_config` re-reads):
    - `model`, `prompt`, `template`, `effort` value swaps (strings —
      LLM-facing, not executed)
    - node-level `temperature`/`timeout`/`retries` and numeric `config` values
      that move toward SAFER (new <= base: attempts, rework bounds, timeouts
      can tighten, never widen)
  REJECTED (everything else, including):
    - any top-level key but `nodes` (`graph` = topology; `providers` carry
      binaries/tools; root keys are deployment trust)
    - new or removed nodes (topology), node `type` changes
    - `target`/`impl` (fn: = code), `command`, `binary`, `allowed_tools`,
      `permission_mode`, `tools`, `mcp`, `cwd_from` (execution surface)
    - `validators`, `concerns_from`, gate fields (safety surface)
    - numeric increases, non-numeric config changes (deny by default)
    - a missing `_authored_by` (provenance is mandatory)
    - stacking: an AI overlay may not extend another AI-authored overlay
      (compounding unreviewability — flatten and promote first)

Returns every problem found; an empty list means the overlay is within the AI's
mutable surface. Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .runtime_factories import _read_json
from .validate import MUTABLE_LEAF_KEYS, MUTABLE_NUMERIC_KEYS

# The allow-list IS the shared mutable-leaf surface (validate.py — one table
# for the lint, the live re-read, and a future config-push, so they can't
# drift). Node-level scalars under MUTABLE_NUMERIC_KEYS get the same
# tighten-only rule as numeric `config` values.
_ALLOWED_NODE_KEYS = MUTABLE_LEAF_KEYS


def lint_overlay(path: str) -> List[str]:
    """Lint ONE overlay file (the raw child, not the merged result) against its
    `_extends` base. See module docstring for the allow/deny line."""
    errs: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return ["overlay must be a JSON object"]
    if not raw.get("_authored_by"):
        errs.append("missing `_authored_by` — provenance is mandatory for linted overlays")
    base_ref = raw.get("_extends")
    if not isinstance(base_ref, str):
        return errs + ["overlay must `_extends` a base (it IS the change-vs-base contract)"]
    base_path = base_ref if os.path.isabs(base_ref) else os.path.normpath(
        os.path.join(os.path.dirname(path), base_ref))

    # no AI-on-AI stacking: the immediate base must not itself be AI-authored
    try:
        with open(base_path, "r", encoding="utf-8") as f:
            raw_base = json.load(f)
        if isinstance(raw_base, dict) and raw_base.get("_authored_by"):
            errs.append("base {!r} is itself an authored overlay — no stacking; "
                        "promote or flatten it first".format(base_ref))
    except OSError:
        return errs + ["cannot read _extends base {!r}".format(base_ref)]

    base = _read_json(base_path)  # fully merged base = the values bounds compare against
    base_nodes = base.get("nodes", {}) if isinstance(base, dict) else {}

    for key in raw:
        if key.startswith("_") or key == "nodes":
            continue
        errs.append("top-level key {!r}: outside the AI surface "
                    "(only `nodes` leaf values may be overlaid)".format(key))

    for role, spec in (raw.get("nodes") or {}).items():
        if role not in base_nodes:
            errs.append("node {!r}: NEW node — topology needs human promotion".format(role))
            continue
        if spec is None:
            errs.append("node {!r}: deletion — topology needs human promotion".format(role))
            continue
        if not isinstance(spec, dict):
            errs.append("node {!r}: spec must be an object".format(role))
            continue
        for k, v in spec.items():
            if k not in _ALLOWED_NODE_KEYS:
                errs.append("node {!r}: key {!r} is outside the AI surface "
                            "(execution/safety/topology — human promotion)".format(role, k))
            elif k in ("model", "prompt", "template", "effort"):
                if v is not None and not isinstance(v, str):
                    errs.append("node {!r}: {} must be a string".format(role, k))
            elif k in MUTABLE_NUMERIC_KEYS:
                base_v = (base_nodes[role] or {}).get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    errs.append("node {!r}: {} must be a number".format(role, k))
                elif isinstance(base_v, (int, float)) and v > base_v:
                    errs.append("node {!r}: {} raised {} -> {} — bounds may only "
                                "tighten (a raise widens retries/timeouts/cost)".format(
                                    role, k, base_v, v))
            elif k == "config":
                errs.extend(_lint_config(role, v, (base_nodes[role] or {}).get("config") or {}))
    return errs


def _lint_config(role: str, cfg: Any, base_cfg: Dict[str, Any]) -> List[str]:
    """Config sub-surface: numeric values may only TIGHTEN (new <= base);
    everything else is deny-by-default."""
    if not isinstance(cfg, dict):
        return ["node {!r}: config must be an object".format(role)]
    errs: List[str] = []
    for ck, cv in cfg.items():
        base_v = base_cfg.get(ck)
        if isinstance(cv, bool) or isinstance(base_v, bool):
            errs.append("node {!r}: config.{} — boolean flags are outside the AI "
                        "surface (often safety switches)".format(role, ck))
        elif isinstance(cv, (int, float)) and isinstance(base_v, (int, float)):
            if cv > base_v:
                errs.append("node {!r}: config.{} raised {} -> {} — bounds may only "
                            "tighten (a raise widens retries/timeouts/cost)".format(
                                role, ck, base_v, cv))
        else:
            errs.append("node {!r}: config.{} — non-numeric config change is outside "
                        "the AI surface".format(role, ck))
    return errs
