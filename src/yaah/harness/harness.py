"""Harness — the line. Drives a run through a Graph using only Comms/Node.

Used by: the runtime (yaah.runtime) and apps via build()/harness_from_config;
callers invoke run() and resume().
Where: the orchestration core, on top of the kernel.
Why: it owns the run loop — per-stage validator retry-with-feedback, fan-out,
conditional routing, baton handover, and suspend/resume around human gates —
while staying ignorant of what any node does.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Union

from ..comms import Comms
from ..core import Envelope, Failure, Kind, Verdict
from ..external_call import call_target
from ..store import EnvelopeStore, MemoryBackend
from ..trace import NullTracer
from .baton import Baton
from .baton_store import BatonStore
from .clear_bus import ClearBus
from .cleared import Cleared
from .done import Done
from .fork_coordinator import ForkCoordinator
from .graph import Graph
from .span_emitter import SpanEmitter
from .stage import Stage
from .stage_failed import StageFailed
from .suspended import Suspended

Outcome = Union[Done, Suspended, Cleared]

_UNSET = object()  # "ttl argument not provided" — distinct from ttl=None (never expire)

# Livelock backstop for the linear walk: a backward `branch` route or a runaway
# feedback edge could spin `_drive`'s `while baton.stage is not None` forever. Far
# above any real linear pipeline; overridable per-harness for tests.
_MAX_STAGE_STEPS = 10000


def _route_key(value: object) -> str:
    """Normalize a branch value to its route-key string. Route keys come from
    JSON config (always strings), so booleans must match "true"/"false" — not
    Python's "True"/"False" (early_review #8). Numbers/strings stringify as-is."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _safe_set(fut: "asyncio.Future", value: object) -> None:
    """Idempotent future.set_result — drops the call if the future is already
    done (a race we can lose between the gate-completion path and the clear-
    handler path). Scheduled via `loop.call_soon_threadsafe` from clear
    handlers so a NATS-thread dispatch can't race the harness loop."""
    if not fut.done():
        fut.set_result(value)


# Substrings that mark a TRANSIENT, infrastructural fault — safe to retry because
# it is pre-effect (the work has not happened): provider overload/rate-limit, a
# gateway/timeout, a network blip, git/index-lock contention, NATS no-responders.
# Conservative and domain-free; an unmatched error is treated as PERMANENT (fail
# fast). Drives the separate error-retry budget in Harness._run_attempts.
_TRANSIENT_SIGNALS = (
    "429", "overloaded", "rate limit", "ratelimit", "503", "502", "504",
    "timeout", "timed out", "temporarily unavailable", "service unavailable",
    "connection reset", "connection refused", "connection aborted",
    "no responders", "index.lock", "cannot lock ref", "unable to create",
)


def _is_transient(text: object) -> bool:
    t = str(text or "").lower()
    return any(sig in t for sig in _TRANSIENT_SIGNALS)


# internal per-stage results (private to the run loop)
@dataclass
class _Pass:
    output: Envelope
    concerns: List[dict] = field(default_factory=list)  # soft validator concerns


@dataclass
class _Suspend:
    awaiting: str
    last_output: Optional[Envelope] = None  # the failed/parked artifact, for resume to keep
    concerns: List[dict] = field(default_factory=list)  # this stage's own soft concerns (escalate path)


@dataclass
class _Cleared:
    """A `clearable` stage's in-flight work was cancelled by a matching clear.
    Bubbles up from the run-stage wrapper like _Suspend; the run loop turns it
    into the terminal Cleared outcome."""
    clear_id: Optional[str]  # the address the clear carried (instance/node/'*')
    payload: dict = field(default_factory=dict)  # the clear envelope's payload


