"""PhaseContributor — the default-on minimum capture (progress UX).

Used by: a Tracer's contributor set; enabled by default (`capture: [phase]`).
Where: the engine's bundled contributors (pure projection, no external system —
like the Static/Routing prompt sources that ship in the engine).
Why: the cheapest useful trace — which stage ran, did it pass, how long — so a
zero-config run gets live progress out of the box. No tokens, no tool detail.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict

from ..contributor import TraceContributor
from ..span import Span


class PhaseContributor(TraceContributor):
    name = "phase"

    def contribute(self, span: Span) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": span.status, "duration_ms": span.duration_ms}
        # Progress-UX attrs the progress sink renders: the stage name, plus the
        # suspend context (who/what a park is waiting for, and where its rendered
        # artifact is), plus the model-ladder escalation labels (M7 — which model
        # a rung escalated FROM and why; without them a laddered run's second
        # model_call is only inferable, never labeled), plus the human-resume
        # audit record: `resumed`/`decision_keys` (the "override is logged" line),
        # `approver` (WHO overrode — identity only), and `decision_diff` (the
        # emitted-vs-edited key-level audit / self-repair corpus signal). All of
        # these carry payload KEYS only (or an identity), never decision VALUES.
        # These must reach the projected record, not just sit in span.attrs — else
        # the lines are dead in real runs (the sink only sees the record, never the
        # raw span). The `effects`/`effects_truncated`/`effects_head` trio is the
        # ADR-0008 rollback handle: the author-chosen effect descriptor (bounded)
        # the `yaah rollback` verb reads back from the persisted record. The
        # `saga` record's five bucket attrs (`rolled_back`/`skipped_costly`/
        # `impossible`/`failed`/`not_attempted`) plus a `skipped` marker are the
        # ADR-0009 auto-saga LEDGER: without them here the projected saga record
        # persists with EMPTY buckets and idempotency is a silent no-op (a retried
        # resume would re-undo everything — design-eval #1). Buckets are identities
        # only (stage/occurrence/node), consistent with the keys-only trace contract.
        for k in ("stage", "awaiting", "artifact", "ladder_from", "ladder_trigger",
                  "resumed", "decision_keys", "approver", "decision_diff",
                  "effects", "effects_truncated", "effects_head",
                  "rolled_back", "skipped_costly", "impossible", "failed",
                  "not_attempted", "skipped"):
            if k in span.attrs:
                out[k] = span.attrs[k]
        return out
