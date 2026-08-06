"""wiring_fingerprint — the TOPOLOGY identity of a graph, stamped on every baton.

Used by: Harness (computes it once at construction, stamps `Baton.wiring` at mint
AND at every checkpoint, compares it on `resume_running` / `resume`) and the `yaah
list` surface (labels a checkpoint whose graph has since been rewired). The
re-stamp is why the field means "the topology this cursor was produced by" rather
than "the topology the run was born on" — see `Harness._checkpoint`.
Where: yaah.harness — engine state identity, the recovery-time twin of the
experiment layer's `config_fingerprint`.
Why: a recovery re-drives `baton.stage` against the CURRENT graph. Edit the
topology between the kill and the recovery and that cursor means something else —
a renamed stage KeyErrors, a rerouted branch silently resumes onto a different
path. The stamp turns "undetected wrong resume" into a refusal that names what
moved.

What it hashes, and what it deliberately does NOT:

  IN — the wiring: `start`, `sticky`, and per stage its name, node role/id, `then`,
  `branch` (on/routes/default), `fork`, `fanin.expect`, `fanout`, `foreach.items`,
  and `final`. These are the things a cursor is interpreted against.

  OUT — everything behavioural: prompts, models, timeouts, retry budgets,
  validators, node config. Drift there is `config_fingerprint`'s job (experiment
  identity), NOT recovery's. "Fix the prompt, resume the run" is the single most
  common recovery there is; a fingerprint that refused it would train operators to
  pass `--allow-rewiring` reflexively, which costs the guard its whole meaning.

Targets Python 3.9+.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

from .graph import Graph


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _stage_wiring(stage: Any) -> Dict[str, Any]:
    branch = stage.branch or {}
    fanin = stage.fanin or {}
    foreach = stage.foreach or {}
    return {
        "name": stage.name,
        "node": stage.node,
        "id": stage.id,
        "then": stage.then,
        "branch": {"on": branch.get("on"),
                   "routes": branch.get("routes") or {},
                   "default": branch.get("default")} if branch else None,
        "fork": list(stage.fork) if stage.fork else None,
        "fanin_expect": fanin.get("expect"),
        "fanout": list(stage.fanout) if stage.fanout else None,
        "foreach_items": foreach.get("items"),
        "final": bool(stage.final),
    }


def wiring_fingerprint(graph: Graph) -> str:
    """SHA-256 hex over the graph's topology (see the module doc for the exact
    surface). Canonical by construction: stages are sorted by name and every
    nested mapping is dumped with `sort_keys`, so two loads of the same config
    hash identically regardless of dict ordering."""
    stages: List[Dict[str, Any]] = [_stage_wiring(s) for s in graph.stages.values()]
    stages.sort(key=lambda s: s["name"])
    h = hashlib.sha256()
    h.update(b"start\0")
    h.update(_canon(graph.start))
    h.update(b"sticky\0")
    h.update(_canon(sorted(graph.sticky)))
    h.update(b"stages\0")
    h.update(_canon(stages))
    return h.hexdigest()
