"""Done — the outcome of a run that finished.

Used by: callers of Harness.run / Harness.resume (check `isinstance(out, Done)`).
Where: returned when a run reaches a stage with no next.
Why: a typed success result carrying the final output envelope and baton id.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..core import Envelope


@dataclass
class Done:
    output: Envelope
    baton_id: str
