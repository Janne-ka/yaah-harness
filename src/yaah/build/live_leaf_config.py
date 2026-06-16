"""LiveLeafConfig — mtime-cached re-reader of a pipeline file's mutable leaves.

Used by: `build()` / `serve_from_config()` when the runtime passes a
`live_config_path` (root config `live_config: true`); each registered node is
wrapped in a `LiveConfigNode` that calls `refresh()` per invocation.
Where: the dispatch seam — between the comms-held (frozen-at-build) NodeConfig
and the node's invoke.
Why: TODO live-vars mechanism (a), a direct requirement of the AI-run goal —
change VARIABLES, never topology, on a RUNNING system. An operator (or a
promoted AI overlay) edits the committed pipeline file; the edit takes effect
on the next node invocation, no restart. The adopted surface is the
`LIVE_NODECONFIG_KEYS` scalars (model / effort / temperature / timeout /
retries) plus NUMERIC `config` values (the bounds class) — the same
leaf/non-code-equivalent table the overlay lint enforces (`validate.py`,
defined once so the surfaces can't drift). Code-equivalent keys (`command`,
`target`, `allowed_tools`, ...) are constructor-frozen by design: nodes were
BUILT from them, and re-reading them would turn a file edit into an RCE
channel.

Failure posture: a missing/unreadable/garbled file keeps the LAST KNOWN
leaves — a config re-read must never kill a running pipeline.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from ..core import NodeConfig
from ..runtime_factories import _read_json  # `_extends`-aware loader


class LiveLeafConfig:
    def __init__(self, path: str) -> None:
        self._path = path
        self._mtime: Optional[float] = None
        self._nodes: Dict[str, Dict[str, Any]] = {}

    def refresh(self, role: str, built: Optional[NodeConfig]) -> NodeConfig:
        """The built config with the file's CURRENT mutable leaves folded in.
        Scalars mirror `_node_config` (a key removed from the file reverts to
        its default); non-numeric extras and the idempotency key stay as
        built."""
        built = built or NodeConfig()
        spec = self._spec(role)
        if spec is None:
            return built
        extras = dict(built.extras)
        for k, v in (spec.get("config") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                extras[k] = v
        return NodeConfig(
            model=spec.get("model"),
            effort=spec.get("effort"),
            temperature=spec.get("temperature"),
            timeout=spec.get("timeout"),
            retries=int(spec.get("retries", 0)),
            idempotency_key=built.idempotency_key,
            extras=extras,
        )

    def _spec(self, role: str) -> Optional[Dict[str, Any]]:
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            return self._nodes.get(role)  # keep last known
        if mtime != self._mtime:
            try:
                cfg = _read_json(self._path)
                nodes = cfg.get("nodes") if isinstance(cfg, dict) else None
                self._nodes = {r: s for r, s in (nodes or {}).items()
                               if isinstance(s, dict)}
                self._mtime = mtime
            except (ValueError, OSError):
                pass  # mid-edit garble: keep last known, retry next call
        return self._nodes.get(role)
