"""Cleared — the outcome of a run whose in-flight stage was cancelled by a clear.

Used by: callers of Harness.run / Harness.resume (check `isinstance(out,
Cleared)`); distinct from Done (it did NOT finish) and Suspended (it is not
resumable — the current envelope was dropped).
Where: returned when a `clearable` stage's node is cancelled mid-flight by a
clear addressed to its node-id (the agent-clear: "stop and remove whatever you
are doing").
Why: a typed "dropped" result. It carries the baton id and the clear's payload
(who/what cleared it, for the listener), so a caller can tell a cleared run from
a completed one and react.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Cleared:
    baton_id: str
    node: str                      # the stage/node that was cleared mid-flight
    clear_id: Optional[str] = None  # the address the clear matched (instance/node/'*')
    payload: dict = field(default_factory=dict)  # the clear envelope's payload

    def to_json_dict(self) -> Dict[str, Any]:
        """The machine-readable outcome shape for `run`/`resume --json` (CLI) and
        the MCP run/resume handlers — the single source of truth both surfaces
        emit, so their outcome JSON can't drift."""
        return {"outcome": "cleared", "baton_id": self.baton_id,
                "node": self.node, "payload": self.payload}
