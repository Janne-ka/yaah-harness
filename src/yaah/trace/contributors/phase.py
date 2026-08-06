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

from ..bounded_text import TRUNCATED_MARKER, bounded
from ..contributor import TraceContributor
from ..span import Span


class PhaseContributor(TraceContributor):
    name = "phase"

    #: Bound on the projected `error` detail. `error` is the ONE free-text attr
    #: in this projection (every other key is an identity, a key list, or a
    #: number), and trace records are line-oriented JSONL — one unbounded
    #: validator message (a schema dump, a diffed payload, a stack) would blow a
    #: single line to megabytes and make the file hostile to `tail`/`jq`/grep.
    #: Bounded here, at the projection, so no sink has to defend itself.
    #: The marker is the engine-wide one (see trace.bounded_text).
    ERROR_MAX = 500
    ERROR_TRUNCATED_MARKER = TRUNCATED_MARKER

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
        # these carry payload KEYS only (or an identity), never decision VALUES
        # — with ONE documented exception, the free-text `error` (see below).
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
        # The RETRY-CAUSE trio on a stage-error span (`retry`/`attempt`/
        # `error_retry_n`, plus the bounded `error` below): the harness's attempt
        # loop already notes WHY each rejected attempt was rejected, but without
        # them in this projection a run that burned N paid model calls on
        # rejected replies traces as N anonymous error spans — the trace can say
        # THAT a stage retried, never why, which is the first question of any
        # postmortem. `retry` is the kind ("transient" | "retry" | "feedback");
        # `attempt` counts against `max_attempts`; `error_retry_n` counts against
        # the SEPARATE transient-fault budget (`error_retries`), which is why
        # both exist. (Traces written before the 2026-08 rename carry that
        # counter as a bare `n`; that is a READ-side concern of aggregate/pretty,
        # which parse persisted records — span.attrs is in-process only, so
        # nothing can arrive here under the old spelling.)
        for k in ("stage", "awaiting", "artifact", "ladder_from", "ladder_trigger",
                  "resumed", "decision_keys", "approver", "decision_diff",
                  "effects", "effects_truncated", "effects_head",
                  "rolled_back", "skipped_costly", "impossible", "failed",
                  "not_attempted", "skipped",
                  "retry", "attempt", "error_retry_n"):
            if k in span.attrs:
                out[k] = span.attrs[k]
        # THE keys-only exception. `error` is free text, and a validator message
        # routinely QUOTES the payload it rejected — so a trace file inherits the
        # sensitivity class of the payloads its run carried, and on the envelope
        # carriage those values ride the wire too. Kept deliberately (the detail
        # IS the postmortem value of the retry-cause trio) and stated in the
        # tracing docs, so nobody treats trace.jsonl as safe-by-construction.
        # Bounded (see ERROR_MAX); everything above is an identity, a key list,
        # or a number and needs no bound.
        if "error" in span.attrs:
            out["error"] = bounded(span.attrs["error"], self.ERROR_MAX)
        return out
