"""ForkCoordinator — owns fork spread + fan-in rejoin (in-memory, in-process).

Used by: Harness, which delegates a stage whose `stage.fork` is set via
`run_collect(stage, input) -> Envelope`.
Where: split out of harness.py (elegance #1, part 2 — assessment 2026-06-09) so
the run loop holds the linear path (single stage -> attempts -> retry / escalate
/ handover) and this owns the parallel path (spread to N branches, optionally
wait for a fan-in clear, reduce, continue). Same semantics as before; one
concern out of the run loop.
Why: the harness module had two distinct responsibilities tangled. The line and
the spread/rejoin are different shapes; separating them lets each be read as one
idea.

Collaborators are EXPLICIT (review 2026-06-11): the ClearBus (subscribe/match/
publish clear signals), the EnvelopeStore (park fan-in arrivals), the Tracer +
clock (one race-error span), and Comms (timeout listeners, reduce targets) are
all passed in. The ONLY reach back into the Harness is the shared run-stage
seam — `graph` / `_exec_stage` / `_next_stage` — so branches execute stages
exactly like the linear walk (theme B: one seam, no drift).

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..core import Envelope, Failure, Kind, Verdict
from ..external_call import call_target
from ..trace import Span
from .reduce import default_reduce
from .stage import Stage
from .stage_failed import StageFailed

if TYPE_CHECKING:                       # runtime import would be circular
    from .harness import Harness


class _WaitDetermined(Exception):
    """The timed fork wait's outcome is already DETERMINED (an arm died, or the
    join is provably unmeetable) — raised so the declared degrade happens now,
    with the real reason on the on_timeout listener, instead of after minutes
    of looks-hung TTL (M9b). Private to run_collect's timed path."""

    def __init__(self, reason: str, detail: "Optional[str]") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def _safe_set(fut: "asyncio.Future", value: object) -> None:
    """Idempotent future.set_result — drops the call if the future is already
    done (a race between two clear publishers). Scheduled via
    `loop.call_soon_threadsafe` so a NATS-thread dispatch can't race the
    harness loop."""
    if not fut.done():
        fut.set_result(value)


@dataclass
class _ForkCtx:
    """In-memory state for ONE fork (in-process, this run only). `joins` holds a
    rendezvous per fan-in stage; `tasks` are ALL background coroutines (branch
    walks + fan-in coordinators) to drain before the run finishes; `branches` is
    the subset that are branch walks — once every branch has settled, fan-in
    arrivals are FINAL, which is what lets the drain/wait release a join whose
    policy can no longer be met instead of hanging forever (H2). `concerns`
    collects branch stages' soft validator concerns (the caller passes the
    baton's list, so they surface like linear-path concerns instead of being
    dropped). `result` is the terminal of the rejoined forward path (the fan-in's
    continuation). Deliberately NOT durable — how a fan-in joins is a swappable
    component concern, not core state."""
    joins: dict = field(default_factory=dict)
    tasks: list = field(default_factory=list)
    branches: list = field(default_factory=list)
    concerns: list = field(default_factory=list)
    result: Optional[Envelope] = None