class Harness:
    def __init__(self, comms: Comms, graph: Graph, *,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 baton_store: Optional[BatonStore] = None,
                 envelope_store: Optional[EnvelopeStore] = None,
                 tracer: Optional[object] = None) -> None:
        self.comms = comms
        self.graph = graph
        # Run state lives behind a BatonStore (default in-memory = today's behavior;
        # a durable StoreBackend extender makes parked gates survive restart and resumable
        # cross-process). The harness only calls save/load/delete/sweep/list.
        self.batons = baton_store or BatonStore(MemoryBackend())
        # Gate parking (fan-in arrivals) goes through EnvelopeStore — memory default
        # (behaviour-neutral), a durable StoreBackend extender makes parked envelopes survive
        # + inspectable + flushable. The "gates park envelopes via one utility" seam.
        self._envelopes = envelope_store or EnvelopeStore(MemoryBackend())
        # TWO time sources, deliberately distinct (bug review H1):
        #  - `clock` (monotonic) for in-process SPAN DURATIONS — accurate, immune to
        #    wall-clock jumps, but its zero point is process-local.
        #  - `wall_clock` (time.time) for the baton TTL / `parked_at` — must be
        #    comparable ACROSS a restart or another process, which is the whole point
        #    of a durable BatonStore. A monotonic value persisted to the store is
        #    meaningless in a second process (gates would leak or be wrongly swept).
        # Both are injectable for testing.
        self._clock = clock
        self._wall = wall_clock
        # Injected tracer: emits a `stage` span per stage so progress/timing is
        # observable. NullTracer (the default) = tracing off, a zero-cost no-op so
        # emit sites call it unconditionally. The carriage/captures are config.
        self._tracer = tracer or NullTracer()
        # Elegance #1 (assessment): the stage-span projection lives on a small
        # SpanEmitter — tracing is cross-cutting, not run-loop logic. Harness
        # delegates instead of holding three call sites that all build the same span.
        self._spans = SpanEmitter(self._tracer, self._clock)
        # Clear signals (address scheme + matching + publication) live on the
        # ClearBus — shared by the linear path (agent-clear, stage `clears`,
        # the reset broadcast) AND the fork's wait-for-clear, so neither side
        # borrows the other's privates (review 2026-06-11 cluster 1).
        self._clear_bus = ClearBus(comms)
        # Elegance #1b (assessment): fork/fan-in machinery lives on a small
        # ForkCoordinator — the spread/rejoin shape is a different concern from
        # the run loop's linear stage walk. Harness delegates a fork stage's
        # rejoined-output computation; the coordinator calls back into the
        # harness ONLY for stage execution + routing (the shared run-stage seam);
        # its other collaborators are passed in explicitly.
        self._fork = ForkCoordinator(self, comms=comms, clear_bus=self._clear_bus,
                                     envelopes=self._envelopes,
                                     tracer=self._tracer, clock=clock)
        # Transient-fault retry policy (SEPARATE from max_attempts, see
        # _run_attempts): an infrastructural blip retries with exponential
        # backoff on its own budget (Stage.error_retries). `_sleep` is injectable
        # so tests don't actually wait; base/cap bound the backoff curve.
        self._sleep = asyncio.sleep
        self._backoff_base = 0.5
        self._backoff_cap = 8.0
        self._max_steps = _MAX_STAGE_STEPS  # livelock backstop for _drive

    def _backoff(self, n: int) -> float:
        return min(self._backoff_base * (2 ** (n - 1)), self._backoff_cap)

    @staticmethod
    def _is_transient_verdict(verdict: Verdict) -> bool:
        """A failed verdict whose failure looks like a transient infrastructural
        fault (a node/transport ERROR carrying an overload/timeout/lock message).
        Gates the separate error-retry budget in _run_attempts."""
        return any(_is_transient((f.code or "") + " " + (f.message or ""))
                   for f in verdict.failures)

    @staticmethod
    def _verdict_detail(verdict: Verdict) -> str:
        return "; ".join("{}: {}".format(f.code, f.message)
                         for f in verdict.failures) or "failed"

    async def sweep_expired(self) -> list:
        """Evict suspended batons past their own ttl (abandoned human gates).
        Returns the ids evicted. Called automatically on run()/resume(); a
        long-idle orchestrator can also call it on a timer to reclaim memory
        without new activity. Each baton decides expiry (`Baton.is_expired`)."""
        return await self.batons.sweep_expired(self._wall())

    async def flush(self, group: str = "") -> int:
        """Drop the durable PARKED SET — all parked envelopes under `group` (default
        everything) — and return the count. This is the store side of a `*` flush:
        the `*` clear SIGNAL releases in-memory waiters (a waiting fork / a clearable
        stage), while this drops what gates parked in the EnvelopeStore. 'reset
        everything' = broadcast clear `*` (release waiters) + `flush()` (drop the
        parked set)."""
        return await self._envelopes.flush(group)

    async def clear(self) -> dict:
        """CLEAR THE HARNESS — the graceful reset, instead of killing the process.
        Composes the clear/flush primitives: (1) broadcast a `*` clear so every
        in-flight CLEARABLE node cancels its work and every waiting fork/gate
        releases (clear the nodes); (2) FLUSH the durable parked envelope set;
        (3) drop suspended batons (abandon parked runs). The process stays alive and
        ready for the next run. Returns counts of what was cleared.

        (Only `clearable` stages cancel in-flight on the broadcast — by design; a
        committed side-effect node isn't cancellable, see the clearable boundary.)"""
        await self._clear_bus.broadcast()
        parked = await self._envelopes.flush()
        suspended = await self.batons.list_suspended()
        for b in suspended:
            await self.batons.delete(b.id)
        return {"parked_flushed": parked, "batons_dropped": len(suspended)}

    async def run(self, task: Envelope, *, ttl: object = _UNSET) -> Outcome:
        """Start a run. `ttl` overrides this baton's suspend lifetime (seconds;
        None = never expire); omit to use the baton default."""
        await self.sweep_expired()
        baton = Baton(id=uuid.uuid4().hex, stage=self.graph.start)
        if ttl is not _UNSET:
            baton.ttl = ttl  # the lifetime is the baton's, set per run
        # Level 1: not persisted while running — _settle saves it only if it parks.
        return await self._settle(baton, task)

    async def resume(self, baton_id: str, response: Envelope) -> Outcome:
        """Deliver an external (human) decision and continue. If the stage
        escalated after failing, the human's decision is MERGED onto the failed
        stage's last artifact (so downstream gets the real output plus the
        decision, not just the decision — early_review #18); a plain gate with no
        prior artifact just uses the response. Branch routing sees the merge."""
        await self.sweep_expired()  # an abandoned (TTL-expired) baton is gone by now
        baton = await self.batons.load(baton_id)
        if baton is None:
            raise KeyError(
                "no resumable baton {!r} — run `yaah list`; each baton is "
                "single-shot, and TTLs expire abandoned ones".format(baton_id))
        if baton.status != "suspended":
            raise ValueError(
                "baton {!r} status is {!r}, not 'suspended' — only suspended "
                "batons can be resumed; run `yaah list` to see what's "
                "actually parked".format(baton_id, baton.status))
        if baton.stage is None:
            raise ValueError(
                "baton {!r} has no suspended stage (engine invariant violation: "
                "a suspended baton should always carry its stage); this is a "
                "bug — report with the corresponding trace".format(baton_id))
        baton.status = "running"
        stage = self.graph.stages[baton.stage]
        resume_input = self._merge_decision(baton.pending, response)
        baton.pending = None
        baton.stage = self._next_stage(stage, resume_input)
        return await self._settle(baton, resume_input)

    @staticmethod
    def _merge_decision(pending: Optional[Envelope], response: Envelope) -> Envelope:
        """Fold the human decision onto the failed stage's artifact (decision keys
        win). No prior artifact (a plain gate) → just the response."""
        if pending is None:
            return response
        payload = dict(pending.payload)
        payload.update(response.payload)
        return Envelope(kind=response.kind, payload=payload, headers=dict(pending.headers))

    # -- internals --

    async def _settle(self, baton: Baton, input: Envelope) -> Outcome:
        """Drive the run, then SAVE the baton if it parked (so resume() — possibly
        in another process — can find it) or DELETE it on any terminal outcome (a
        returned Done or a raised exception, e.g. StageFailed). This is what bounds
        the store: it only ever holds runs parked awaiting a resume. (delete is a
        no-op when the baton was never saved — a run that finished without parking.)"""
        try:
            outcome = await self._drive(baton, input)
        except StageFailed:
            await self.batons.delete(baton.id)   # logical terminal — evict
            raise
        except BaseException:
            # NON-logical failure (a transport/store blip, cancellation, an engine
            # bug): do NOT evict. A baton lives in the store ONLY because it
            # previously PARKED (suspended, awaiting a human) — so deleting it on an
            # infrastructural error would nuke a resumable run and lose the human's
            # pending decision (the blanket-delete bug). Leave it in its
            # last-persisted state for a later resume / the TTL sweep; a still-running
            # baton was never saved, so nothing leaks either way.
            raise
        if isinstance(outcome, Suspended):
            await self.batons.save(baton)  # parked — persist for resume()
            return outcome
        await self.batons.delete(baton.id)  # Done — terminal, evict
        return outcome

    async def _exec_stage(self, stage: Stage, input: Envelope) -> Union["_Pass", "_Suspend", "_Cleared"]:
        """ONE stage with FULL semantics — the single run-stage seam shared by the
        linear walk (_drive) and the fork branch walk (ForkCoordinator._walk):
        the clearable race, stage/error span emission, and on_error recovery on a
        hard failure. Theme B (assessment): the two walkers had drifted — branch
        stages silently lost clearable / on_error / error-span behavior. One seam
        means they can't drift again.

        A `clearable` stage runs interruptibly: a clear addressed to its node-id
        cancels it in-flight (_Cleared). Per-node error-handling (on_error) runs
        here on a hard failure — OUTSIDE the clearable race, so the recovery's own
        clear can't be mistaken for a cancel."""
        t0 = self._clock()
        try:
            result = await (self._run_clearable(stage, input) if stage.clearable
                            else self._run_stage(stage, input))
        except StageFailed as e:
            await self._spans.error(stage.name, input, t0, e)         # CORE: failures are traced
            await self._handle_error(stage, input, e.output or input, e.verdict)
            raise
        # concerns_from: a passing stage hands its payload-borne soft concerns
        # (e.g. a parsed sceptic report) to the engine channel HERE — in the one
        # seam — so fork branches route them identically and they reach the next
        # gate without payload-threading through the stages in between. The pop is
        # intentional CONSUMPTION: the concerns become engine state, so the key
        # must NOT also ride downstream (this stage's output is freshly produced
        # and unshared, so mutating it in place is safe).
        if stage.concerns_from and isinstance(result, _Pass):
            raised = result.output.payload.pop(stage.concerns_from, None) or []
            result.concerns.extend(self._as_concern(stage, c) for c in raised)
        # Status mapping replaces the old isinstance branching inside SpanEmitter:
        # _Cleared → "cleared", _Suspend → "suspended", _Pass → "ok".
        if isinstance(result, _Cleared):
            _status = "cleared"
        elif isinstance(result, _Suspend):
            _status = "suspended"
        else:
            _status = "ok"
        # Decision provenance: record the value that will drive this stage's branch
        # route (it is already in the output payload at emit time) so the trace
        # answers "why did it go there?" without re-deriving from transient payload.
        route = None
        # A _Suspend result has no `output` (it parked); its parked payload is on
        # `last_output`. Fall back to it so the rendered-artifact `path` (Y2) and
        # any other payload-borne attrs reach the emitter on a suspend too. For a
        # suspend, stage.branch is absent, so the route logic below is untouched.
        out = getattr(result, "output", None) or getattr(result, "last_output", None)
        if stage.branch and out is not None:
            on = stage.branch.get("on")
            if on is not None:
                # distinguish an ABSENT routing key (a typo'd producer → every run
                # silently takes the default) from a present value — a silent
                # misroute is otherwise invisible in the trace.
                route = ("<absent→default>" if on not in out.payload
                         else _route_key(out.payload.get(on)))
        await self._spans.stage(stage.name, input, t0,
                                status=_status,
                                concerns=getattr(result, "concerns", None),
                                output=out, route=route,
                                awaiting=getattr(result, "awaiting", None))
        return result

    def _fold_sticky(self, stage_input: Envelope, stage_output: Envelope) -> None:
        """Re-fold the graph's sticky payload keys from a stage's input into its
        output when the stage dropped them (fill-if-missing: a stage that SET
        the key wins). The engine-level kill for the dropped-key defect class
        (H5) — payload-replacing nodes plus hand-maintained carry lists meant a
        load-bearing key (task, workdir, repo_root...) was eventually forgotten.
        Runs on the linear pass path and on a fork's reduced join."""
        for k in self.graph.sticky:
            if k in stage_input.payload and k not in stage_output.payload:
                stage_output.payload[k] = stage_input.payload[k]

    @staticmethod
    def _as_concern(stage: Stage, item: Any) -> dict:
        """Normalize one concerns_from list item (a dict or a bare string) to the
        same record shape soft validators emit, so gates/reports read one format."""
        if isinstance(item, dict):
            return {"stage": stage.name, "validator": "payload:" + (stage.concerns_from or ""),
                    "code": str(item.get("code", "concern")),
                    "message": str(item.get("message", item)),
                    "fix_hint": str(item.get("fix_hint", ""))}
        return {"stage": stage.name, "validator": "payload:" + (stage.concerns_from or ""),
                "code": "concern", "message": str(item), "fix_hint": ""}

    async def _drive(self, baton: Baton, input: Envelope) -> Outcome:
        steps = 0
        while baton.stage is not None:
            steps += 1
            if steps > self._max_steps:
                # a branch route cycles, or a feedback edge never settles — fail
                # cleanly (StageFailed → evicts) instead of spinning forever.
                raise StageFailed(baton.stage, Verdict.failed(Failure(
                    "step_ceiling",
                    "run exceeded {} stage transitions — a branch route likely "
                    "cycles".format(self._max_steps),
                    "check branch routes for a back-edge")), input)
            stage = self.graph.stages[baton.stage]
            if stage.fork:
                # A fork PRODUCES the join's "clear" (the reduced result) as its
                # output, then continues like any stage: `then` set -> the forking
                # flow resumes carrying the clear (synchronized scatter-gather);
                # `then` None -> terminal. So a fork is just a stage whose work is
                # "spread, wait for the fan-in clear, hand it forward." Branch
                # soft concerns flow into baton.concerns (they used to be dropped
                # inside branches); a branch failure surfaces as StageFailed
                # instead of hanging the clear-wait forever (H2).
                t0 = self._clock()
                try:
                    cleared = await self._fork.run_collect(stage, input,
                                                           concerns=baton.concerns)
                except StageFailed as e:
                    await self._spans.error(stage.name, input, t0, e)
                    raise
                await self._spans.stage(stage.name, input, t0, status="ok")
                # a fan-in REDUCE replaces the payload wholesale — historically the
                # top spot for the dropped-key class; sticky folds here too
                self._fold_sticky(input, cleared)
                input = cleared
                baton.stage = self._next_stage(stage, cleared)
                continue
            # concerns_into: the inverse of concerns_from — a late stage (report
            # renderer) declares it to SEE the run's accumulated soft concerns,
            # which otherwise only reach the terminal Done payload. Copies, so a
            # node mutating its input can't corrupt engine state. The input
            # envelope is the previous stage's unshared output, so setting a key
            # in place is safe (same argument as the concerns_from pop).
            if stage.concerns_into:
                input.payload[stage.concerns_into] = [dict(c) for c in baton.concerns]
            result = await self._exec_stage(stage, input)
            if isinstance(result, _Cleared):
                baton.status = "cleared"  # terminal: current envelope dropped, not resumable
                return Cleared(baton.id, stage.name, result.clear_id, result.payload)
            if isinstance(result, _Suspend):
                baton.status = "suspended"
                baton.parked_at = self._wall()  # wall-clock: TTL must survive a restart (H1)
                baton.pending = result.last_output  # the artifact, for resume to keep
                baton.awaiting = result.awaiting   # the open question, for the mailbox view
                # surface concerns at the gate: those from prior passed stages PLUS
                # this stage's own (when it escalated after a soft validator flagged)
                baton.concerns.extend(result.concerns)
                # surface the gate's rendered question (its `ask`) so the human knows
                # what to answer at stdin / the mailbox
                ask = ""
                if result.last_output is not None:
                    ask = result.last_output.payload.get("ask") or result.last_output.payload.get("question") or ""
                return Suspended(baton.id, result.awaiting, concerns=list(baton.concerns), ask=ask)
            baton.concerns.extend(result.concerns)  # soft gate: noted, not blocking
            self._fold_sticky(input, result.output)
            input = result.output  # handover: output becomes next stage's input
            if stage.clears:  # this node clears the named gate(s) on completion
                await self._clear_bus.publish_clears(stage.clears, input.correlation_id, input.payload)
            baton.stage = self._next_stage(stage, result.output)
        baton.status = "done"
        if baton.concerns:  # soft gate -> noted on the final output (e.g. the report)
            input.payload["concerns"] = list(baton.concerns)
        return Done(input, baton.id)

    # Stage-span / error-span emission lives on `self._spans` (SpanEmitter,
    # elegance #1). The old _emit_stage_span / _emit_error_span methods are gone;
    # call sites use `self._spans.stage(...)` / `self._spans.error(...)`.

    @staticmethod
    def _next_stage(stage: Stage, output: Envelope) -> Optional[str]:
        b = stage.branch
        if not b:
            return stage.then
        routes = b.get("routes", {})
        default = b.get("default", stage.then)
        if b["on"] not in output.payload:
            return default  # field absent → default (not a "None" route key match)
        key = _route_key(output.payload[b["on"]])
        return routes.get(key, default)

    async def _run_stage(self, stage: Stage, input: Envelope) -> Union[_Pass, _Suspend]:
        """Run one stage to a _Pass or _Suspend. Single-node and fan-out stages
        share ONE retry/validate/escalate loop (`_run_attempts`); they differ only
        in how an attempt PRODUCES its output (one request vs a gather+merge), so
        each just supplies a producer. Keeps the two paths from drifting."""
        produce = self._produce_fanout if stage.fanout else self._produce_single
        return await self._run_attempts(stage, input, produce)

    async def _run_attempts(
        self, stage: Stage, input: Envelope,
        produce: Callable[[Stage, Envelope], Awaitable[object]],
    ) -> Union[_Pass, _Suspend]:
        """The shared per-stage loop, bounded by max_attempts: produce -> validate
        -> (pass | retry-with-feedback | escalate-to-human | fail). `produce`
        returns either a _Suspend (a node parked the stage) or a tuple
        (output, pre_verdict) where pre_verdict is a ready Verdict to use as-is
        (e.g. a fan-out error) or None to validate normally. This is the ONLY
        place the retry/escalate policy lives; called by _run_stage with one of
        the producers below."""
        attempt = 0
        errors = 0
        while True:
            produced = await produce(stage, input)
            if isinstance(produced, _Suspend):
                return produced  # a node chose to suspend (gate / await)
            out, pre_verdict = produced
            if pre_verdict is None:
                verdict, soft = await self._validate(stage, out)
            else:  # the producer already decided (e.g. a fan-out role failed)
                verdict, soft = pre_verdict, []
            if verdict.ok:
                return _Pass(out, soft)
            # TRANSIENT-FAULT tolerance on a SEPARATE budget (does NOT spend
            # max_attempts): an infrastructural blip (provider overload/timeout,
            # git index-lock) retries with backoff before it ever counts as a
            # stage failure — so a transient can't fail a max_attempts:1 gate.
            # Idempotent: each retry is a fresh request, and a transient fault is
            # pre-effect. A PERMANENT fault falls straight through to the policy.
            if errors < stage.error_retries and self._is_transient_verdict(verdict):
                errors += 1
                await self._spans.note(stage.name, input, status="error", attrs={
                    "retry": "transient", "n": errors, "error": self._verdict_detail(verdict)})
                await self._sleep(self._backoff(errors))
                continue
            attempt += 1
            if attempt >= stage.max_attempts:
                if stage.escalate == "human":
                    # keep the failed artifact so resume can merge the decision onto
                    # it, AND surface this stage's own soft concerns at the gate.
                    # Fold the failed verdict onto the parked artifact as a GENERIC
                    # scalar dict (same shape as `concerns`, so it round-trips through
                    # the baton store) — otherwise the failure that broke the stage is
                    # thrown away at exactly the moment `yaah list` should show it (Y3).
                    out.payload["escalation"] = {
                        "stage": stage.name,
                        "failures": [{"code": f.code, "message": f.message,
                                      "fix_hint": f.fix_hint} for f in verdict.failures],
                    }
                    return _Suspend("human:" + stage.name, out, concerns=soft)
                # carry the failed artifact on the exception; per-node error-handling
                # (on_error) runs in _drive, OUTSIDE the clearable race (so a self-clear
                # can't turn this failure into a Cleared).
                raise StageFailed(stage.name, verdict, out)
            # a real (logical) retry — record the failed attempt so the trace shows
            # the trajectory, not just the final attempt (per-attempt observability).
            await self._spans.note(stage.name, input, status="error", attrs={
                "retry": "feedback" if stage.feedback else "retry",
                "attempt": attempt, "error": self._verdict_detail(verdict)})
            if stage.feedback:
                input = self._with_feedback(input, out, verdict)

    # Per-reply cap on ingested remote trace records (assessment #6): reply
    # headers are remote-controlled data — without a bound, one malicious or
    # runaway worker could balloon the orchestrator's tracer/sinks per reply.
    _TRACE_INGEST_MAX = 1000

    async def _ingest_remote_trace(self, env: Envelope) -> None:
        """R6 — when a reply arrives carrying spans in `headers["trace"]` (envelope
        carriage), feed them into the local tracer so the orchestrator's sinks /
        own buffer see remote spans alongside local ones. Pop the field after
        ingesting so it doesn't ride further downstream (the orchestrator is the
        terminal consumer; carrying it onward would double-count). Guarded
        (assessment #6): an out-of-tree tracer without `ingest` must not turn a
        successful reply into an AttributeError; non-dict records are dropped
        and the batch is capped — the records are remote-controlled data."""
        recs = env.headers.pop("trace", None)
        if not recs or not hasattr(self._tracer, "ingest"):
            return
        if not isinstance(recs, list):
            return  # malformed carriage field — not worth failing the stage over
        clean = [r for r in recs if isinstance(r, dict)]
        dropped = len(clean) - self._TRACE_INGEST_MAX
        if dropped > 0:
            clean = clean[:self._TRACE_INGEST_MAX]
            clean.append({"name": "trace_truncated", "dropped": dropped,
                          "corr": env.correlation_id})
        if clean:
            await self._tracer.ingest(clean)

    @staticmethod
    def _error_verdict(role: str, env: Envelope) -> Verdict:
        """A node replied Kind.ERROR (a remote transport caught the handler's
        exception — NatsComms.serve does this). Turn it into a ready hard-fail
        verdict so the reply enters the SAME retry/escalate/StageFailed path as
        a validator fail. Without this, an ERROR reply on a validator-less stage
        validated as Verdict.passed() — a failed node sailing through as success
        (H3); in-proc raises, NATS replies ERROR — transports must converge here."""
        return Verdict.failed(Failure(
            "node_error",
            "node {!r} replied ERROR: {}".format(role, env.payload.get("error", env.payload)),
            "see the node's logs/trace for the exception; the error payload is the artifact"))

    async def _safe_request(self, target: str, input: Envelope) -> Envelope:
        """Request a node, CONVERGING the transports: an in-proc node that RAISES
        becomes the same Kind.ERROR reply a remote `serve()` returns (the H3
        convergence, finished for in-proc — `InProcessComms.request` does not
        catch). So a node fault enters the retry / escalate / StageFailed path
        with a traced span and a retained artifact instead of crashing the run as
        a bare traceback. Only `Exception` is caught — `BaseException`
        (cancellation, KeyboardInterrupt) propagates. The error repr feeds the
        transient classifier (a network/lock blip then rides the error-retry
        budget; a logic bug fails fast)."""
        try:
            return await self.comms.request(target, input)
        except Exception as e:
            return Envelope(Kind.ERROR, {"error": repr(e)}, dict(input.headers))

    async def _produce_single(self, stage: Stage, input: Envelope) -> Union["_Suspend", tuple]:
        """One attempt for a single-node stage: one request. An 'await' reply parks
        the stage, keeping what flowed INTO the gate so resume can merge the
        decision onto that artifact (early_review #18); an ERROR reply is a ready
        hard-fail verdict (H3). Used by _run_attempts."""
        out = await self._safe_request(stage.node, input)
        await self._ingest_remote_trace(out)
        if out.kind == Kind.ERROR:  # remote handler raised — fail, don't validate as success
            return out, self._error_verdict(stage.node or stage.name, out)
        if out.kind == Kind.VERDICT:  # a node RETURNED a verdict as its output
            # (e.g. WorktreeNode's dirty-guard refusal). A FAILED one on a
            # validator-less stage would otherwise validate as passed and sail
            # onward, dropping the artifact downstream nodes need (H3 class, the
            # VERDICT variant of the ERROR convergence above). Route it into the
            # SAME retry/escalate/StageFailed path so the failure surfaces HERE,
            # named, instead of as a cryptic error two stages later.
            node_verdict = Verdict.from_envelope(out)
            if not node_verdict.ok:
                return out, node_verdict
        if out.kind == Kind.AWAIT:  # a node (e.g. UI/gate) chose to suspend
            # Park the artifact that flowed INTO the gate (resume merges the human's
            # decision onto it — early_review #18) AUGMENTED with what the gate
            # added — its rendered question/`ask` — so the mailbox view can show the
            # human what to decide. The gate's reply enriches the artifact; it does
            # not replace it (the spec/diff under decision must survive to resume).
            parked = Envelope(kind=input.kind,
                              payload={**input.payload, **out.payload},
                              headers=dict(input.headers))
            return _Suspend(str(out.payload.get("awaiting", "external")), parked)
        return out, None  # validate normally

    async def _produce_fanout(self, stage: Stage, input: Envelope) -> Union["_Suspend", tuple]:
        """One attempt for a fan-out stage: request every role in parallel, then
        merge into one envelope. return_exceptions so one role's failure surfaces
        as a ready fan-out-error verdict (handled as a StageFailed by the loop)
        without discarding the others — a Kind.ERROR reply (a remote handler
        raised, H3) counts as a failed role exactly like a local exception; any
        role choosing to suspend parks the whole stage. Carries the original input fields forward so a post-fan-out branch
        or downstream node can still read domain fields (early_review #17). Used by
        _run_attempts."""
        roles = stage.fanout or []
        results = await asyncio.gather(
            *(self.comms.request(r, input) for r in roles), return_exceptions=True)
        outs, errors = [], []
        for role, res in zip(roles, results):
            if isinstance(res, BaseException):
                errors.append((role, res))
            elif res.kind == Kind.ERROR:  # remote handler raised (H3) — a failed role,
                await self._ingest_remote_trace(res)  # not a result to merge
                errors.append((role, res))
            else:
                await self._ingest_remote_trace(res)  # R6 per-branch trace merge
                outs.append((role, res))

        for _, res in outs:  # a fanned-out node that chose to suspend parks the stage
            if res.kind == Kind.AWAIT:
                return _Suspend(str(res.payload.get("awaiting", "external")))

        merged_payload = dict(input.payload)
        merged_payload.update(results=[res.payload for _, res in outs],
                              roles=[role for role, _ in outs],
                              failed_roles=[role for role, _ in errors])
        merged = input.reply_with(Kind.RESULT, merged_payload)
        if errors:
            return merged, Verdict.failed(Failure(
                "fanout_error",
                "fan-out role(s) failed: {}".format([role for role, _ in errors]),
                "ensure every fan-out node is reachable and succeeds"))
        return merged, None  # validate normally

    # Clear-id matching + clear publication live on the ClearBus (clear_bus.py) —
    # shared by the agent-clear race here and the fork's wait-for-clear.

    # -- agent-clear (cancel an in-flight stage) --

    async def _run_clearable(self, stage: Stage, input: Envelope) -> Union["_Pass", "_Suspend", "_Cleared"]:
        """Run a `clearable` stage while listening for a clear addressed to its
        node-id. The stage's work and the clear race: if the work finishes first it
        wins (normal _Pass/_Suspend); if a matching clear arrives first, the work is
        CANCELLED in-flight ("stop and remove whatever you are doing") and the stage
        yields _Cleared. Sender-agnostic — any party publishing the clear (a human, a
        timer, a sibling node) can interrupt it. Opt-in via Stage.clearable; only safe
        for reversible work (committed side effects need compensation, not cancel)."""
        node_id = stage.id or stage.name
        x = "{}:{}".format(node_id, input.correlation_id)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        async def _on_clear(env: Envelope) -> None:  # match by address; ignore the sender
            # The future was created on `loop` (the harness loop). If a transport
            # (NATS has an I/O thread) dispatches this callback from a DIFFERENT
            # loop/thread, calling fut.set_result() directly is undefined behavior.
            # Routing through loop.call_soon_threadsafe queues the set on the right
            # loop — correct on every transport, free on the same-loop case.
            if fut.done() or not self._clear_bus.matches(env.headers.get("clear_id"), x, node_id):
                return
            loop.call_soon_threadsafe(_safe_set, fut, env)

        sub = await self._clear_bus.subscribe(_on_clear)
        work = asyncio.ensure_future(self._run_stage(stage, input))
        try:
            await asyncio.wait({work, fut}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()  # finished first — propagate _Pass/_Suspend (or raise)
            work.cancel()            # cleared first — drop the in-flight work
            await asyncio.gather(work, return_exceptions=True)
            env = fut.result()
            return _Cleared(env.headers.get("clear_id"), dict(env.payload))
        finally:
            sub.cancel()

    # -- fork / fan-in --
    # The spread-to-N-branches + wait-for-fan-in + reduce shape lives on
    # ForkCoordinator (fork_coordinator.py). The Harness only invokes it via
    # `self._fork.run_collect(stage, input)` from `_drive`. The coordinator
    # calls back into the harness for stage execution / routing / clear publishing.

    async def _handle_error(self, stage: Stage, input: Envelope, out: Envelope,
                            verdict: Verdict) -> None:
        """Per-node error recovery on TERMINAL failure — the alias from the error-
        handling design. `on_error` resolves to one of two recoveries, composing the
        dumb primitives (clear signal + store delete + compensation):
          - "clear" (reversible node): publish a clear for this node-id (release any
            waiter) and drop its parked set from the store — the in-memory state IS
            the only thing to undo.
          - {"compensate": T} (side-effecting node): run the node-specific undo target
            T (a call_target fn:/node:/http:), which receives the corr + failed
            artifact + error codes so it can target what to roll back, then drop the
            parked set. If the undo itself fails, `on_compensate_fail` picks the
            severity ("error" default = escalate loud / "warn" = note + tolerate).
        No-op when `on_error` is unset (fail straight through). It does NOT swallow the
        failure — recovery runs, then the caller still raises StageFailed; cleanup
        leaves the system ready, it doesn't paper over the error."""
        oe = stage.on_error
        if not oe:
            return
        node_id = stage.id or stage.name
        corr = input.correlation_id
        parked_prefix = "{}:{}:".format(node_id, corr)
        if oe == "clear":  # reversible: dropping the in-memory/parked state is the undo
            await self._clear_bus.publish_clears([node_id], corr, dict(out.payload))
            await self._envelopes.flush(parked_prefix)
            return
        if isinstance(oe, dict) and oe.get("compensate"):  # side-effecting: run the undo
            ctx = {"correlation_id": corr, "node": node_id,
                   "payload": dict(out.payload),
                   "error": [f.code for f in verdict.failures]}
            # The undo can ITSELF fail (rollback target down / raises). The
            # component declares how loud via `on_compensate_fail` (slop-fix #6):
            #   "error" (default) → escalate: the un-undone side-effect is live, so
            #                       fail loud, carrying the ORIGINAL failures PLUS a
            #                       compensation_failed one (masks nothing).
            #   "warn"            → tolerate: NOTE it in the trace, let the original
            #                       StageFailed surface (caller re-raises it).
            # `flush` (parked-set bookkeeping, independent of the external effect)
            # always runs.
            # Bounds: only a RAISED failure is caught (fn:/http: that raise, node:
            # whose invoke raises) — a node: target that instead RETURNS an error
            # envelope doesn't raise, so it reads as success (no engine contract for
            # "a result payload means the undo failed"). An unknown on_compensate_fail
            # value falls to the "error" branch (fail loud — the safe default).
            try:
                await call_target(oe["compensate"], ctx, comms=self.comms)
            except Exception as ce:  # the undo failed
                # Trace + StageFailed identity is stage.name (as everywhere else —
                # the origin span at _drive and the origin StageFailed both use it);
                # node_id/parked_prefix stay the addressing identity for bookkeeping.
                await self._spans.note(stage.name, input, status="error", attrs={
                    "event": "compensation_failed", "target": str(oe["compensate"]),
                    "on_compensate_fail": oe.get("on_compensate_fail", "error"),
                    "error": str(ce)})
                await self._envelopes.flush(parked_prefix)
                if oe.get("on_compensate_fail", "error") == "warn":
                    return  # noted; the original StageFailed re-raises at the caller
                raise StageFailed(stage.name, Verdict.failed(
                    *verdict.failures,
                    Failure("compensation_failed",
                            "compensate target {!r} failed: {}".format(oe["compensate"], ce))),
                    out) from ce
            await self._envelopes.flush(parked_prefix)

    async def _validate(self, stage: Stage, out: Envelope) -> "tuple[Verdict, List[dict]]":
        """Returns (verdict, soft_concerns). A hard fail stops the line. Soft
        fails don't block but are RECORDED as concerns (the design's "soft gate
        -> noted in the report, continues"), tagged with the validator role."""
        soft: List[dict] = []
        for role in stage.validators:  # cheap/deterministic first by list order
            vout = await self._safe_request(role, out)
            await self._ingest_remote_trace(vout)  # R6 — validator may have traced too
            if vout.kind == Kind.ERROR:  # the VALIDATOR itself crashed remotely (H3):
                # hard-fail carrying the actual error, not an empty no-status verdict
                return self._error_verdict(role, vout), soft
            verdict = Verdict.from_envelope(vout)
            if not verdict.ok:
                if verdict.severity == "hard":
                    return verdict, soft  # hard fail stops the line
                soft.extend({"stage": stage.name, "validator": role, "code": f.code,
                             "message": f.message, "fix_hint": f.fix_hint}
                            for f in verdict.failures)
        return Verdict.passed(), soft

    @staticmethod
    def _with_feedback(input: Envelope, out: Envelope, verdict: Verdict) -> Envelope:
        payload = dict(input.payload)
        payload["priorAttempt"] = out.payload
        payload["feedback"] = [
            {"code": f.code, "message": f.message, "fix_hint": f.fix_hint}
            for f in verdict.failures
        ]
        return Envelope(kind=input.kind, payload=payload, headers=dict(input.headers))
