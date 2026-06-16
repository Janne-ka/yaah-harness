"""Tiny fn: targets for test_sticky_concerns_into.py (transforms only, so the
test needs no model backend). Importable because the test inserts tests/ into
sys.path. Targets Python 3.9+."""
from __future__ import annotations

from typing import Any, Dict


def emit_concerns(input: Any, config: Any) -> Dict[str, Any]:
    """Set a payload key shaped like a parsed sceptic report (for concerns_from)."""
    p = dict(input.payload)
    p["found"] = [{"code": "sceptic", "message": "spec smells", "fix_hint": "look"}]
    return p


def drop_all(input: Any, config: Any) -> Dict[str, Any]:
    """A payload-REPLACING stage that forgets every key — the H5 defect shape."""
    return {"fresh": True}


def snapshot(input: Any, config: Any) -> Dict[str, Any]:
    """Pass through, recording what this stage actually saw in its input."""
    p = dict(input.payload)
    p["seen_task"] = p.get("task")
    return p


def override_task(input: Any, config: Any) -> Dict[str, Any]:
    """Deliberately SET a sticky key — the stage's own value must win."""
    p = dict(input.payload)
    p["task"] = "OVERRIDE"
    return p
