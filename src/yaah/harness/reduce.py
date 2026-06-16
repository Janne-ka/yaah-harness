"""default_reduce — the generic, domain-free combine for a fan-in.

Used by: the harness fan-in when no `reduce` override is configured. Apps override
with a `call_target` string (`fn:`/`node:`/`http:`) when they need semantic merge
(dedup, parse-raw) — the engine never learns the data shape.
Where: the join point of a fork/fan-in.
Why: "append all together" is the sensible default since branch payloads are usually
JSON: same-key lists concatenate, dicts merge, scalars take the last writer. Pure and
deterministic (branches folded in id order), so two runs of the same arrivals combine
identically.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict


def default_reduce(arrived: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Combine the per-branch payloads (`{branch_id: payload}`) into one dict by a
    generic append: concat same-key lists, merge same-key dicts, last-wins on a scalar
    clash. Folded in sorted branch-id order for determinism."""
    out: Dict[str, Any] = {}
    for _bid, payload in sorted(arrived.items()):
        _merge_into(out, payload or {})
    return out


def _merge_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in src.items():
        if k in dst and isinstance(dst[k], list) and isinstance(v, list):
            dst[k] = dst[k] + v
        elif k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
            _merge_into(dst[k], v)
        else:
            dst[k] = v  # last writer wins on a scalar/type clash
