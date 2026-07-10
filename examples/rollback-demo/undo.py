"""Transform functions and rollback targets for the rollback-demo example.

The write_* functions are fn: targets for transform nodes (call: "envelope").
Each creates a file and records the absolute path as the effect descriptor
under payload["effect"]. The stage's effects_from: "effect" key captures that
descriptor onto the trace span at completion, making it available to the undo.

The delete_* functions are fn: rollback targets. Each receives the rollback
context dict {correlation_id, stage, node, effects, cost} — NOT the stage's
full payload (ADR-0008 D7 / compensate-ctx divergence: compensate receives the
payload, rollback receives only the bounded effects descriptor). Do not try to
read payload keys from ctx; only effects, correlation_id, stage, node, cost.

Source: examples/rollback-demo/undo.py
"""
from __future__ import annotations

import os
import pathlib
from typing import Any, Dict


# ---------------------------------------------------------------------------
# Write functions — fn: transform targets (call: "envelope")
# ---------------------------------------------------------------------------

def write_alpha(envelope: Any, config: Any) -> Dict[str, Any]:
    """Write alpha.txt; record absolute path as the effect descriptor."""
    path = pathlib.Path("alpha.txt").resolve()
    path.write_text("alpha effect — written by rollback-demo stage write_alpha\n")
    return {**envelope.payload, "effect": {"file": str(path)}}


def write_beta(envelope: Any, config: Any) -> Dict[str, Any]:
    """Write beta.txt; record absolute path as the effect descriptor."""
    path = pathlib.Path("beta.txt").resolve()
    path.write_text("beta effect — written by rollback-demo stage write_beta\n")
    return {**envelope.payload, "effect": {"file": str(path)}}


def write_gamma(envelope: Any, config: Any) -> Dict[str, Any]:
    """Write gamma.txt; record absolute path as the effect descriptor.

    write_gamma's node declares no rollback — this stage appears in the
    impossible bucket of the rollback menu.
    """
    path = pathlib.Path("gamma.txt").resolve()
    path.write_text("gamma effect — written by rollback-demo stage write_gamma\n")
    return {**envelope.payload, "effect": {"file": str(path)}}


# ---------------------------------------------------------------------------
# Delete functions — fn: rollback targets
# ctx = {correlation_id, stage, node, effects, cost}
# ---------------------------------------------------------------------------

def delete_alpha(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Rollback target for write_alpha (cost: cheap): delete the recorded file."""
    effects = ctx.get("effects") or {}
    fname = effects.get("file") if isinstance(effects, dict) else None
    existed = fname is not None and os.path.exists(fname)
    if fname and os.path.exists(fname):
        os.remove(fname)
    return {"deleted": fname, "existed": existed}


def delete_beta(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Rollback target for write_beta (cost: costly): delete the recorded file."""
    effects = ctx.get("effects") or {}
    fname = effects.get("file") if isinstance(effects, dict) else None
    existed = fname is not None and os.path.exists(fname)
    if fname and os.path.exists(fname):
        os.remove(fname)
    return {"deleted": fname, "existed": existed}
