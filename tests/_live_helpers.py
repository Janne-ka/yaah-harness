"""Tiny fn: target for test_live_config.py (a transform that echoes the
NodeConfig it actually received, so the test can see live refreshes).
Importable because the test inserts tests/ into sys.path. Targets Python 3.9+."""
from __future__ import annotations

from typing import Any, Dict


def echo_config(input: Any, config: Any) -> Dict[str, Any]:
    """Snapshot the per-call NodeConfig into the payload."""
    return {"seen_model": config.model,
            "seen_timeout": config.timeout,
            "seen_n": config.extras.get("n"),
            "seen_name": config.extras.get("name")}
