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
from typing import Optional

from ..core import Envelope, Failure, Kind, Verdict
from ..external_call import call_target
from ..trace import Span
from .reduce import default_reduce
from .stage import Stage
from .stage_failed import StageFailed


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

    def __init__(self, harness: object, *, comms: object, clear_bus: object,
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
                    await self._tracer.emit(Span.timed(
                        "stage", corr=input.correlation_id, parent=stage.name,
                        t0=self._clock(), t1=self._clock(), status="error",
                        attrs={"stage": stage.name, "error": "fork_branch_failed: " + repr(extra)}))
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
                cleared_env = await asyncio.wait_for(fut, timeout)
            else:  # H2: unbounded wait watches the branches too — see _await_clear
                cleared_env = await self._await_clear(fut, ctx, stage)
            await self._drain(ctx)  # branches/coordinator settle
            # reply_with off `input` keeps correlation_id (trace) + any OUTER clear_id
            cleared = input.reply_with(Kind.RESULT, dict(cleared_env.payload))
        except asyncio.TimeoutError:
            await self._publish_wait_timeout(stage, wait)
            for t in ctx.tasks:  # abandon outstanding branches
                t.cancel()
            await asyncio.gather(*ctx.tasks, return_exceptions=True)  # retrieve, no warnings
            cleared = input
        finally:
            sub.cancel()
        return cleared

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
        while not fut.done():
            pending = [t for t in ctx.tasks if not t.done()]
            if not pending:
                break
            self._release_unmeetable_joins(ctx)
            await asyncio.wait([fut, *pending], return_when=asyncio.FIRST_COMPLETED)
        if fut.done():
            return fut.result()
        for t in ctx.tasks:  # surface the FIRST branch failure as the cause
            if not t.cancelled() and t.exception() is not None:
                raise t.exception()
        if any(j.get("doomed") for j in ctx.joins.values()):
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
        # default = behaviour-neutral; a durable Store makes it survive + inspectable
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
            await self._publish_join_error(stage, join)
            await self._envelopes.flush(join["addr"] + ":")  # release the parked arrivals
            return
        if join.get("doomed"):  # released as UNMEETABLE (every branch settled,
            # policy unmet — H2): exit the same way the timeout path does, so the
            # drain/wait above can finish instead of this coordinator waiting forever
            await self._publish_join_error(stage, join)
            await self._envelopes.flush(join["addr"] + ":")
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
            await self._tracer.emit(Span.timed(
                "stage", corr=join.get("corr") or "", parent=stage.name,
                t0=self._clock(), t1=self._clock(), status="error",
                attrs={"stage": stage.name, "error": "fanin_reduce_failed: " + repr(e)}))
            await self._publish_join_error(stage, join)
            await self._envelopes.flush(join["addr"] + ":")
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
                    await self._tracer.emit(Span.timed(
                        "stage", corr=corr, parent=stage.name,
                        t0=self._clock(), t1=self._clock(),
                        status="error",
                        attrs={"stage": stage.name,
                               "error": "fanin_terminal_race: dropping second "
                                        "terminal (first preserved)"}))
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

    async def _publish_wait_timeout(self, stage: Stage, wait: dict) -> None:
        """Propagate a FORK's wait-for-clear timeout to a listener (a Comms topic).
        No-op if no `on_timeout` is configured; the fork then proceeds with whatever
        cleared so far."""
        topic = wait.get("on_timeout")
        if not topic:
            return
        await self._comms.publish(topic, Envelope(Kind.ERROR, {
            "fork": stage.name, "reason": "wait_timeout"}))
