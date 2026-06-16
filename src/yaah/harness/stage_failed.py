"""StageFailed — raised when a stage exhausts retries with no human gate.

Used by: Harness (raised) and callers (catch to handle a hard failure).
Where: the validator retry loop, when attempts run out and escalate != 'human'.
Why: surface an unrecoverable stage failure with the offending verdict.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Optional

from ..core import Envelope, Verdict


class StageFailed(Exception):
    def __init__(self, stage: str, verdict: Verdict, output: Optional[Envelope] = None) -> None:
        # The message NAMES the failures (code: message [fix_hint]) — "failed
        # validation" alone told the operator nothing (assessment DX: failure
        # detail must travel to wherever the exception surfaces).
        detail = "; ".join(
            "{}: {}{}".format(f.code, f.message,
                              " [{}]".format(f.fix_hint) if f.fix_hint else "")
            for f in verdict.failures) or "no failure detail"
        super().__init__(
            "stage {!r} failed validation with no human gate — {}".format(stage, detail))
        self.stage = stage
        self.verdict = verdict
        self.output = output  # the failed artifact, for per-node error-handling (on_error)
