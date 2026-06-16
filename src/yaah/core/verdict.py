"""Verdict — a validator node's output (pass/fail + failures).

Used by: validator nodes (return one, carried as an Envelope of kind 'verdict')
and the harness (reads pass/fail to drive the retry loop).
Where: produced by validators; consumed in Harness._validate.
Why: a uniform shape for "is this output acceptable?", independent of which
validator produced it.

Targets Python 3.9+.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .envelope import Envelope
from .failure import Failure
from .kind import Kind


@dataclass
class Verdict:
    status: str  # 'pass' | 'fail'
    failures: List[Failure] = field(default_factory=list)
    severity: str = "hard"  # 'hard' | 'soft'

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    @classmethod
    def passed(cls) -> "Verdict":
        return cls(status="pass")

    @classmethod
    def failed(cls, *failures: Failure, severity: str = "hard") -> "Verdict":
        return cls(status="fail", failures=list(failures), severity=severity)

    def to_envelope(self, in_reply_to: Optional[Envelope] = None) -> Envelope:
        payload = {
            "status": self.status,
            "severity": self.severity,
            "failures": [
                {"code": f.code, "message": f.message, "fix_hint": f.fix_hint}
                for f in self.failures
            ],
        }
        if in_reply_to is not None:  # preserve the correlation chain
            return in_reply_to.reply(Kind.VERDICT, **payload)
        return Envelope(kind=Kind.VERDICT, payload=payload)

    @classmethod
    def from_envelope(cls, env: Envelope) -> "Verdict":
        # Tolerate a malformed / ERROR reply (no "status"): treat it as a clean hard
        # fail rather than KeyError-ing out of the harness's _validate (bug review L2).
        p = env.payload
        return cls(
            status=p.get("status", "fail"),
            severity=p.get("severity", "hard"),
            failures=[Failure(**f) for f in p.get("failures", [])],
        )
