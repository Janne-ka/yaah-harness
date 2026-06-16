"""Kind — reserved structural envelope kinds the harness understands.

Used by: the harness (to detect verdict / await / handoff) and any node that
emits a structurally-typed envelope.
Where: imported wherever envelopes are created (agents, validators, nodes).
Why: a small named set of structural message types, distinct from open
app-domain kinds and from the comms *mode* (event/call/handover).

Targets Python 3.9+.
"""
from __future__ import annotations


class Kind:
    TASK = "task"        # work for a worker
    RESULT = "result"    # a worker's output
    VERDICT = "verdict"  # a validator's output
    AWAIT = "await"      # a node asking to suspend (human / external gate)
    HANDOFF = "handoff"  # a handover intent (ownership transfer)
    RESUME = "resume"    # payload delivered on resume
    EVENT = "event"      # a fire-and-forget notification (push)
    ERROR = "error"      # a failure
