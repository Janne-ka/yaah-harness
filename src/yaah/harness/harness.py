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
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

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
from .lease_state import DEFAULT_LEASE_HORIZON, LIVE, LeaseState, mint_owner
from .span_emitter import SpanEmitter
from .stage import Stage
from .stage_failed import DecisionRejected, StageFailed
from .suspended import Suspended
from .wiring_fingerprint import wiring_fingerprint

Outcome = Union[Done, Suspended, Cleared]

_UNSET = object()  # "ttl argument not provided" — distinct from ttl=None (never expire)

# Livelock backstop for the linear walk: a backward `branch` route or a runaway
# feedback edge could spin `_drive`'s `while baton.stage is not None` forever. Far
# above any real linear pipeline; overridable per-harness for tests.
_MAX_STAGE_STEPS = 10000

# The reserved resume-envelope HEADER that names WHO approved an override (the
# AI-Act Art. 14(4)(d) audit signal). A header, not a payload key: identity is
# metadata, and a payload key would both risk colliding with a domain decision
# key and leak into the merged decision that flows downstream.
_APPROVER_HEADER = "approver"

# Upper bounds on a resume record's decision_diff, so a pathological decision
# can't bloat the trace (keys only ever, never values): at most _MAX_DIFF_KEYS
# keys per list, each key name clipped to _MAX_DIFF_KEY_CHARS. Both are far
# above any real human approval form.
_MAX_DIFF_KEYS = 40
_MAX_DIFF_KEY_CHARS = 120


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
    # a provider subprocess that died before its pipes opened (claude_cli's
    # immediate-exit shape) is a host blip — without this it classified
    # PERMANENT and back-to-back-burned max_attempts into a spurious human
    # park on unattended runs (mailbox M6). A bare nonzero exit stays
    # permanent: it can be auth/config, and the default is fail-fast.
    "exited before pipe opened",
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
                 tracer: Optional[object] = None,
                 strict_resume: bool = True,
                 owner: Optional[str] = None,
                 lease_host: Optional[str] = None,
                 lease_horizon: float = DEFAULT_LEASE_HORIZON) -> None:
        self.comms = comms
        self.graph = graph
        # The name this deployment calls THIS host (root `lease_host`). Absent =
        # `socket.gethostname()`, so nothing changes for anyone who does not set it.
        # It exists for CONTAINERS, where `gethostname()` is the pod/container id and
        # is different on every restart: the same machine looks like a new host each
        # time, which permanently demotes every lease to tier 3 (the coarse
        # `lease_horizon` age guess) even though `os.kill(pid, 0)` would have been
        # real evidence. Setting `lease_host` to a stable per-NODE identity (the k8s
        # node name, the VM's hostname) restores the same-host pid probe. It must be
        # stable AND unique per kernel — two containers sharing one `lease_host` on
        # DIFFERENT kernels would probe each other's pid namespace and read a
        # coincidental pid as "the owner is alive".
        self._lease_host = lease_host
        # LIVENESS LEASE (docs/durable-state.md §10). One owner id per Harness —
        # "<host>/<pid>/<nonce8>" — stamped on every checkpoint write, so the lease
        # is refreshed at every stage boundary with NO heartbeat thread: feature 2
        # made every stage boundary a store write already, and a lease that rides
        # the write it already had to do costs nothing. Injectable (like `clock`)
        # so a test can play a foreign host or a dead pid.
        self._owner = owner or mint_owner(lease_host)
        # How long a FOREIGN host's lease may go unrefreshed before its process is
        # presumed dead. Only tier 3 uses it — same-host liveness is probed, not
        # guessed (LeaseState).
        self._lease_horizon = lease_horizon
        # The graph's TOPOLOGY hash, computed ONCE here (the graph is immutable for
        # this harness's life) and stamped on each baton at mint. Recovery compares
        # it before re-driving a cursor; see wiring_fingerprint for what it covers.
        self._wiring = wiring_fingerprint(graph)
        # DEFAULT-ON resume-time decision-form enforcement (N1, TIER-0): when a
        # parked gate declared a `form`, a submitted decision is validated against
        # that form's schema BEFORE it is merged/routed; a nonconforming decision
        # raises DecisionRejected instead of silently falling to the branch
        # default. `strict_resume=False` (root config) restores the old lenient
        # blind-merge. The check is a no-op for a gate with no form declared.
        self._strict_resume = strict_resume
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
        # Level 2: has this harness already warned that checkpoint writes are
        # failing? The failure is swallowed (best-effort durability), so without a
        # visible warning an operator learns that recovery was silently off only
        # when a kill leaves nothing to `resume-run`. Warn ONCE per harness — one
        # line names a real problem, one line per stage is noise. See _checkpoint.
        self._checkpoint_warned = False
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
        Gates the separate error-retry budget in _run_attempts.

        A `foreach_error` aggregate is EXEMPT (impl-eval MED): its message embeds
        each failed ITEM's detail, so one item's 429 read as "the stage is
        transient" and re-ran the WHOLE swarm on the error_retries budget —
        15 calls for 5 items at the defaults. The transient-retry rationale
        ("a fresh request, pre-effect") is false for a swarm: the healthy items'
        cost already happened. Partial tolerance is `min_success`, not a re-run."""
        if any(f.code == "foreach_error" for f in verdict.failures):
            return False
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
        (3) drop suspended batons (abandon parked runs) AND Level 2 running
        checkpoints (abandon crashed mid-runs). The process stays alive and ready
        for the next run. Returns counts of what was cleared:
        `{parked_flushed, batons_dropped, checkpoints_dropped}` — `batons_dropped`
        keeps its original meaning (SUSPENDED gate batons only); the Level 2
        running checkpoints are counted separately under `checkpoints_dropped`.

        Running checkpoints are included because otherwise a graceful reset leaves
        them behind FOREVER: a crashed run's checkpoint has no owner to delete it,
        and `clear` is the operator's "start from a clean store" verb. On a SHARED
        durable store a running checkpoint may belong to a run still LIVE in another
        process; `clear` does NOT consult the liveness lease (it is the destructive
        all-or-nothing reset), and dropping the record does not kill that process,
        it only removes its recovery record. Name individual ids with
        `clear --baton` to be surgical, and read `yaah list`'s `lease=` column first.

        SINCE THE FIRST-STAGE CHECKPOINT, `checkpoints_dropped` counts more than it
        used to: a run that has not yet completed a single stage now has a record in
        the store, so it is counted here where before it was invisible. The number
        is bigger for the same fleet — that is the checkpoint becoming complete, not
        a leak.

        (Only `clearable` stages cancel in-flight on the broadcast — by design; a
        committed side-effect node isn't cancellable, see the clearable boundary.)"""
        await self._clear_bus.broadcast()
        parked = await self._envelopes.flush()
        suspended = await self.batons.list_suspended()
        for b in suspended:
            await self.batons.delete(b.id)
        running = await self.batons.list_running()
        for b in running:
            await self.batons.delete(b.id)
        return {"parked_flushed": parked, "batons_dropped": len(suspended),
                "checkpoints_dropped": len(running)}

    async def run(self, task: Envelope, *,
                  ttl: object = _UNSET,
                  checkpoint_ttl: object = _UNSET) -> Outcome:
        """Start a run. `ttl` overrides this baton's parked-gate lifetime (seconds;
        None = never expire) and `checkpoint_ttl` its running-checkpoint sweep
        window (None/omitted = inherit `ttl`); omit both to use the baton defaults."""
        await self.sweep_expired()
        baton = Baton(id=uuid.uuid4().hex, stage=self.graph.start,
                      wiring=self._wiring)
        if ttl is not _UNSET:
            baton.ttl = ttl  # the lifetime is the baton's, set per run
        if checkpoint_ttl is not _UNSET:
            baton.checkpoint_ttl = checkpoint_ttl
        # FIRST-STAGE CHECKPOINT: persist BEFORE driving, so `graph.start` is inside
        # the recoverable window like every other stage. Without this write a kill
        # during the first stage left NOTHING in the store — no baton to
        # `resume-run`, the run re-launched from the top — which is exactly the
        # window a long seeding/discovery first stage sits in.
        await self._checkpoint(baton, task)
        # Level 1 persists only on park; Level 2 (docs/durable-state.md §5) ALSO
        # writes a running checkpoint at the start and after each completed stage
        # (see _checkpoint), so a crash mid-run is recoverable via resume_running.
        # Both bounded: a terminal outcome deletes the baton; a park overwrites the
        # running checkpoint with the suspended record.
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
        # ENFORCE the gate's decision form BEFORE any state mutation and BEFORE
        # `_settle` (N1). A raise here evicts NOTHING: the persisted baton is
        # untouched until `_settle.save/delete`, and `load` returned a detached
        # copy — so a rejected decision leaves the gate PARKED and re-submittable.
        # NEVER move this into `_drive`/`_settle`: a StageFailed there DELETES the
        # baton (see `_settle`), which would strand a resumable run on a typo.
        self._enforce_decision_form(baton_id, baton.pending, response)
        self._check_gate_wiring(baton)
        baton.status = "running"
        stage = self.graph.stages[baton.stage]
        pending = baton.pending  # the gate's EMITTED artifact, captured before the merge clears it
        resume_input = self._merge_decision(pending, response)
        baton.pending = None
        # LOG THE OVERRIDE: without this record the human decision left no trace
        # at all — resume routes PAST the gate (no stage re-execution, so no
        # stage span) and the baton is deleted on completion, so the run's trace
        # ended at status:suspended. Emitted BEFORE the run continues, so the
        # decision is on record even if the continuation fails. Keys only, never
        # values (the RESPONSE payload may be sensitive); corr rides the merged
        # input, which keeps the parked run's correlation_id. `approver` (WHO
        # overrode) rides the resume envelope's header — identity is recorded,
        # content is not. `decision_diff` is the emitted-vs-edited audit.
        await self._spans.resumed(stage.name, resume_input,
                                  awaiting=baton.awaiting,
                                  decision_keys=response.payload.keys(),
                                  approver=response.headers.get(_APPROVER_HEADER),
                                  decision_diff=self._decision_diff(pending, response))
        # CLEAR THE GATE FIELDS now that the decision is delivered and recorded.
        # `pending` was cleared above; `awaiting`/`parked_at` had to wait until the
        # resumed span read them. Since Level 2 this baton is persisted AGAIN while
        # running (the next `_checkpoint`), so leaving them set would publish a
        # running checkpoint that still claims to be awaiting a human at a past
        # park time — `yaah list` would show a contradiction and the debugging.md
        # contract ("awaiting/parked_at are null while running") would be false.
        baton.awaiting = None
        baton.parked_at = None
        baton.stage = self._next_stage(stage, resume_input)
        # FIRST-STAGE-AFTER-A-GATE CHECKPOINT (mirrors the one in `run`): the human's
        # decision is already MERGED into `resume_input`, so persisting here is what
        # makes the decision itself durable. Before this, a kill in the post-gate
        # stage left the SUSPENDED record in the store and the operator had to submit
        # the decision AGAIN; now the run recovers with `resume-run` and the gate
        # never re-opens. Guarded exactly like `_drive`'s: a gate that routed to a
        # terminal `None` has no next stage to checkpoint.
        if baton.stage is not None:
            await self._checkpoint(baton, resume_input)
        return await self._settle(baton, resume_input)

    async def resume_running(self, baton_id: str, *, force: bool = False,
                             allow_rewiring: bool = False) -> Outcome:
        """Recover a run KILLED mid-flight (Level 2 checkpoint durability,
        docs/durable-state.md §5). Loads the running checkpoint for `baton_id` and
        RE-DRIVES from its cursor: the stage that was in flight when the process
        died RE-RUNS (ROADMAP 'per-stage input checkpoint' — re-run the current
        stage, don't skip it), while the completed stages before it are not
        repeated (their outputs are the checkpoint input). Distinct from resume():
        that delivers a human decision to a SUSPENDED gate; this re-drives a
        RUNNING crash with no new input.

        THREE refusals, each with its OWN escape (they are different assertions and
        must never share a flag):
          - not a running checkpoint — a suspended gate (use resume()), a
            finished/swept run (gone), or a baton never checkpointed. No escape.
          - the graph was REWIRED since this baton was minted (wiring_fingerprint):
            the cursor no longer means what it meant. `allow_rewiring=True` (CLI
            `--allow-rewiring`) asserts the edit is cursor-compatible. Prompt/model/
            timeout edits do NOT trip this — see wiring_fingerprint.
          - the owner's process is still LIVE (LeaseState): another process is
            driving this run and re-driving it would double-execute every remaining
            stage. `force=True` (CLI `--force`) asserts the owner is dead anyway
            (a SIGSTOP'd process, a pid the probe cannot see through).

        Single-owner is then CLAIMED, not just checked: the record is re-written
        with a CAS on the revision it was read at, so two operators racing the same
        recovery cannot both win (advisory on a non-CAS backend — BatonStore.claim).

        Re-running the in-flight stage is at-least-once; a committed side effect in
        that stage needs an idempotency guard (§6) to stay exactly-once."""
        await self.sweep_expired()  # a checkpoint past its ttl is already gone
        baton, rev = await self.batons.load_rev(baton_id)
        if baton is None:
            raise KeyError(
                "no baton {!r} to recover — run `yaah list` (a finished or "
                "ttl-swept run leaves nothing to resume)".format(baton_id))
        if baton.status != "running":
            raise ValueError(
                "baton {!r} status is {!r}, not 'running' — resume_running recovers a "
                "crash mid-run; a suspended gate is resumed with a decision via "
                "resume()".format(baton_id, baton.status))
        if baton.cursor_input is None or baton.stage is None:
            raise ValueError(
                "baton {!r} has no checkpoint (no cursor_input/stage) — it was never "
                "checkpointed, so there is no mid-run state to recover".format(baton_id))
        self._check_wiring(baton, allow_rewiring)
        lease = LeaseState.of(baton, self._wall(), self._lease_horizon,
                              host=self._lease_host)
        if not lease.recoverable and not force:
            raise ValueError(
                "baton {!r} is LEASED: {}. Re-driving it would run every remaining "
                "stage twice. Wait for that process to finish or die, or pass "
                "--force if you know it is dead (a stopped process still answers "
                "the liveness probe).{}".format(
                    baton_id, lease.detail,
                    "" if self.batons.has_cas() else
                    " NB this store has no compare-and-set tier, so the "
                    "single-owner claim is ADVISORY here."))
        if not lease.recoverable and force:
            print("warning: --force overriding a {} lease on baton {} (owner {}, "
                  "lease age {:.0f}s) — if that process is actually alive, every "
                  "remaining stage now runs twice.".format(
                      lease.state, baton_id, lease.owner, lease.age or 0.0),
                  file=sys.stderr, flush=True)
        elif lease.state != LIVE:
            print("note: recovering baton {} — {}".format(baton_id, lease.detail),
                  file=sys.stderr, flush=True)
        # TAKE the lease before re-driving, under a CAS on the revision we read at:
        # a second operator racing the same recovery loses the claim and is refused
        # rather than silently double-driving the run.
        baton.owner = self._owner
        baton.leased_at = self._wall()
        if not await self.batons.claim(baton, rev):
            current = await self.batons.load(baton_id)
            raise ValueError(
                "lost the claim to {} on baton {!r} — another process wrote this "
                "record between our read and our claim, so it is now recovering it. "
                "Do NOT retry blindly; re-check `yaah list`.".format(
                    (current.owner if current is not None else None) or "another writer",
                    baton_id))
        return await self._settle(baton, baton.cursor_input)

    def _check_gate_wiring(self, baton: Baton) -> None:
        """The GATE-resume half of the wiring check, deliberately WEAKER than
        `_check_wiring`. A parked gate is a human waiting: refusing their decision
        because a stage was added elsewhere in the graph would be a hostile way to
        lose work, and the gate's own stage is the only wiring the resume needs. So
        a mismatch WARNS and continues — EXCEPT when the parked stage itself is gone
        from the current graph, which is not a warning but a broken resume: the very
        next line (`self.graph.stages[baton.stage]`) would raise a bare KeyError with
        no explanation of what happened."""
        if baton.stage is not None and baton.stage not in self.graph.stages:
            raise ValueError(
                "baton {!r} is parked at stage {!r}, which NO LONGER EXISTS in the "
                "current graph — the pipeline was edited while this gate was parked, "
                "so there is nowhere to deliver the decision. Restore the stage (or "
                "the graph the run started on) and resume again; `yaah clear --baton "
                "{}` abandons the run instead.".format(
                    baton.id, baton.stage, baton.id))
        if baton.wiring is not None and baton.wiring != self._wiring:
            print("warning: baton {} was parked on a DIFFERENT graph topology "
                  "(wiring {} vs the current {}); its gate stage {!r} still exists, "
                  "so the decision is being delivered — but the stages AFTER it may "
                  "not be the ones this run was started on.".format(
                      baton.id, baton.wiring[:12], self._wiring[:12], baton.stage),
                  file=sys.stderr, flush=True)

    def _check_wiring(self, baton: Baton, allow_rewiring: bool) -> None:
        """Refuse to re-drive a cursor into a graph that was REWIRED since the baton
        was minted. A pre-upgrade baton carries no fingerprint — skip with a note
        rather than refuse, since refusing would strand every record written before
        the stamp existed and the engine genuinely has no evidence."""
        if baton.wiring is None:
            print("note: baton {} predates the wiring fingerprint — recovering "
                  "WITHOUT a topology check (a graph edit since the kill would go "
                  "undetected).".format(baton.id), file=sys.stderr, flush=True)
            return
        if baton.wiring == self._wiring or allow_rewiring:
            return
        raise ValueError(
            "baton {!r} was minted on a DIFFERENT graph topology (wiring {} vs the "
            "current {}) and its cursor is at stage {!r}, which {}. Re-driving it "
            "would resume onto wiring the run never took. Restore the graph the run "
            "started on, or pass --allow-rewiring if the edit is cursor-compatible. "
            "(Prompt/model/timeout edits do NOT trip this check — only topology: "
            "stages, targets, branch routes, fork/fan-in shape.)".format(
                baton.id, baton.wiring[:12], self._wiring[:12], baton.stage,
                "no longer exists in the current graph"
                if baton.stage not in self.graph.stages else "still exists"))

    async def _checkpoint(self, baton: Baton, next_input: Envelope) -> None:
        """LEVEL 2 write (docs/durable-state.md §5): after a stage completes and the
        cursor advances, persist the baton (status still 'running', `stage` = the
        NEXT stage) carrying the envelope that will feed it (`cursor_input`) — one
        atomic artifact, so a crash can never leave a cursor without its input. A
        later resume_running re-drives from exactly here.

        BEST-EFFORT: the parked-gate save (Level 1) is the authoritative durability
        point, so a checkpoint-write blip must NOT fail the run — it is NOTED on the
        trace and swallowed (the run keeps going; the next stage's checkpoint, or
        the next park, re-establishes durability). Only `Exception` is caught —
        cancellation propagates. Cheap on the default memory backend (a dict put
        that dies with the process); crash-survivable on a durable StoreBackend.

        The FIRST failure also prints one stderr warning. A trace note alone is
        invisible in the moment: crash-recovery would be silently off for the rest
        of the run, and the operator would discover it only when a kill left nothing
        for `resume-run`. Once per harness — the cause is a broken store, not a
        per-stage event, so repeating it every stage would just be noise (and the
        trace already carries every occurrence).

        THE LEASE RIDES THIS WRITE. `owner`/`leased_at` are stamped here and nowhere
        else, which is why there is no heartbeat thread: since the first-stage
        checkpoint, every stage boundary is already a store write, so the lease is
        refreshed exactly as often as the run makes progress. A lease that goes
        unrefreshed therefore means "no progress" — precisely what a recovery caller
        wants to know (LeaseState).

        SO DOES THE WIRING FINGERPRINT, and for the same reason. `wiring` is stamped
        at mint, but a checkpoint is written by whichever harness is DRIVING, so the
        honest value is that harness's graph — not the one the run was first minted
        on. Without the re-stamp, a `--allow-rewiring` recovery drove the edited graph
        while the record still claimed the ORIGINAL topology, so a second kill refused
        again and the operator had to pass the flag once per crash, each time
        asserting compatibility with a graph that was no longer the one that ran.
        Re-stamping makes the record mean "the topology this cursor was produced by",
        which is exactly what the next recovery needs to compare against. The write is
        happening anyway, so this costs nothing.

        The trade is deliberate: `wiring` is no longer mint PROVENANCE. Nothing reads
        it as such (`_check_wiring` / `_check_gate_wiring` both ask "does this cursor
        match the graph I am about to drive?"), and a run's origin belongs on the
        trace, not on a record that is rewritten every stage."""
        baton.cursor_input = next_input
        baton.checkpointed_at = self._wall()
        baton.owner = self._owner
        baton.leased_at = baton.checkpointed_at
        baton.wiring = self._wiring
        try:
            await self.batons.save(baton)
        except Exception as e:
            await self._spans.note(baton.stage or "?", next_input, status="error",
                                   attrs={"event": "checkpoint_failed", "error": repr(e)})
            if not self._checkpoint_warned:
                self._checkpoint_warned = True
                print("warning: checkpoint write failed at stage {!r} ({!r}) — "
                      "crash recovery is OFF for this run (a kill will leave "
                      "nothing to `yaah resume-run`); parked human gates are "
                      "unaffected. Further failures are on the trace only "
                      "(event: checkpoint_failed).".format(baton.stage or "?", e),
                      file=sys.stderr, flush=True)

    def _enforce_decision_form(self, baton_id: str, pending: Optional[Envelope],
                               response: Envelope) -> None:
        """Reject a resume decision that violates the parked gate's declared form
        (N1, TIER-0). No-op when strict_resume is off, when the gate parked
        WITHOUT a prior artifact (a fanout/foreach member's bare AWAIT — nowhere
        the form could have been stamped), or when no `form` was declared (legacy
        gates are unchanged).

        Validates the human's RAW decision (`response.payload`), NOT the merged
        `resume_input`: the form describes what the human submits, and the merge
        would fold in the gate's emitted artifact keys (`raw`, `findings`, ...)
        which the form never described. `form`/`decision_schema` live on the
        parked artifact (`pending.payload`) — HumanGate stamps them on the AWAIT
        envelope, the harness parks that (same source `yaah baton-schema` reads).

        The checker is `yaah.jsonschema.check_schema` (the exact one an agent's
        `output_schema` uses) against `decision_forms.lookup(...)`'s schema — the
        contract and checker already existed; this seam simply wires them. An
        unknown/misconfigured form (lookup raises) FAILS LOUD as the SAME family
        — only reachable via hand-edited durable state, since the builder rejects
        unknown forms at load time (defense in depth)."""
        if not self._strict_resume or pending is None:
            return
        form = pending.payload.get("form")
        if form is None:
            return  # legacy gate: no form declared → no validation
        from ..jsonschema import check_schema
        from .decision_forms import lookup
        try:
            resolved = lookup(form, inline_schema=pending.payload.get("decision_schema"))
        except ValueError as e:
            # the gate's OWN form is invalid (author bug via tampered state) —
            # loud, same exception family, message names the form itself.
            raise DecisionRejected(baton_id, form, [str(e)], form_invalid=True) from e
        errors = check_schema(response.payload, resolved["schema"])
        if errors:
            raise DecisionRejected(baton_id, form, errors)

    @staticmethod
    def _merge_decision(pending: Optional[Envelope], response: Envelope) -> Envelope:
        """Fold the human decision onto the failed stage's artifact (decision keys
        win). No prior artifact (a fanout/foreach member's AWAIT parks without one)
        → the response alone, MINUS the reserved approver header: the identity is
        recorded on the resume span and must stop there — passing the response
        through verbatim carried it into the merged input and downstream (eval
        finding; the pending path never leaked, its headers come from pending)."""
        if pending is None:
            headers = {k: v for k, v in response.headers.items()
                       if k != _APPROVER_HEADER}
            return Envelope(kind=response.kind, payload=dict(response.payload),
                            headers=headers)
        payload = dict(pending.payload)
        payload.update(response.payload)
        return Envelope(kind=response.kind, payload=payload, headers=dict(pending.headers))

    @staticmethod
    def _decision_diff(pending: Optional[Envelope], response: Envelope) -> Dict[str, Any]:
        """Key-level audit of what the human EDITED at the gate — the emitted-vs-
        edited signal a future self-repair corpus reads off the trace record.
        Compares the gate's EMITTED artifact (`pending`) against the human's
        decision (`response`):
          - emitted: keys the gate produced (the pending artifact; [] for a gate
            with no prior artifact),
          - added:   keys the human introduced (in response, not emitted),
          - changed: keys the human overrode (in both, VALUE differs).
        No `removed`: the merge is additive (merged = emitted ∪ response), so an
        emitted key is never dropped from the flow — a `removed` list would be
        provably always empty, i.e. padding.

        KEYS ONLY — values are compared in-memory to detect `changed` but never
        stored (same keys-only contract as decision_keys; a value may be a
        sensitive free-text ruling). Bounded on BOTH axes (eval finding — a count
        cap alone lets one megabyte-long key NAME bloat the record): each list is
        capped at _MAX_DIFF_KEYS entries AND each stored key name is clipped to
        _MAX_DIFF_KEY_CHARS; either cut sets the `truncated` flag."""
        emitted = pending.payload if pending is not None else {}
        resp = response.payload
        added = sorted(k for k in resp if k not in emitted)
        changed = sorted(k for k in resp if k in emitted and emitted[k] != resp[k])
        emitted_keys = sorted(emitted.keys())
        truncated = max(len(emitted_keys), len(added), len(changed)) > _MAX_DIFF_KEYS

        def clip(keys: List[str]) -> List[str]:
            nonlocal truncated
            out = []
            for k in keys[:_MAX_DIFF_KEYS]:
                if len(k) > _MAX_DIFF_KEY_CHARS:
                    truncated = True
                    k = k[:_MAX_DIFF_KEY_CHARS] + "…"
                out.append(k)
            return out

        diff: Dict[str, Any] = {
            "emitted": clip(emitted_keys),
            "added": clip(added),
            "changed": clip(changed),
        }
        if truncated:
            diff["truncated"] = True
        return diff

    # -- internals --

    async def _settle(self, baton: Baton, input: Envelope) -> Outcome:
        """Drive the run, then SAVE the baton if it parked (so resume() — possibly
        in another process — can find it) or DELETE it on any terminal outcome (a
        returned Done or a raised exception, e.g. StageFailed). Together with the
        Level 2 running checkpoints written by `_checkpoint` mid-drive, this is what
        bounds the store: it holds parked runs plus the checkpoints of runs still in
        flight. (delete is a no-op when the baton was never saved.)"""
        try:
            outcome = await self._drive(baton, input)
        except StageFailed:
            await self.batons.delete(baton.id)   # logical terminal — evict
            raise
        except BaseException:
            # NON-logical failure (a transport/store blip, cancellation, an engine
            # bug): do NOT evict. Two kinds of state can be in the store here — a
            # gate that previously PARKED (deleting it would nuke a resumable run
            # and lose the human's pending decision — the blanket-delete bug) and,
            # since Level 2, this run's own RUNNING CHECKPOINT.
            #
            # DO NOT "tidy up" by deleting the running checkpoint on this arm:
            # preserving the last-persisted state IS the crash-recovery mechanism.
            # That checkpoint is precisely what `resume_running` re-drives from, and
            # this arm is the path a killed/blipped run takes out of the engine. A
            # delete here would make every non-logical failure unrecoverable. What
            # bounds the leak is the TTL sweep (Baton.is_expired covers running
            # checkpoints), not an eager delete.
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
        # effects_from (ADR-0008 D2): a PASSING stage hands its author-chosen effect
        # HANDLE (payload[effects_from]) to its completion span so the `yaah
        # rollback` verb has the context an undo needs. Mirrors the concerns_from
        # pull above but COPIES (the descriptor stays on the payload for downstream
        # stages), and only on _Pass — a _Cleared/_Suspend never committed the
        # effect, and a FAILED attempt raised before reaching here (so a retry's
        # note/error span never carries effects). The bound (serialize/clip) lives
        # in the emitter. Passed only when configured so a stage without
        # effects_from records no `effects` attr (backward compatible).
        effects_attr: Dict[str, Any] = {}
        if stage.effects_from and isinstance(result, _Pass):
            effects_attr["effects"] = result.output.payload.get(stage.effects_from)
        await self._spans.stage(stage.name, input, t0,
                                status=_status,
                                concerns=getattr(result, "concerns", None),
                                output=out, route=route,
                                awaiting=getattr(result, "awaiting", None),
                                **effects_attr)
        return result

    def _fold_sticky(self, stage_input: Envelope, stage_output: Envelope) -> None:
        """Re-fold the graph's sticky payload keys from a stage's input into its
        output when the stage dropped them (fill-if-missing: a stage that SET
        the key wins). The engine-level kill for the dropped-key defect class
        (H5) — payload-replacing nodes plus hand-maintained carry lists meant a
        load-bearing key (task, workdir, repo_root...) was eventually forgotten.
        Runs on the linear pass path and on a fork's reduced join.

        A `final: true` TERMINAL stage opts its OUTPUT out of this re-fold — the
        skip is a guard at the linear call site in `_drive`, NOT here and NOT in
        the fork coordinator's walk. `final` is honored only on the linear
        terminal because that is the one place a stage's own output becomes the
        run's Done surface; the fork-machinery fold sites (this method's other
        callers) stay unconditional. validate enforces the match — it rejects
        `final` on any stage with a continuation key AND on any fork-scoped stage
        — so a fork/branch fold never needs a `final` skip here."""
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
                if baton.stage is not None:  # Level 2: checkpoint the MAIN chain
                    await self._checkpoint(baton, input)  # (a fork's inner branches aren't)
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
                # A parked gate is Level 1's own durable artifact — it must NOT also
                # look like a resumable running checkpoint (resume_running would
                # re-drive a run that is actually awaiting a human). Clear the L2
                # cursor as the record transitions running -> suspended.
                baton.cursor_input = None
                baton.checkpointed_at = None
                # ...and the LEASE with it: a parked gate has no owning process. The
                # engine exits at the park, so leaving the lease stamped would leave
                # a dead pid claiming the record, and `yaah list` would label a gate
                # awaiting a human as "owned by <host>/<pid>".
                baton.owner = None
                baton.leased_at = None
                # Pin THIS RUN's correlation id onto the parked artifact before it
                # is persisted. The artifact's own chain can have diverged from the
                # run corr (a feedback-retry envelope copies headers WITHOUT a
                # correlation_id, so its chain restarts on a fresh id) — and resume,
                # possibly in another process, recovers the run corr ONLY from these
                # headers. Without the pin the resume trace record (the logged
                # human-override event) would land under a corr no other record of
                # the run shares — an orphaned audit line. `input` here is the
                # stage-entry envelope, the same one the suspended stage span was
                # emitted with, so this is exactly the corr the trace groups by.
                if result.last_output is not None:
                    result.last_output.headers["correlation_id"] = input.correlation_id
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
            # `final: true` (terminal only, enforced by validate): this stage's
            # output is the run's FINAL word — skip the sticky re-fold so a tidy
            # cleanup stage can drop the loop-state run frame from the Done output.
            if not stage.final:
                self._fold_sticky(input, result.output)
            input = result.output  # handover: output becomes next stage's input
            if stage.clears:  # this node clears the named gate(s) on completion
                await self._clear_bus.publish_clears(stage.clears, input.correlation_id, input.payload)
            baton.stage = self._next_stage(stage, result.output)
            if baton.stage is not None:  # Level 2: persist the resume cursor + its
                await self._checkpoint(baton, input)  # input after each completed stage
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
        """Run one stage to a _Pass or _Suspend. Single-node, fan-out and foreach
        stages share ONE retry/validate/escalate loop (`_run_attempts`); they
        differ only in how an attempt PRODUCES its output (one request vs a
        gather+merge), so each just supplies a producer. Keeps the paths from
        drifting. (validate rejects shape combos, so the order here is not
        load-bearing — foreach first only because its check is cheapest.)"""
        if stage.foreach:
            produce: Any = self._produce_foreach
        elif stage.fanout:
            produce = self._produce_fanout
        else:
            produce = self._produce_single
        return await self._run_attempts(stage, input, produce)

    async def _run_attempts(
        self, stage: Stage, input: Envelope,
        produce: "Callable[[Stage, Envelope], Awaitable[Union[_Suspend, Tuple[Envelope, Optional[Verdict]]]]]",
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

    @staticmethod
    def _not_a_checker_verdict(role: str, env: Envelope) -> Verdict:
        """A role listed in a stage's `validators:[]` replied with a non-verdict
        envelope (kind {!r} here, not 'verdict') — it is an agent/other node, not a
        checker. `Verdict.from_envelope` would read that reply as a fail with an EMPTY
        failures list, so the stage died with an opaque "no failure detail". Name the
        real cause: WHICH validator, WHAT it replied, and the fix. Detection is the
        structural contract (only a checker replies Kind.VERDICT), NOT a hardcoded
        checker-type set — so a custom checker registered via the contract seam is
        unaffected as long as it returns a Verdict.""".format(env.kind)
        return Verdict.failed(Failure(
            "validator_not_a_checker",
            "validator {!r} replied kind {!r}, not a verdict — a role in "
            "`validators:[]` must be a checker node".format(role, env.kind),
            "make {!r} a checker (json_object / json_schema / expect_field / "
            "shell_check) or move it out of `validators:[]` — an agent/other node "
            "returns output, not a Verdict".format(role),
            data={"role": role, "kind": env.kind}))

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

    async def _produce_single(self, stage: Stage, input: Envelope) -> "Union[_Suspend, Tuple[Envelope, Optional[Verdict]]]":
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

    async def _produce_fanout(self, stage: Stage, input: Envelope) -> "Union[_Suspend, Tuple[Envelope, Optional[Verdict]]]":
        """One attempt for a fan-out stage: request every role in parallel, then
        merge into one envelope. return_exceptions so one role's failure surfaces
        as a ready fan-out-error verdict (handled as a StageFailed by the loop)
        without discarding the others — a Kind.ERROR reply (a remote handler
        raised, H3) and a member-RETURNED failed verdict (the fan-out twin of
        _produce_single's H3 VERDICT rule) both count as a failed role exactly
        like a local exception; any role choosing to suspend parks the whole
        stage. Carries the original input fields forward so a post-fan-out branch
        or downstream node can still read domain fields (early_review #17). Used by
        _run_attempts."""
        roles = stage.fanout or []
        results = await asyncio.gather(
            *(self.comms.request(r, input) for r in roles), return_exceptions=True)
        outs, errors = await self._classify_parallel(list(zip(roles, results)))

        for _, res in outs:  # a fanned-out node that chose to suspend parks the stage
            if res.kind == Kind.AWAIT:
                return _Suspend(str(res.payload.get("awaiting", "external")))

        merged_payload = dict(input.payload)
        merged_payload.update(results=[res.payload for _, res in outs],
                              roles=[role for role, _ in outs],
                              failed_roles=[role for role, _ in errors])
        merged = input.reply_with(Kind.RESULT, merged_payload)
        if errors:
            # k-of-n completion (M9a): with `min_success: k` declared, enough
            # healthy members = a PASS with `failed_roles` naming the dead ones
            # (a downstream reducer emits its degraded-mode concern from that),
            # instead of throwing N-1 healthy results away for 1 flaky member.
            if stage.min_success is not None and len(outs) >= stage.min_success:
                return merged, None  # validate normally; degrade is visible, not silent
            return merged, Verdict.failed(Failure(
                "fanout_error",
                "fan-out role(s) failed: {}".format(
                    ", ".join(self._failed_role_detail(role, res) for role, res in errors)),
                "ensure every fan-out node is reachable and succeeds"))
        return merged, None  # validate normally

    async def _classify_parallel(
        self, tagged: "List[Tuple[Any, Any]]",   # reply is Envelope | BaseException (gather)
    ) -> "Tuple[List[Tuple[Any, Envelope]], List[Tuple[Any, object]]]":
        """Split parallel members' replies into (successes, failures) — the ONE
        classification both fanout and foreach use (ADR-0007 D6: shared machinery
        so the shapes can't drift). A failure is a local exception, a Kind.ERROR
        reply (a remote handler raised, H3), or a member-RETURNED failed verdict
        (an agent exhausting its schema/parse attempts — merged as a result it
        would read as a clean pass downstream, the dead-lens class, M8a).
        Ingests every ENVELOPE reply's remote trace (R6), success or failure.
        `tagged` pairs each reply with its caller-meaningful tag (a role name /
        an item index) which flows through untouched."""
        outs: "List[Tuple[Any, Envelope]]" = []
        errors: "List[Tuple[Any, object]]" = []
        for tag, res in tagged:
            if isinstance(res, BaseException):
                errors.append((tag, res))
            elif res.kind == Kind.ERROR:
                await self._ingest_remote_trace(res)
                errors.append((tag, res))
            elif res.kind == Kind.VERDICT and not Verdict.from_envelope(res).ok:
                await self._ingest_remote_trace(res)
                errors.append((tag, res))
            else:
                await self._ingest_remote_trace(res)
                outs.append((tag, res))
        return outs, errors

    async def _produce_foreach(self, stage: Stage, input: Envelope) -> "Union[_Suspend, Tuple[Envelope, Optional[Verdict]]]":
        """One attempt for a foreach stage (ADR-0007): map the stage's node over
        `payload[items]` (a runtime-sized list), bounded to `max_concurrent` in
        flight, then merge. Per-item input is REPLACE + named carries + sticky —
        `{into: element, item_index, carries, sticky}` — never a copy of the
        whole inbound payload (the visible-cost rule, D1). Each per-item
        envelope is built with reply_with so corr/baton/clear_id survive and the
        item's spans stitch into the run's trace (design-eval #3). Merge:
        inbound payload ∪ {results: [{item_index, payload}] pairs in item order
        (compaction-safe provenance, design-eval #2), failed_items: [indexes]}.
        min_success reuses the k-of-n rule; an item replying AWAIT parks the
        WHOLE stage; a stage retry re-runs ALL items (documented v1 semantics).
        Used by _run_attempts."""
        fe = stage.foreach or {}
        key = str(fe.get("items", ""))
        items = input.payload.get(key)
        if not isinstance(items, list):
            # fail LOUD at run time (validate can't know the runtime payload):
            # name the key and the actual type, not a downstream KeyError.
            return input.reply_with(Kind.RESULT, dict(input.payload)), Verdict.failed(Failure(
                "foreach_input",
                "foreach.items key {!r} must hold a list on the stage input; got {}".format(
                    key, type(items).__name__),
                "produce the list upstream (an agent output_schema / a transform provides)"))
        into = str(fe.get("into") or "item")
        carry = [c for c in (fe.get("carry") or []) if isinstance(c, str)]
        limit = int(fe.get("max_concurrent") or 3)
        sem = asyncio.Semaphore(max(1, limit))

        def _item_env(i: int, element: object) -> Envelope:
            payload: Dict[str, Any] = {into: element, "item_index": i}
            for k in carry:
                if k in input.payload:
                    payload[k] = input.payload[k]
            # sticky keys are the run frame (workdir etc.) — auto-included so a
            # per-item worker resolves cwd_from exactly like a fanout member
            # (design-eval #7); REPLACE applies to the bulky domain payload.
            for k in self.graph.sticky:
                if k in input.payload and k not in payload:
                    payload[k] = input.payload[k]
            return input.reply_with(input.kind, payload)

        async def _one(i: int, element: object) -> Envelope:
            async with sem:
                return await self.comms.request(stage.node, _item_env(i, element))

        replies = await asyncio.gather(
            *(_one(i, el) for i, el in enumerate(items)), return_exceptions=True)
        outs, errors = await self._classify_parallel(list(enumerate(replies)))

        for _, res in outs:  # an item that chose to suspend parks the whole stage (v1)
            if res.kind == Kind.AWAIT:
                return _Suspend(str(res.payload.get("awaiting", "external")))

        merged_payload = dict(input.payload)
        merged_payload.update(
            results=[{"item_index": i, "payload": res.payload} for i, res in outs],
            failed_items=[i for i, _ in errors])
        merged = input.reply_with(Kind.RESULT, merged_payload)
        # Default (no min_success): every item must succeed — any failure fails the
        # stage. With min_success declared, k-of-n TOLERATES failures — but the k
        # floor holds UNCONDITIONALLY, not only on the errors path: unlike fanout
        # (where validate bounds min_success ≤ len(roles) statically), the item
        # count is runtime-sized, so an EMPTY or short list can under-deliver with
        # zero failures — that must not read as a clean pass.
        tolerated = stage.min_success is not None and len(outs) >= stage.min_success
        if errors and not tolerated:
            return merged, Verdict.failed(Failure(
                "foreach_error",
                "foreach item(s) failed: {}".format(
                    ", ".join(self._failed_role_detail("item {}".format(i), res)
                              for i, res in errors)),
                "fix the failing items or declare min_success for partial tolerance"))
        if stage.min_success is not None and len(outs) < stage.min_success:
            return merged, Verdict.failed(Failure(
                "foreach_error",
                "only {} item(s) succeeded but min_success={} (items list held {})".format(
                    len(outs), stage.min_success, len(items)),
                "provide more items upstream or lower min_success"))
        return merged, None  # validate normally (degrade under k-of-n is visible, not silent)

    @staticmethod
    def _failed_role_detail(role: str, res: object) -> str:
        """One failed fan-out role, WITH its why — 'role failed' without the
        member's own failure codes forces the operator into the trace to learn
        what a dead lens actually died of (the naming half of M8a)."""
        if isinstance(res, Envelope) and res.kind == Kind.VERDICT:
            codes = ", ".join(f.code for f in Verdict.from_envelope(res).failures)
            return "{} (failed verdict: {})".format(role, codes or "unspecified")
        if isinstance(res, Envelope):  # Kind.ERROR carries the remote repr
            return "{} ({})".format(role, res.payload.get("error", "error"))
        return "{} ({!r})".format(role, res)  # a local exception

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
            # The undo can ITSELF fail; `on_compensate_fail` picks the severity
            # (see docstring; unknown values fall to "error" — fail loud). Only a
            # RAISED failure counts — a node: target RETURNING an error envelope
            # reads as success (no payload-level failure contract). `flush`
            # (bookkeeping) always runs.
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
            if vout.kind != Kind.VERDICT:  # the role in `validators:[]` is NOT a checker:
                # an agent/other node replied `result` (no verdict shape), which
                # `from_envelope` would default to a fail with an EMPTY failures list —
                # an opaque "no failure detail" stage-fail. Name the misconfiguration
                # instead (structural signal: only a checker replies Kind.VERDICT — this
                # holds for custom checkers too, they carry their verdict the same way).
                return self._not_a_checker_verdict(role, vout), soft
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
