"""SpanEmitter — emit the harness's stage-level trace spans.

Used by: the Harness (composition, not inheritance) — `self._spans.stage(...)`
on a successful stage, `self._spans.error(...)` on a failed one.
Where: the engine tracing core, but the EMITTER (one tiny class) lives here
in `harness/` because the only callers are the run loop and the fork walker.
Why: extracted from Harness as part of the elegance #1 split — tracing is
cross-cutting, not run-loop logic. Harness keeps its core state, the emitter
owns the projection of (stage, result, time) into a Span.

One source of truth for the stage-span shape: status mapping (ok/suspended/
cleared/error), attrs population (`stage`, `concerns`, `error`), and the
clock/parent/corr wiring. A change to the trace contract is one edit here,
not three sites in harness.py.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Iterable, Optional

from ..core import Envelope
from ..trace import Span

# effects_from bound (ADR-0008 D2). The effect descriptor is the FIRST payload
# VALUE ever written to a trace record — concerns only recorded a COUNT, so there
# is no bounding precedent to copy; the bound is defined here from scratch.
_EFFECTS_MAX = 2048   # max JSON-serialized length stored verbatim
_EFFECTS_HEAD = 256   # plain-string head kept when the descriptor is dropped

# sentinel: distinguishes "no effects_from configured" (emit nothing) from an
# effects_from whose value is genuinely None (record effects: null).
_NO_EFFECTS = object()


def _effects_attrs(value: Any) -> Dict[str, Any]:
    """Shape the author-chosen effect descriptor into completion-span attrs
    (ADR-0008 D2). JSON-serialize to MEASURE; a descriptor whose serialization
    exceeds _EFFECTS_MAX is NOT stored — a mid-string JSON clip is unparseable
    garbage a consumer would choke on — the record instead carries
    `effects: null` + `effects_truncated: true` + `effects_head` (a plain-string
    diagnostic head, never presented as parseable JSON). A NON-serializable
    descriptor is treated as oversize (never raises); its head is `str(value)`.

    The stored descriptor is the JSON ROUND-TRIP, not the live payload object:
    the descriptor is COPIED (not popped) off the payload, so a downstream stage
    mutating it in place must not be able to corrupt the already-emitted record
    through a shared reference (a lazily-serializing sink would see the mutation).

    Known limit: for a bare NUMBER descriptor whose serialization exceeds the
    bound (a >2048-digit literal), the head is digits — which happens to parse
    as JSON. Effect handles are ids/strings/objects in practice; the truncated
    shape (effects: null + effects_truncated) still marks it unmistakably."""
    try:
        serialized = json.dumps(value)
    except (TypeError, ValueError):
        return {"effects": None, "effects_truncated": True,
                "effects_head": str(value)[:_EFFECTS_HEAD]}
    if len(serialized) > _EFFECTS_MAX:
        return {"effects": None, "effects_truncated": True,
                "effects_head": serialized[:_EFFECTS_HEAD]}
    return {"effects": json.loads(serialized)}


class SpanEmitter:
    def __init__(self, tracer: Any, clock: Callable[[], float]) -> None:
        self._tracer = tracer
        self._clock = clock

    async def note(self, stage_name: str, input: Envelope, *,
                   status: str, attrs: dict) -> None:
        """Emit a point-in-time `stage` span (t0==t1) for an INTERMEDIATE event a
        completed-stage span can't carry — a failed/retried attempt inside the
        retry loop. Without this the trace collapsed a stage to its FINAL attempt:
        a flaky stage that passed on try 3 looked identical to one that passed on
        try 1 (observability blind spot — per-attempt history). One line per
        retry, so the route waterfall shows the actual attempt trajectory."""
        now = self._clock()
        await self._tracer.emit(Span.timed(
            "stage", corr=input.correlation_id, parent=input.id,
            t0=now, t1=now, status=status, attrs={"stage": stage_name, **attrs}))

    async def resumed(self, stage_name: str, input: Envelope, *,
                      awaiting: Optional[str],
                      decision_keys: Iterable[str],
                      approver: Optional[str] = None,
                      decision_diff: Optional[Dict[str, Any]] = None) -> None:
        """Emit a point-in-time `stage` span recording an EXTERNAL (human) RESUME
        decision. Without it the human-override event had NO trace record at all:
        resume() routes PAST the gate without re-executing it, so the run's trace
        ended at status:suspended and the baton — the only other witness — is
        deleted on completion (the "override is logged" audit gap). Status is
        plain "ok" (no new taxonomy in aggregate; a resume is not an error and
        must not count as one).

        Two audit policies, deliberately different (they answer different
        questions of AI-Act Art. 14(4)(d)):
        - Decision CONTENT stays out. `decision_keys` (and `decision_diff` below)
          carry payload KEYS only, sorted — payload VALUES may be sensitive (a
          human's free-text ruling) and must never reach the trace.
        - Decision AUTHOR is recorded. The OPTIONAL `approver` is the human's
          identity ("who overrode") — it IS the audit signal, so it is kept even
          though it is likely PII. It rides the resume envelope's `approver`
          HEADER — metadata, distinct from the domain-data payload and from the
          standard `sender` header: `sender` names the COMPONENT that produced
          the envelope (a node role, a driver), `approver` names the HUMAN who
          authorized the override — even when one operator is both, the audit
          needs the roles kept apart. The header never pollutes the merged
          decision that flows downstream (_merge_decision drops it on both the
          artifact-merge and the no-artifact paths).
          Absent header ⇒ no attr (records stay as they were).

        `decision_diff` is the emitted-vs-edited key-level audit (the self-repair
        corpus signal): {emitted, added, changed} key lists (bounded, values-free)
        computed by the caller at the decision merge."""
        attrs: dict = {"resumed": True, "decision_keys": sorted(decision_keys)}
        if awaiting is not None:  # always set on a suspended baton; guard anyway
            attrs["awaiting"] = awaiting
        if approver is not None:
            attrs["approver"] = approver
        if decision_diff is not None:
            attrs["decision_diff"] = decision_diff
        await self.note(stage_name, input, status="ok", attrs=attrs)

    async def stage(self, stage_name: str, input: Envelope, t0: float,
                    *, status: str, concerns: Optional[list] = None,
                    output: Optional[Envelope] = None, route: Any = None,
                    awaiting: Optional[str] = None,
                    effects: Any = _NO_EFFECTS) -> None:
        """Emit a `stage` span for a completed stage. Status reflects the stage
        outcome: 'ok' (passed), 'suspended' (parked at gate), 'cleared'
        (cancelled in-flight). Soft concerns (validators that flagged but
        didn't block) are recorded so the trace shows a stage that continued
        with concerns. When the stage's output payload carries an `exit_code`
        (the shell-node contract), it is recorded too — the error-path
        contract (BUG-662): a subprocess's exit code must be observable in the
        trace even on the pass path (a shell node with `|| true`-style
        tolerance can pass while the command failed).

        `effects` (ADR-0008 D2): the author-chosen effect handle pulled from
        payload[stage.effects_from] by the harness. Passed only when the stage
        declares effects_from (the caller omits it otherwise, so a stage without
        the key records no `effects` attr). Bounded/shaped by `_effects_attrs`."""
        attrs: Dict[str, Any] = {"stage": stage_name}
        if concerns:
            attrs["concerns"] = len(concerns)
        if output is not None and isinstance(output.payload.get("exit_code"), int):
            attrs["exit_code"] = output.payload["exit_code"]
        # Decision provenance: the value that DROVE this stage's branch route —
        # the cheapest high-value observability win. Without it, "why did it park /
        # rework / block?" was unanswerable (the branch key lived only in the
        # transient payload, never a span).
        if route is not None:
            attrs["route"] = route
        # Suspend context — who/what the gate is waiting for. The progress
        # sink renders this inline so an operator tailing the log doesn't
        # have to `yaah list` to find out what just parked.
        if awaiting is not None:
            attrs["awaiting"] = awaiting
        # Rendered-artifact pointer: when the stage's output carries the GENERIC
        # `path` key (the render node's written-file contract), record its
        # basename so the progress sink can tell an operator what to open — the
        # suspend-at-a-gate UX win (Y2). Generic key, no app concept named.
        if output is not None and output.payload.get("path"):
            attrs["artifact"] = os.path.basename(output.payload["path"])
        # Effect descriptor (ADR-0008 D2) — the rollback handle, bounded/clipped.
        if effects is not _NO_EFFECTS:
            attrs.update(_effects_attrs(effects))
        await self._tracer.emit(Span.timed(
            "stage", corr=input.correlation_id, parent=input.id,
            t0=t0, t1=self._clock(), status=status, attrs=attrs))

    async def error(self, stage_name: str, input: Envelope, t0: float,
                    exc: BaseException) -> None:
        """Emit a `stage` span with status='error' for a FAILED stage. CORE,
        not fanout-specific: a failed stage's success-path emit never runs, so
        without this the run trace — and the report's "what went wrong" derived
        from non-ok spans — would be blind to failures. Called from both the
        main drive path and fork branch walking, so every failure is observable
        wherever it happens. Carries the verdict's failures (StageFailed) or the
        exception repr in `error`."""
        failures = getattr(getattr(exc, "verdict", None), "failures", None)
        if failures:  # name every failure: "code: message" (assessment #14 — the
            # old singular `verdict.failure` getattr always missed, so error spans
            # only ever carried the bare exception repr)
            detail = "; ".join("{}: {}".format(f.code, f.message) for f in failures)
        else:
            detail = repr(exc)
        await self._tracer.emit(Span.timed(
            "stage", corr=input.correlation_id, parent=input.id,
            t0=t0, t1=self._clock(), status="error",
            attrs={"stage": stage_name, "error": detail}))