class ForkCoordinator:
    """Run a fork stage to its rejoined output. One instance per Harness; methods
    are re-entrant for nested forks (a fresh _ForkCtx per call to run_collect)."""

    def __init__(self, harness: "Harness", *, comms: object, clear_bus: object,
                 envelopes: object, tracer: object, clock: object) -> None:
        # `harness` is reached ONLY for the shared run-stage seam (graph /
        # _exec_stage / _next_stage — theme B: branches must run stages exactly
        # like the linear walk). Everything else is an explicit collaborator.
        self._h = harness
        self._comms = comms
        self._bus = clear_bus
        self._envelopes = envelopes
        self._tracer = tracer
        self._clock = clock

    async def run_collect(self, stage: Stage, input: Envelope,
                          concerns: Optional[list] = None) -> Envelope:
        """Spread to the branch stages and produce the fork's output. Two shapes:

        TERMINAL (no `then`, no `wait`) — spread, let the branches + the fan-in's own
        `then` run to completion, return that (the decoupled a/b/c/d case).

        STRUCTURED (`then` set, or `wait` declared) — wait for the fan-in's CLEAR:
        `clear(x)` where x = correlation_id, delivered over the `clear` topic. It is
        SENDER-AGNOSTIC — the fan-in, a human, a timer, ANY party may publish it; the
        fork matches on the id, not the sender. `wait: {timeout, on_timeout, clear_topic}`
        bounds it; on timeout it publishes to the listener, abandons the branches, and
        proceeds with the unchanged input. Gates inside a fork branch are unsupported.

        `concerns` (the caller's list, e.g. the baton's) collects branch soft
        concerns. A branch failure SURFACES as the branch's StageFailed — with no
        `wait.timeout` the old code awaited the clear unconditionally, so a dead
        branch meant an unmeetable fan-in and a run hung forever (H2)."""
        ctx = _ForkCtx(concerns=concerns if concerns is not None else [])
        wait = stage.wait or {}
        if stage.then is None and not wait:  # terminal / decoupled
            excs = await self._spread(stage, input, ctx)
            await self._drain(ctx)
            if excs:  # H2 terminal case: a branch failure must not vanish into a task
                for extra in excs[1:]:  # don't lose siblings — only excs[0] is raised
                    await self._error_span(stage.name, input.correlation_id,
                                           "fork_branch_failed: " + repr(extra))
                raise excs[0]
            return ctx.result if ctx.result is not None else input

        # x = this gate's address = "<node-id>:<correlation_id>". node-id is the gate's
        # configurable unique name (`id`, default the stage name); correlation_id is the
        # run. The pair is unique per gate-per-run AND addressable — any clearer that
        # knows the gate's node-id + the run can target it. It rides each branch
        # envelope (header `clear_id`, preserved through replies) to the fan-in.
        node_id = stage.id or stage.name
        x = "{}:{}".format(node_id, input.correlation_id)
        branch_input = Envelope(input.kind, dict(input.payload),
                                {**input.headers, "clear_id": x})
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        async def _on_clear(env: Envelope) -> None:  # match by address; ignore sender
            if fut.done() or not self._bus.matches(
                    env.headers.get("clear_id"), x, node_id):
                return
            loop.call_soon_threadsafe(_safe_set, fut, env)

        topic = wait.get("clear_topic", "clear")
        sub = await self._bus.subscribe(_on_clear, topic)
        for s in (stage.fork or []):  # spread as background tasks
            t = asyncio.ensure_future(self._walk(s, branch_input, branch_id=s, ctx=ctx))
            ctx.tasks.append(t)
            ctx.branches.append(t)
        timeout = wait.get("timeout")
        try:
            if timeout is not None:
                # M9b: the TIMED wait also watches the branches — a dead arm /
                # provably-unmeetable join degrades NOW (raising _WaitDetermined
                # into the same handling as the TTL) instead of sitting out the
                # full TTL looking hung. The TTL keeps its pure LIVENESS role.
                cleared_env = await asyncio.wait_for(
                    self._timed_clear(fut, ctx), timeout)
            else:  # H2: unbounded wait watches the branches too — see _await_clear
                cleared_env = await self._await_clear(fut, ctx, stage)
            await self._drain(ctx)  # branches/coordinator settle
            # reply_with off `input` keeps correlation_id (trace) + any OUTER clear_id
            cleared = input.reply_with(Kind.RESULT, dict(cleared_env.payload))
        except (asyncio.TimeoutError, _WaitDetermined) as e:
            reason = e.reason if isinstance(e, _WaitDetermined) else "wait_timeout"
            detail = e.detail if isinstance(e, _WaitDetermined) else None
            await self._publish_wait_timeout(stage, wait, reason=reason, detail=detail)
            for t in ctx.tasks:  # abandon outstanding branches
                t.cancel()
            await asyncio.gather(*ctx.tasks, return_exceptions=True)  # retrieve, no warnings
            cleared = await self._degraded_output(stage, input, ctx, reason, detail)
        finally:
            sub.cancel()
        return cleared

    async def _degraded_output(self, stage: Stage, input: Envelope, ctx: _ForkCtx,
                               reason: str, detail: "Optional[str]") -> Envelope:
        """Build the fork's output for a DEGRADE (TTL elapsed / arm died / join
        unmeetable), delivering whatever the fan-in already collected.

        Why (M33): the degrade used to return the pre-fork `input` unchanged, so
        a branch that had ALREADY deposited at the fan-in was thrown away — the
        completed arm's work sat in the EnvelopeStore, paid for and unreachable,
        while the continuation ran on a payload with no branch results at all.
        Downstream that reads as "the whole fork produced nothing", which is a
        LIE whenever an arm finished. Same lesson as the fan-out's `min_success`
        degrade (M9a): hand the healthy results forward with a marker naming what
        is missing, and let the app decide — never silently discard them.

        The engine stays domain-free: it delivers the arrivals under the reserved
        `fork_partial` key ({fork, reason, detail, results:{branch_id: payload},
        expected, missing}) and takes no view of their shape. A reducer is NOT run — the
        fan-in's `reduce` is declared for a MET policy, and calling it on a
        partial set would fabricate a full-fork result. With NO arrivals the
        pre-fork input is returned exactly as before (fully backwards
        compatible), so a degrade with nothing to show stays loud downstream.

        The parked sets are flushed here, as every other end-of-join path does —
        the data now rides the payload, so the store must not keep a second copy.

        SALVAGE MUST NOT BE WORSE THAN NO SALVAGE (review MED-2). The body this
        replaced was infallible — it returned `input`. Draining a store can fail
        (a full disk, a swept file, a backend that lost its connection), and a
        degrade that CRASHES is strictly worse than one that delivers nothing.
        So the whole drain is guarded: on any store error the fault is traced and
        the pre-fork input is returned — exactly the old behaviour.
        """
        try:
            arrived, expected = await self._collect_parked(stage, ctx)
        except Exception as e:                      # noqa: BLE001 — see docstring
            await self._error_span(
                stage.name, input.correlation_id,
                "fork_partial_salvage_failed: " + repr(e))
            return input
        if not arrived:
            return input
        # No `arrived` list: it is exactly `sorted(results)`, and a derivable field
        # on a published contract is a field that can go stale. `missing` stays —
        # it is NOT derivable from `results` alone (it needs `expected`) and is the
        # one an app actually branches on.
        partial = {"fork": stage.name, "reason": reason, "results": arrived,
                   # what the join WANTED, so a listener can name the arm that
                   # never landed (see _collect_parked for when it is knowable)
                   "expected": sorted(expected),
                   "missing": sorted(expected - set(arrived))}
        if detail is not None:
            partial["detail"] = detail
        payload = dict(input.payload)
        payload["fork_partial"] = partial
        return input.reply_with(Kind.RESULT, payload)

    async def _collect_parked(self, stage: Stage, ctx: _ForkCtx) -> tuple:
        """`({branch_id: payload}, expected_branch_ids)` — the degrade's evidence,
        drained out of this fork's joins and released from the store.

        LIVE LISTING FIRST, SNAPSHOT SECOND (review HIGH-1). A join whose own
        coordinator already exited (fan-in timeout / unmeetable / broken reduce)
        has ALREADY flushed its parked set, and on two of the three degrade
        routes it does so BEFORE this runs — see `_release_join`. Those exits
        leave the arrivals on `join["parked"]`, so an empty live listing falls
        back to that snapshot instead of reading "nothing arrived".

        `expected` — what the join WANTED, so a listener can name the arm that
        never landed. Knowable in two shapes: a list `expect` names its branches
        outright; a `{"count": n}` expect names none, but a count EQUAL to the
        fork's width means "every arm", and branch ids ARE the fork's stage names
        (`run_collect`/`_spread` call `_walk(s, branch_id=s)` over `stage.fork`),
        so naming them is reporting, not inventing. Any other count, or an
        omitted `expect` ("whatever arrives"), leaves it empty — the engine will
        not guess which arms a partial policy wanted.

        CAVEAT — MULTI-JOIN FORKS FLATTEN (review LOW-6). A fork with two fan-ins
        merges every join's arrivals into ONE `{branch_id: payload}` map, so two
        joins reached by the same branch id would collide (last wins) and
        `expected` is the union across joins rather than per-join. Every fork
        shipped on this engine has exactly ONE join, and a per-join shape would
        change the published `fork_partial` contract for a case nobody has;
        stated here rather than papered over.
        """
        arrived: dict = {}
        expected: set = set()
        for name, join in ctx.joins.items():
            parked = await self._envelopes.list(join["addr"] + ":")
            if not parked:
                parked = join.get("parked") or []
            for key, env in parked:
                arrived[key.rsplit(":", 1)[-1]] = env.payload
            await self._envelopes.flush(join["addr"] + ":")
            exp = (self._h.graph.stages[name].fanin or {}).get("expect")
            if isinstance(exp, list):
                expected.update(exp)
            elif isinstance(exp, dict) and exp.get("count") == len(stage.fork or []):
                # `.get("count")` with NO default: an expect that names no count
                # ({"any": true}) wanted no particular set, and defaulting it to 1
                # made a width-1 fork report `expected: [that arm]` for a policy
                # that never asked for it — guessing, in the method that says it
                # will not. A non-numeric count simply never equals the width.
                expected.update(stage.fork or [])
        return arrived, expected

    async def _release_join(self, join: dict) -> None:
        """Release one join's parked set, keeping a SNAPSHOT on the join first.

        Why (review HIGH-1): M33 promised the salvage on all THREE degrade routes
        but delivered it on `wait_timeout` alone. On `branch_failed` and
        `fanin_unmeetable` the fan-in coordinator reaches its own exit FIRST —
        `_release_unmeetable_joins` sets the join's event from inside the watch
        loop, the coordinator wakes and flushes, and only then does the loop
        observe `doomed` and let `run_collect` degrade. The live listing in
        `_collect_parked` was empty by then, so a completed arm was discarded
        exactly as before the fix (verified by repro; both routes now pinned).

        Snapshotting keeps no second copy in the STORE — the flush still happens
        — only in this run's in-memory join, which dies with the fork. The list
        is guarded because this runs in a background coordinator whose exceptions
        `_drain` retrieves and discards: a store fault here must degrade the
        salvage, never turn a handled join error into a vanished one.
        """
        try:
            join["parked"] = await self._envelopes.list(join["addr"] + ":")
        except Exception:                           # noqa: BLE001 — see docstring
            join["parked"] = []
        await self._envelopes.flush(join["addr"] + ":")

    async def _error_span(self, stage_name: str, corr: "Optional[str]",
                          error: str) -> None:
        """One error span for a fork/join fault, in the one shape every sink in
        this module already emits (`parent` = the owning stage, `error` = a short
        code plus its detail). A helper so the four fault sites cannot drift into
        four different span shapes."""
        await self._tracer.emit(Span.timed(
            "stage", corr=corr or "", parent=stage_name,
            t0=self._clock(), t1=self._clock(), status="error",
            attrs={"stage": stage_name, "error": error}))

    async def _watch_branches(self, fut: "asyncio.Future", ctx: _ForkCtx) -> tuple:
        """The shared H2 watch loop: await the clear while watching the branch
        tasks. Returns (cleared_env | None, first_branch_exc | None, doomed) —
        cleared set means the fan-in (or an external party) published; otherwise
        every branch has settled without a clear and the CALLER decides: the
        unbounded wait fails loud, the timed wait degrades (M9b)."""
        while not fut.done():
            pending = [t for t in ctx.tasks if not t.done()]
            if not pending:
                break
            self._release_unmeetable_joins(ctx)
            await asyncio.wait([fut, *pending], return_when=asyncio.FIRST_COMPLETED)
        if fut.done():
            return fut.result(), None, False
        exc = next((t.exception() for t in ctx.tasks
                    if not t.cancelled() and t.exception() is not None), None)
        doomed = any(j.get("doomed") for j in ctx.joins.values())
        return None, exc, doomed

    async def _timed_clear(self, fut: "asyncio.Future", ctx: _ForkCtx) -> Envelope:
        """The timed wait's body (bounded by wait_for in run_collect). Watches
        the branches; the moment the degrade outcome is DETERMINED — an arm
        died, or the join is provably unmeetable — raises _WaitDetermined so
        the caller degrades immediately with the real reason (M9b). A CLEAN
        settle keeps waiting for a possible external clear (the sender-agnostic
        no-fan-in pattern) until the TTL fires."""
        env, exc, doomed = await self._watch_branches(fut, ctx)
        if env is not None:
            return env
        if exc is not None:
            raise _WaitDetermined("branch_failed", repr(exc))
        if doomed:
            raise _WaitDetermined("fanin_unmeetable", None)
        return await fut  # clean settle: an external clear may still arrive

    async def _await_clear(self, fut: "asyncio.Future", ctx: _ForkCtx,
                           stage: Stage) -> Envelope:
        """Await the fan-in clear WITHOUT `wait.timeout` (H2). The old code awaited
        `fut` unconditionally: a branch that failed meant the fan-in policy was
        never met, nobody published the clear, and the run hung forever with the
        branch's StageFailed swallowed inside its task. Now the wait also watches
        the background tasks: once every BRANCH has settled, arrivals are final,
        so any join still unmet is released as unmeetable (its coordinator exits
        the timeout way); when everything has settled with no clear, an ERROR
        (failed branch / unmeetable join) fails the fork. A CLEAN settle keeps
        waiting: the clear is sender-agnostic — an external party may still
        publish it (the no-fan-in pattern); bound that with wait.timeout."""
        env, exc, doomed = await self._watch_branches(fut, ctx)
        if env is not None:
            return env
        if exc is not None:  # surface the FIRST branch failure as the cause
            raise exc
        if doomed:
            raise StageFailed(stage.name, Verdict.failed(Failure(
                "fork_no_clear",
                "fork {!r}: every branch settled but the fan-in policy was never met, "
                "so no clear was published".format(stage.name),
                "check fanin.expect/wait against the fork's branch list, or bound the "
                "fork with wait.timeout")))
        return await fut  # clean settle, no fan-in involved: await the external clear

    def _release_unmeetable_joins(self, ctx: _ForkCtx) -> None:
        """Once every branch task has settled, fan-in arrivals are FINAL — a join
        whose policy isn't met by then can never be (policies are monotone over
        arrivals). Mark it doomed and set its event so the coordinator exits via
        its failure path instead of waiting forever (H2). No-op while branches
        are still running. (An empty `branches` list means the terminal path,
        where _spread has already gathered every branch inline.)"""
        if not all(b.done() for b in ctx.branches):
            return
        for join in ctx.joins.values():
            if not join["event"].is_set():
                join["doomed"] = True
                join["event"].set()

    async def _spread(self, stage: Stage, input: Envelope, ctx: _ForkCtx) -> list:
        """Spread `input` to every fork successor STAGE, walking them concurrently.
        return_exceptions so ONE failed branch doesn't abandon its siblings
        mid-flight; returns the branch exceptions for the caller to surface
        AFTER the drain (H2: they used to propagate out of gather and vanish)."""
        results = await asyncio.gather(
            *(self._walk(s, input, branch_id=s, ctx=ctx) for s in (stage.fork or [])),
            return_exceptions=True)
        return [r for r in results if isinstance(r, BaseException)]

    async def _drain(self, ctx: _ForkCtx) -> None:
        """Await branch tasks + fan-in coordinators (which may spawn more) until
        none remain — so the run isn't Done until every branch + rejoined path has
        settled. Incremental (FIRST_COMPLETED) rather than batch-gather: a batch
        containing a coordinator stuck on an unmeetable join would never finish;
        each pass releases doomed joins once the branches have settled (H2).
        Exceptions are retrieved, not re-raised — a failed branch is already
        traced at the stage that raised it, and the caller surfaces it."""
        while True:
            pending = [t for t in ctx.tasks if not t.done()]
            if not pending:
                await asyncio.gather(*ctx.tasks, return_exceptions=True)  # retrieve all
                return
            self._release_unmeetable_joins(ctx)
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _walk(self, stage_name: Optional[str], input: Envelope, *,
                    branch_id: str, ctx: _ForkCtx) -> Optional[Envelope]:
        """Walk one branch from `stage_name` until a terminal, a fan-in, or a nested
        fork. Returns the terminal envelope, or None if it deposited at a fan-in /
        handed off to a sub-fork / was cleared in-flight. `branch_id` identifies
        this branch to a fan-in. Stage execution goes through the harness's ONE
        run-stage seam (`_exec_stage`, theme B) so branches get clearable /
        on_error / span semantics identical to the linear walk."""
        from .harness import _Cleared, _Suspend  # local: private to the run loop
        while stage_name is not None:
            stage = self._h.graph.stages[stage_name]
            if stage.fanin:
                await self._fanin_arrive(stage, input, branch_id, ctx)
                return None  # the fan-in coordinator owns the continuation
            if stage.fork:  # a nested fork runs to its own clear, then the branch continues
                cleared = await self.run_collect(stage, input, concerns=ctx.concerns)
                self._h._fold_sticky(input, cleared)  # nested-fork join: mirror _drive
                input = cleared
                stage_name = self._h._next_stage(stage, cleared)
                continue
            result = await self._h._exec_stage(stage, input)
            if isinstance(result, _Cleared):
                return None  # cancelled in-flight — the branch deposits nothing
            if isinstance(result, _Suspend):
                raise RuntimeError(
                    "stage {!r} suspended inside a fork branch — gates inside a fork "
                    "are unsupported in v1; keep branches gateless".format(stage.name))
            ctx.concerns.extend(result.concerns)  # soft concerns survive the fork
            # Re-fold graph.sticky between branch stages, exactly as harness._drive
            # does on the linear path (theme B: one run-stage seam, no drift). Without
            # this a payload-replacing branch stage silently dropped a sticky key a
            # later branch stage needed — the chain worked linearly but lost data in a
            # branch (contradicting Graph.sticky's "after every passing stage"). The
            # helper is fill-if-missing, so a stage that SET the key still wins.
            self._h._fold_sticky(input, result.output)
            input = result.output
            if stage.clears:  # a branch node can clear named gate(s) too
                await self._bus.publish_clears(
                    stage.clears, input.correlation_id, input.payload)
            stage_name = self._h._next_stage(stage, result.output)
        return input

    async def _fanin_arrive(self, stage: Stage, input: Envelope, branch_id: str,
                            ctx: _ForkCtx) -> None:
        """Record one branch's arrival at a fan-in. The first arrival starts the
        single coordinator (it owns the timeout + the one continuation); each arrival
        sets the completion event once the wait policy over the expected set is met."""
        join = ctx.joins.get(stage.name)
        if join is None:
            node_id = stage.id or stage.name
            join = {"arrived": set(), "event": asyncio.Event(),
                    "addr": "{}:{}".format(node_id, input.correlation_id),
                    "corr": input.correlation_id,
                    "clear_id": input.headers.get("clear_id")}
            ctx.joins[stage.name] = join
            ctx.tasks.append(asyncio.ensure_future(
                self._fanin_coordinator(stage, join, ctx)))
        # PARK the arriving envelope through the one EnvelopeStore utility (memory
        # default = behaviour-neutral; a durable StoreBackend makes it survive + inspectable
        # + flushable). `arrived` (in-memory) tracks WHICH branches came, for policy.
        await self._envelopes.save("{}:{}".format(join["addr"], branch_id), input)
        join["arrived"].add(branch_id)
        if self._policy_met(stage.fanin or {}, join["arrived"]):
            join["event"].set()

    async def _fanin_coordinator(self, stage: Stage, join: dict, ctx: _ForkCtx) -> None:
        """Own one fan-in: wait (up to `timeout`) for the policy to be met, then
        reduce the arrived branches and continue down `then` ONCE. On timeout, publish
        an error to the `on_timeout` listener and do not continue."""
        cfg = stage.fanin or {}
        timeout = cfg.get("timeout")
        try:
            if timeout is not None:
                await asyncio.wait_for(join["event"].wait(), timeout)
            else:
                await join["event"].wait()
        except asyncio.TimeoutError:
            # THIS JOIN's own timeout, which may be SHORTER than the fork's
            # wait.timeout (review MED-3): the fork then still has TTL left to
            # burn, reaches _degraded_output long after this flush, and would
            # find nothing. `_release_join` snapshots first, so a fan-in that
            # gives up early still hands its arrivals to the fork's degrade.
            await self._publish_join_error(stage, join)
            await self._release_join(join)          # release the parked arrivals
            return
        if join.get("doomed"):  # released as UNMEETABLE (every branch settled,
            # policy unmet — H2): exit the same way the timeout path does, so the
            # drain/wait above can finish instead of this coordinator waiting forever
            await self._publish_join_error(stage, join)
            await self._release_join(join)
            return
        # gather the PARKED arrivals (branch -> payload) from the EnvelopeStore and reduce
        parked = await self._envelopes.list(join["addr"] + ":")
        arrived = {key.rsplit(":", 1)[-1]: env.payload for key, env in parked}
        try:
            reduced = await self._reduce(stage, arrived)
        except Exception as e:
            # A broken `reduce` target used to vanish: this coordinator runs as a
            # background task whose exception _drain retrieved-and-DISCARDED, so the
            # fork saw only a MISSING clear and hung (or timed out misleadingly).
            # Surface it as an explicit join error + error span instead of a silent hang.
            # Snapshot-then-flush like the other non-continuing exits: the fork
            # above will sit out its TTL and degrade, and the arms that DID land
            # are the only evidence that run has.
            await self._error_span(stage.name, join.get("corr"),
                                   "fanin_reduce_failed: " + repr(e))
            await self._publish_join_error(stage, join)
            await self._release_join(join)
            return
        payload = dict(reduced) if isinstance(reduced, dict) else {"result": reduced}
        corr = join.get("corr") or ""
        topic = (stage.fanin or {}).get("clear_topic", "clear")
        # CLEAR: explicit `clears` targets if configured, else echo the fork's
        # own address (the automatic fork->fan-in pair).
        if stage.clears:
            await self._bus.publish_clears(stage.clears, corr, payload, topic)
        elif join.get("clear_id"):
            await self._bus.publish_clear(join["clear_id"], corr, payload, topic)
        # decoupled path: if the fan-in has its OWN `then`, run it and capture result
        if stage.then is not None:
            ch = {"correlation_id": corr}
            if join.get("clear_id"):
                ch["clear_id"] = join["clear_id"]
            term = await self._walk(stage.then,
                                    Envelope(Kind.RESULT, dict(payload), dict(ch)),
                                    branch_id=stage.name, ctx=ctx)
            if term is not None:
                # FIRST-WINS (assessment cluster 1 MED — fan-in terminal race).
                # Nested forks share `ctx`, so two fan-ins can both produce a
                # terminal envelope. Last-writer-wins silently dropped the first;
                # we preserve the FIRST and trace the loss so misuse is observable.
                if ctx.result is None:
                    ctx.result = term
                else:
                    await self._error_span(
                        stage.name, corr,
                        "fanin_terminal_race: dropping second terminal "
                        "(first preserved)")
        await self._envelopes.flush(join["addr"] + ":")  # release this join's parked set

    @staticmethod
    def _policy_met(cfg: dict, arrived) -> bool:
        """Has the fan-in's wait policy been satisfied over its defined input set?
        `expect` = a list of branch ids, or {"count": n}, or omitted (= whatever
        arrives). `wait` = "all" | "any" | <n>."""
        expect = cfg.get("expect")
        wait = cfg.get("wait", "all")
        if isinstance(expect, dict):
            have, need = len(arrived), int(expect.get("count", 1))
        elif isinstance(expect, list):
            have, need = len(set(arrived) & set(expect)), len(expect)
        else:
            have = need = len(arrived)
        if wait == "any":
            return have >= 1
        if wait != "all":
            try:
                return have >= int(wait)
            except (TypeError, ValueError):
                pass
        return have >= need

    async def _reduce(self, stage: Stage, arrived: dict):
        """Combine the arrived branch payloads. Default = generic JSON append; an
        app `reduce` target (fn:/node:/http:) overrides via the shared call_target
        seam, so the engine never learns the data shape."""
        red = (stage.fanin or {}).get("reduce")
        if red:
            return await call_target(red, arrived, comms=self._comms)
        return default_reduce(arrived)

    async def _publish_join_error(self, stage: Stage, join: dict) -> None:
        """Propagate a fan-in timeout to a listener (a Comms topic). No-op if no
        `on_timeout` topic is configured."""
        topic = (stage.fanin or {}).get("on_timeout")
        if not topic:
            return
        err = Envelope(Kind.ERROR, {
            "join": stage.name, "reason": "timeout",
            "expected": (stage.fanin or {}).get("expect"),
            "arrived": sorted(join["arrived"]),
        })
        await self._comms.publish(topic, err)

    async def _publish_wait_timeout(self, stage: Stage, wait: dict, *,
                                    reason: str = "wait_timeout",
                                    detail: Optional[str] = None) -> None:
        """Propagate a FORK's wait-degrade to a listener (a Comms topic). `reason`
        says WHY the fork degraded — "wait_timeout" (the liveness TTL elapsed),
        "branch_failed" (an arm died; `detail` carries its error), or
        "fanin_unmeetable" (every arm settled, the join policy can't be met) —
        so a listener can route dead-arm degrades differently from slow-arm ones
        (M9b). No-op if no `on_timeout` is configured; the fork then proceeds
        with whatever cleared so far."""
        topic = wait.get("on_timeout")
        if not topic:
            return
        payload = {"fork": stage.name, "reason": reason}
        if detail is not None:
            payload["detail"] = detail
        await self._comms.publish(topic, Envelope(Kind.ERROR, payload))
