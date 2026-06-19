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

from typing import Any, Callable, Optional

from ..core import Envelope
from ..trace import Span


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

    async def stage(self, stage_name: str, input: Envelope, t0: float,
                    *, status: str, concerns: Optional[list] = None,
                    output: Optional[Envelope] = None, route: Any = None,
                    awaiting: Optional[str] = None) -> None:
        """Emit a `stage` span for a completed stage. Status reflects the stage
        outcome: 'ok' (passed), 'suspended' (parked at gate), 'cleared'
        (cancelled in-flight). Soft concerns (validators that flagged but
        didn't block) are recorded so the trace shows a stage that continued
        with concerns. When the stage's output payload carries an `exit_code`
        (the shell-node contract), it is recorded too — the error-path
        contract (BUG-662): a subprocess's exit code must be observable in the
        trace even on the pass path (a shell node with `|| true`-style
        tolerance can pass while the command failed)."""
        attrs = {"stage": stage_name}
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
