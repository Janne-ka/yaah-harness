"""StageFailed — raised when a stage exhausts retries with no human gate.
DecisionRejected — raised when a resume decision violates the gate's form.

Used by: Harness (raised) and callers (catch to handle a hard failure).
Where: the validator retry loop, when attempts run out and escalate != 'human';
DecisionRejected at the resume seam, before the run is driven.
Why: surface an unrecoverable stage failure with the offending verdict.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import Envelope, Failure, Verdict


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

    def to_failure_json(self) -> Dict[str, Any]:
        """The machine-readable failure shape both `run/resume --json` (CLI) and
        the MCP error content emit, so a debugger parses stage + failures[]
        (code/message/fix_hint/data) instead of regex-scraping the prose message.
        Failure.to_dict is the single source of truth for each failure entry."""
        return {
            "outcome": "failed",
            "stage": self.stage,
            "failures": [f.to_dict() for f in self.verdict.failures],
        }


class DecisionRejected(Exception):
    """Raised at RESUME when the submitted decision violates the parked gate's
    declared `form` (N1, TIER-0). Before this, `Harness.resume` merged any
    decision blind — a nonconforming value matched no branch route, fell to the
    default, and the run silently misrouted. Now the gate's form is BINDING.

    DELIBERATELY NOT a `StageFailed`. A rejected decision is an INPUT error, not
    a stage failure: subclassing StageFailed would route it through
    `saga.settle_terminal` and ARM the ADR-0009 auto-rollback saga (unwinding
    prior stages) for a mere typo at the gate — a wrong, potentially destructive
    side effect. As a plain Exception it propagates PAST `settle_terminal`'s
    `except StageFailed` untouched, and the CLI/MCP surface it via the duck-typed
    `to_failure_json` (below) — the SAME structured `{outcome:"failed", ...}`
    shape, WITHOUT the saga coupling.

    The gate the decision was for stays PARKED and re-submittable: the harness
    raises this BEFORE `_settle` runs, so nothing is evicted (see Harness.resume).

    Enforcement is the JSON-Schema SUBSET contract yaah's own checker understands
    — required / enum / type. `additionalProperties` is NOT enforced (the subset
    checker doesn't), so a decision with EXTRA keys is tolerated; the guarantee is
    "the routing-bearing keys conform," not "the exact shape matches."

    Carries structured fields (the form name, the schema errors, the baton id) in
    the wrapped Failure's `data` slot — same pattern as render's unfilled keys —
    so a driver reads them instead of scraping the message. The message names the
    form, the errors, AND BOTH remedies (fetch `yaah baton-schema` and submit a
    conforming decision / set `strict_resume: false` if the form is mis-declared).
    """

    def __init__(self, baton_id: str, form: Optional[str], errors: List[str],
                 *, form_invalid: bool = False) -> None:
        self.baton_id = baton_id
        self.form = form
        self.errors = list(errors)
        # Two shapes of message, both ending in the two remedies. `form_invalid`
        # is the author-bug case (the gate declared a form the catalog can't
        # resolve — only reachable via hand-edited durable state, since the
        # builder rejects unknown forms at load time); the default is the
        # operator-bug case (a well-formed gate, a nonconforming decision).
        if form_invalid:
            head = ("resume decision for baton {!r} rejected: the gate's declared "
                    "form {!r} is invalid — {}".format(baton_id, form, "; ".join(errors)))
            fix_hint = ("fix the human_gate's `form` in the pipeline (author bug), "
                        "or set `strict_resume: false` in the root config to bypass")
        else:
            head = ("resume decision for baton {!r} does not conform to the gate's "
                    "form {!r} — {}".format(baton_id, form, "; ".join(errors)))
            fix_hint = ("fetch `yaah baton-schema` and submit a conforming decision")
        message = (head + " — fetch `yaah baton-schema` for the required shape and "
                   "submit a conforming decision, or set `strict_resume: false` in "
                   "the root config if this gate's form is mis-declared")
        super().__init__(message)
        # A real Verdict/Failure so `to_failure_json` reuses Failure.to_dict (the
        # single source of truth) — code/message/fix_hint + the structured `data`.
        self.verdict = Verdict.failed(Failure(
            "decision_rejected", message, fix_hint,
            data={"form": form, "errors": self.errors, "baton_id": baton_id}))

    def to_failure_json(self) -> Dict[str, Any]:
        """The machine-readable shape the CLI (`resume --json`) and the MCP error
        content emit — duck-typed by both surfaces (they call any exception's
        `to_failure_json` if present). `stage` is None: a rejected decision never
        entered a stage. The rejection detail (form/errors/baton_id) rides the
        single failure's `data`, same slot as render's unfilled keys."""
        return {
            "outcome": "failed",
            "stage": None,
            "code": "decision_rejected",
            "failures": [f.to_dict() for f in self.verdict.failures],
        }
