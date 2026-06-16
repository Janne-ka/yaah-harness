"""Suspended — the outcome of a run parked awaiting an external decision.

Used by: callers of Harness.run / Harness.resume (check `isinstance(out,
Suspended)`, then later call resume(baton_id, decision)).
Where: returned when a stage escalates to a human or a node returns `await`.
Why: a typed "paused" result carrying the baton id and what it waits for.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Suspended:
    baton_id: str
    awaiting: str  # e.g. 'human:spec_review'
    # Soft validator concerns gathered before the gate — what a sceptic flagged,
    # for the human (or gate driver) to weigh. Empty if none.
    concerns: List[dict] = field(default_factory=list)
    # The gate's rendered QUESTION (its `ask`, e.g. the grill's question or the spec
    # under review) — so the decider/stdin/mailbox shows the human what to answer.
    ask: str = ""
