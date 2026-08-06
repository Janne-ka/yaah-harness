"""Liveness lease — telling a CRASHED run from one still live in another process.

Every checkpoint write stamps `owner` (`<host>/<pid>/<nonce>`) and `leased_at` on
the baton, so `resume_running` can probe instead of asking the operator to assert
death. The tiers (LeaseState): our OWN lease -> always ours to take back; no owner
-> allow with a note; same host -> a real `os.kill(pid, 0)` probe; foreign host ->
the lease AGE against `lease_horizon`. The decision is then CLAIMED with a
compare-and-set, so two operators racing the same recovery cannot both win — and
the same claim now fences a gate `resume`.

Covers: a dead pid on this host resumes; our OWN live pid refuses; `--force`
overrides that refusal loudly; a FAILED recovery releases the lease it took and
stays recoverable by the SAME HARNESS (tier 0 — which is per-harness-object, not
per-process: a sibling harness here reads LIVE, and the release arm is what covers
it); a second resume of one parked gate loses the claim and never drives the
post-gate stage; the gate claim's write is a clean, complete running checkpoint, so
a crash right after it recovers with the decision already merged; a foreign host
inside the horizon refuses and outside it resumes; a park CLEARS the lease (a
parked gate has no owning process); a lost CAS claim on FileBackend across two
event loops refuses naming the winner; the `yaah list --json` lease labels; and the
`yaah list` PROSE line, where the `resume-run` hint must be suppressed for a live
lease.

Mirrors tests/test_checkpoint_resume.py's kill/re-drive harness (its `Step`,
`KillOnce`, `Gate` and `dead_pid_owner` are imported rather than re-typed).

Run: cd yaah && PYTHONPATH=src:tests python3 tests/test_lease.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import socket
import tempfile

from yaah import Done, Envelope, Graph, Harness, InProcessComms, Stage, Suspended
from yaah.adapters.stores import FileBackend
from yaah.harness import BatonStore, LeaseState
from yaah.harness.lease_state import FOREIGN, LIVE, NONE, SELF, STALE, mint_owner
from yaah.runtime import _baton_json
from yaah.store import MemoryBackend

from test_checkpoint_resume import Gate, InfraError, KillOnce, Step, dead_pid_owner

HOST = socket.gethostname()


def _graph() -> Graph:
    return Graph.of(Stage("a", node="role:a", then="b"),
                    Stage("b", node="role:b", then="c"),
                    Stage("c", node="role:c"))


def _comms() -> InProcessComms:
    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:b", KillOnce("b"))
    comms.register("role:c", Step("c"))
    return comms


async def _killed(store, owner=None, **kw):
    """Drive a run until the kill in stage `b`; return (checkpoint, comms)."""
    comms = _comms()
    h = Harness(comms, _graph(), baton_store=BatonStore(store), owner=owner, **kw)
    try:
        await h.run(Envelope("task", {"steps": []}))
    except InfraError:
        pass
    running = await BatonStore(store).list_running()
    assert len(running) == 1, running
    return running[0], comms


def scenario_tiers_are_decided_from_the_owner_string() -> None:
    """The tier table itself, driven with injected host/probe so every branch is
    reachable without spawning processes."""
    class B:
        def __init__(self, owner, leased_at):
            self.owner, self.leased_at = owner, leased_at

    dead = LeaseState.of(B("h1/7/aa", 100.0), 200.0, 3600.0,
                         host="h1", alive=lambda pid: False)
    assert dead.state == STALE and dead.recoverable, dead
    assert "7" in dead.detail and "gone" in dead.detail, dead

    live = LeaseState.of(B("h1/7/aa", 100.0), 200.0, 3600.0,
                         host="h1", alive=lambda pid: True)
    assert live.state == LIVE and not live.recoverable, live
    assert live.label() == "live", live.label()

    inside = LeaseState.of(B("h2/7/aa", 100.0), 200.0, 3600.0, host="h1")
    assert inside.state == FOREIGN and not inside.recoverable, inside
    assert inside.label().startswith("foreign("), inside.label()

    outside = LeaseState.of(B("h2/7/aa", 0.0), 100000.0, 3600.0, host="h1")
    assert outside.state == STALE and outside.recoverable, outside
    assert outside.label().startswith("stale("), outside.label()
    assert "presumed dead" in outside.detail, outside

    none = LeaseState.of(B(None, None), 200.0, 3600.0, host="h1")
    assert none.state == NONE and none.recoverable, none
    assert "pre-upgrade" in none.detail, none
    assert none.label() == "none", none.label()

    assert mint_owner("h9").startswith("h9/{}/".format(os.getpid())), mint_owner("h9")
    print("PASS lease tiers: none/live/stale(dead pid)/foreign/stale(past horizon)")


def scenario_pid_probe_errno_arms() -> None:
    """`_pid_alive`'s three arms, which the tier table above bypasses (it injects
    `alive=` to reach every tier without real processes) — so the errno policy itself
    was comment-only. It is the whole basis of tier 2: EPERM means the pid EXISTS and
    belongs to another user (alive → refuse), an unknown OSError is answered alive on
    purpose (refuse is the safe direction), and ONLY ESRCH is death."""
    from yaah.harness import lease_state as ls

    def raising(exc):
        def kill(_pid, _sig):
            raise exc()
        return kill

    real = ls.os.kill
    try:
        ls.os.kill = raising(PermissionError)
        assert ls._pid_alive(1) is True, "EPERM: the pid exists, owned by another user"
        ls.os.kill = raising(OSError)
        assert ls._pid_alive(1) is True, "an unknown errno must fail toward REFUSE"
        ls.os.kill = raising(ProcessLookupError)
        assert ls._pid_alive(1) is False, "ESRCH is the only evidence of death"
    finally:
        ls.os.kill = real
    assert ls._pid_alive(os.getpid()) is True, "the real probe still sees this process"
    print("PASS the pid probe answers alive on EPERM and unknown errno, dead only on ESRCH")


async def scenario_dead_pid_on_this_host_resumes() -> None:
    store = MemoryBackend()
    cp, comms = await _killed(store, owner=dead_pid_owner())
    assert cp.owner is not None and cp.leased_at is not None, cp

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = await Harness(comms, _graph(), baton_store=BatonStore(store)
                            ).resume_running(cp.id)
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b", "c"], out.output.payload
    assert "is gone" in err.getvalue(), err.getvalue()
    print("PASS a dead pid on this host is recovered without --force")


async def scenario_own_live_pid_refuses_and_force_overrides() -> None:
    """The default owner names THIS process, which is provably alive — so recovery
    refuses. That is the whole point: on a shared store the record could belong to a
    healthy run, and re-driving it would double-execute every remaining stage."""
    store = MemoryBackend()
    cp, comms = await _killed(store)          # default owner = this live process
    assert cp.owner.startswith(HOST + "/" + str(os.getpid()) + "/"), cp.owner

    h2 = Harness(comms, _graph(), baton_store=BatonStore(store))
    refused = None
    try:
        await h2.resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None, "a live owner must refuse"
    assert "is LEASED" in str(refused), refused
    assert HOST in str(refused) and str(os.getpid()) in str(refused), refused
    assert "--force" in str(refused), refused

    # --force asserts the owner is dead anyway, and says so LOUDLY on stderr.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = await h2.resume_running(cp.id, force=True)
    assert isinstance(out, Done), out
    assert "--force overriding" in err.getvalue(), err.getvalue()
    assert cp.owner in err.getvalue(), err.getvalue()
    print("PASS our own LIVE pid refuses (naming host/pid); --force overrides, loudly")


class KillTwice(KillOnce):
    """Raises on the first TWO calls: the original kill, then AGAIN on the recovery
    re-drive. Models the failure path — a recovery that claims the lease and then
    blows up — that used to leave the record leased forever."""

    async def invoke(self, env, config):
        self.calls += 1
        if self.calls <= 2:
            raise InfraError("blew up at " + self.name)
        return env.reply("result", steps=list(env.payload.get("steps", [])) + [self.name])


async def scenario_a_failed_recovery_releases_and_stays_recoverable() -> None:
    """A recovery TAKES the lease and then the re-drive fails non-logically. The
    checkpoint is preserved on purpose (it is what the next recovery re-drives), so
    the lease must not be: in a long-lived embedded process the owning pid is still
    alive, and the record then reads LIVE forever — the harness refuses its own retry
    and the `--force` hint it prints names the process reading it.

    Two independent guarantees, and this pins both: the lease is RELEASED on the way
    out (so every OTHER reader sees the truth), and the tier-0 self-owner check makes
    the record ours to take back even if that release never happened."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:b", KillTwice("b"))
    comms.register("role:c", Step("c"))
    h = Harness(comms, _graph(), baton_store=BatonStore(store), owner=dead_pid_owner())
    try:
        await h.run(Envelope("task", {"steps": []}))
    except InfraError:
        pass
    cp = (await BatonStore(store).list_running())[0]

    h2 = Harness(comms, _graph(), baton_store=BatonStore(store))   # a LIVE owner (us)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            await h2.resume_running(cp.id)
        except InfraError:
            pass
    after = await BatonStore(store).load(cp.id)
    assert after is not None, "the checkpoint itself must survive the failure"
    assert after.owner is None and after.leased_at is None, \
        ("a failed recovery must not leave the record leased", after.owner)

    # ...and even with the stamp back on, OUR OWN lease never refuses US.
    after.owner, after.leased_at = h2._owner, after.checkpointed_at   # noqa: SLF001
    await BatonStore(store).save(after)
    lease = LeaseState.of(after, 0.0, self_owner=h2._owner)           # noqa: SLF001
    assert lease.state == SELF and lease.recoverable and lease.label() == "self", lease
    with contextlib.redirect_stderr(io.StringIO()):
        out = await h2.resume_running(cp.id)          # no --force needed
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b", "c"], out.output.payload
    print("PASS a failed recovery releases the lease, and our own lease never refuses us")


async def scenario_gate_resume_claims_the_baton() -> None:
    """A parked gate's resume had no claim at all: two operators answering the same
    gate both passed the `suspended` check and both drove the post-gate stage. It is
    now read-decide-CLAIM like `resume_running`, so the loser is refused."""
    with tempfile.TemporaryDirectory() as d:
        comms = InProcessComms()
        post_gate = Step("c")
        comms.register("role:gate", Gate())
        comms.register("role:c", post_gate)
        graph = Graph.of(Stage("gate", node="role:gate", then="c"),
                         Stage("c", node="role:c"))
        h = Harness(comms, graph, baton_store=BatonStore(FileBackend(d)))
        out = await h.run(Envelope("task", {"steps": []}))
        assert isinstance(out, Suspended), out

        # The other operator writes INSIDE our read-claim window (same technique as
        # the recovery race below: a claim against a superseded revision loses).
        real_load_rev = h.batons.load_rev

        async def racing_load_rev(bid):
            baton, at_rev = await real_load_rev(bid)
            interloper, cur = await real_load_rev(bid)
            interloper.owner = "other-host/1/00000000"
            assert await h.batons.claim(interloper, cur), "the interloper must win"
            return baton, at_rev

        h.batons.load_rev = racing_load_rev
        refused = None
        try:
            await h.resume(out.baton_id, Envelope("resume", {"human": "approve"}))
        except ValueError as e:
            refused = e
    assert refused is not None, "the second resume of one gate must be refused"
    assert "lost the claim" in str(refused), refused
    assert "other-host/1/00000000" in str(refused), refused
    assert "delivering a decision" in str(refused), refused
    assert post_gate.calls == 0, "the loser must not drive the post-gate stage"
    print("PASS a second resume of the same gate loses the claim and is refused")


def scenario_self_tier_is_per_harness_not_per_process() -> None:
    """Tier 0 matches the WHOLE owner id, per-harness nonce included — so two Harness
    objects in ONE process do not see each other's leases as their own. The second
    reads the first's stamp as tier 2 LIVE (same host, and the pid is this very
    process), which is the honest answer: it cannot know that harness is finished.
    What covers that case is the RELEASE arm on the failure path, not this tier."""
    h1 = Harness(_comms(), _graph(), baton_store=BatonStore(MemoryBackend()))
    h2 = Harness(_comms(), _graph(), baton_store=BatonStore(MemoryBackend()))
    assert h1._owner != h2._owner, "each Harness mints its own owner id"   # noqa: SLF001

    class B:
        def __init__(self, owner):
            self.owner, self.leased_at = owner, 100.0

    mine = LeaseState.of(B(h1._owner), 200.0, self_owner=h1._owner)        # noqa: SLF001
    assert mine.state == SELF and mine.recoverable, mine
    sibling = LeaseState.of(B(h1._owner), 200.0, self_owner=h2._owner)     # noqa: SLF001
    assert sibling.state == LIVE and not sibling.recoverable, sibling
    # ...and a RELEASED record (owner nulled by the failure path) is not claimed to be
    # a pre-upgrade one — the wording has to cover both, since they are identical.
    released = LeaseState.of(B(None), 200.0)
    assert released.state == NONE and released.recoverable, released
    assert "released it" in released.detail and "pre-upgrade" in released.detail, released
    print("PASS tier 0 is per-HARNESS: a sibling harness in the same process reads LIVE")


async def scenario_gate_claim_publishes_a_recoverable_checkpoint() -> None:
    """The gate claim's write IS the first post-gate checkpoint, not a bare status
    flip. Two things must hold of it: the record is valid on its own (no
    awaiting/parked_at/pending on a `running` baton — the debugging.md contract), and
    `resume_running` accepts it (cursor + the merged decision as its input).

    Written the other way round — claim, then checkpoint — a crash in between left a
    record NEITHER verb would take (`resume` refuses a non-suspended baton,
    `resume_running` refuses one with no cursor), losing a run that before the claim
    existed would simply have stayed parked."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:gate", Gate())
    comms.register("role:c", KillOnce("c"))     # dies on the FIRST post-gate call
    graph = Graph.of(Stage("gate", node="role:gate", then="c"),
                     Stage("c", node="role:c"))
    h = Harness(comms, graph, baton_store=BatonStore(store))
    parked = await h.run(Envelope("task", {"steps": []}))
    assert isinstance(parked, Suspended), parked

    claimed: dict = {}
    real_claim = h.batons.claim

    async def spy(baton, rev):
        claimed.update(baton.to_dict())        # the record exactly AS CLAIMED
        return await real_claim(baton, rev)

    h.batons.claim = spy
    try:
        await h.resume(parked.baton_id, Envelope("resume", {"human": "approve"}))
    except InfraError:
        pass
    assert claimed, "the gate resume must claim the record"
    assert claimed["status"] == "running" and claimed["stage"] == "c", claimed
    assert claimed["awaiting"] is None and claimed["parked_at"] is None, claimed
    assert claimed["pending"] is None, claimed
    assert claimed["cursor_input"] is not None, claimed
    assert claimed["cursor_input"]["payload"]["human"] == "approve", claimed

    after = await BatonStore(store).load(parked.baton_id)
    assert after.owner is None and after.leased_at is None, \
        ("the failed drive must release the lease it took", after.owner)
    assert await BatonStore(store).list_suspended() == [], "the gate was answered"
    assert len(await BatonStore(store).list_running()) == 1, "…and left a checkpoint"
    # THE DECISION IS DURABLE: it is the recovery's input, so the human is not asked
    # again. This is what the pre-claim ordering lost.
    assert after.cursor_input.payload["human"] == "approve", after.cursor_input.payload

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = await Harness(comms, graph, baton_store=BatonStore(store)
                            ).resume_running(parked.baton_id)
    assert isinstance(out, Done), out
    assert await BatonStore(store).list_suspended() == [], "the gate must not re-open"

    # THE TERMINAL GATE is deliberately NOT claimed: no post-gate stage to
    # double-drive, and no cursor to checkpoint — a claim there would buy nothing and
    # create the unrecoverable window this scenario exists to close.
    store2 = MemoryBackend()
    comms2 = InProcessComms()
    comms2.register("role:gate", Gate())
    last = Harness(comms2, Graph.of(Stage("gate", node="role:gate")),
                   baton_store=BatonStore(store2))
    parked2 = await last.run(Envelope("task", {}))
    claimed.clear()
    real_claim2 = last.batons.claim

    async def spy2(baton, rev):
        claimed.update(baton.to_dict())
        return await real_claim2(baton, rev)

    last.batons.claim = spy2
    done = await last.resume(parked2.baton_id, Envelope("resume", {"human": "approve"}))
    assert isinstance(done, Done), done
    assert not claimed, "a terminal gate must not be claimed"
    assert await BatonStore(store2).list_running() == [], "…and leaves no checkpoint"
    print("PASS the gate claim publishes a clean record a crash can recover from")


async def scenario_foreign_host_inside_and_outside_the_horizon() -> None:
    store = MemoryBackend()
    cp, comms = await _killed(store, owner="other-host/4242/abcd1234")

    # INSIDE the horizon: no probe is possible, and the lease is fresh -> refuse.
    tight = Harness(comms, _graph(), baton_store=BatonStore(store),
                    lease_horizon=3600.0)
    refused = None
    try:
        await tight.resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None and "other-host/4242/abcd1234" in str(refused), refused
    assert "cannot be probed" in str(refused), refused

    # OUTSIDE it: the lease has gone unrefreshed longer than the horizon allows, so
    # the owner is presumed dead and the run is recoverable.
    loose = Harness(comms, _graph(), baton_store=BatonStore(store),
                    lease_horizon=0.0)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = await loose.resume_running(cp.id)
    assert isinstance(out, Done), out
    assert "presumed dead" in err.getvalue(), err.getvalue()
    print("PASS a foreign host refuses inside lease_horizon, recovers past it")


async def scenario_lease_host_reaches_the_owner_and_the_tier() -> None:
    """Root `lease_host` — the CONTAINER escape. `gethostname()` in a container is the
    pod id and changes on every restart, so the same machine looks like a new host each
    time: tier 2's `kill(pid, 0)` is never reachable and every one of a deployment's own
    runs is decided by tier 3's coarse age guess. `lease_host` names the machine
    stably.

    Both halves are asserted, because one without the other is useless: the owner id
    must be MINTED with the name, and the recovery must PROBE against the same name.
    Absent, nothing changes — the default is still `gethostname()`."""
    store = MemoryBackend()
    cp, comms = await _killed(store, lease_host="node-7")
    assert cp.owner.startswith("node-7/" + str(os.getpid()) + "/"), cp.owner

    # A harness that agrees it is `node-7` probes THIS pid — alive — and refuses.
    same = Harness(comms, _graph(), baton_store=BatonStore(store), lease_host="node-7")
    refused = None
    try:
        await same.resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None, "the same lease_host must reach tier 2 and refuse"
    assert "on this host (node-7)" in str(refused), refused

    # A harness on a DIFFERENT name cannot probe it — tier 3, the age fallback.
    other = Harness(comms, _graph(), baton_store=BatonStore(store),
                    lease_host="node-9", lease_horizon=100000.0)
    refused = None
    try:
        await other.resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None and "cannot be probed" in str(refused), refused

    # ...and the default is untouched: no lease_host = gethostname().
    assert mint_owner().startswith(HOST + "/"), mint_owner()

    # THE ROOT PATH. Both knobs pass straight through `build()` to the Harness, and
    # an UNSET one stays None all the way into LeaseState — which owns the default,
    # so it is written down in exactly one place.
    from yaah.build.build import build
    unset = build({"nodes": {"role:t": {"type": "transform", "target": "fn:json:loads"}},
                   "graph": {"start": "s", "stages": {"s": {"node": "role:t"}}}})
    assert unset._lease_horizon is None, unset._lease_horizon    # noqa: SLF001
    assert unset._lease_host is None, unset._lease_host          # noqa: SLF001
    built = build({"nodes": {"role:t": {"type": "transform", "target": "fn:json:loads"}},
                   "graph": {"start": "s", "stages": {"s": {"node": "role:t"}}}},
                  lease_host="node-7", lease_horizon=60)
    assert built._lease_host == "node-7", built._lease_host      # noqa: SLF001
    assert built._owner.startswith("node-7/"), built._owner      # noqa: SLF001
    print("PASS root `lease_host` mints the owner and decides the tier; default unchanged")


async def scenario_park_clears_the_lease() -> None:
    """A parked gate has no owning process — the engine exits at the park. Leaving
    the lease stamped would make `yaah list` show a gate awaiting a human as owned
    by a pid that is long gone."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:gate", Gate())
    graph = Graph.of(Stage("a", node="role:a", then="gate"),
                     Stage("gate", node="role:gate"))
    h = Harness(comms, graph, baton_store=BatonStore(store))
    out = await h.run(Envelope("task", {"steps": []}))
    assert isinstance(out, Suspended), out

    parked = (await BatonStore(store).list_suspended())[0]
    assert parked.owner is None, "a parked gate must not carry an owner"
    assert parked.leased_at is None, "a parked gate must not carry a lease time"
    assert LeaseState.of(parked, 0.0).state == NONE
    print("PASS a gate park clears owner/leased_at (a parked gate has no owner)")


def scenario_cas_claim_conflict_on_filebackend() -> None:
    """Two recoveries racing the same checkpoint on a DURABLE store: the second reads
    at revision N, the first claims and bumps it, so the second's CAS fails and it
    refuses instead of double-driving. Each side runs on its OWN event loop (separate
    `asyncio.run`), which is as close to two processes as one test gets.

    Two things this scenario has to arrange, both learned the hard way — an earlier
    version of it could not fail, and `resume_running`'s "lost the claim" branch was
    uncovered suite-wide:

    1. THE LEASE MUST NOT BE WHAT REFUSES. `resume_running` checks the lease BEFORE it
       claims. A winner stamped with a fresh `leased_at` on a foreign host is `foreign`
       (inside `lease_horizon`), so the call was refused one tier early and the CAS
       never ran — the assertion passed on the wrong refusal. The winner's lease is
       therefore ANCIENT (`leased_at = 0.0`), which makes the tier `stale` and lets the
       call reach the claim.
    2. THE INTERLOPER MUST WRITE INSIDE THE READ-CLAIM WINDOW. `resume_running` does its
       OWN `load_rev`, so a write that landed BEFORE it is simply the revision it reads
       at, and its claim would succeed. `racing_load_rev` is that window made
       deterministic: hand back the record and the revision it was read at, then let a
       second operator write, bumping the revision underneath.
    """
    with tempfile.TemporaryDirectory() as d:
        async def seed():
            store = FileBackend(d)
            cp, _ = await _killed(store, owner=dead_pid_owner())
            return cp.id
        baton_id = asyncio.run(seed())

        async def race():
            store = FileBackend(d)
            h = Harness(_comms(), _graph(), baton_store=BatonStore(store))
            # --- the PRIMITIVE: two claims against the same revision, one wins ---
            stale_baton, stale_rev = await h.batons.load_rev(baton_id)
            assert stale_rev is not None, "FileBackend must expose revisions"
            winner, rev = await h.batons.load_rev(baton_id)
            winner.owner = "winner-host/1/00000000"
            winner.leased_at = 0.0        # see (1): past any horizon => tier `stale`
            assert await h.batons.claim(winner, rev), "the first claim must win"
            # The loser's claim is against the revision it read -> conflict.
            stale_baton.owner = "loser-host/2/00000000"
            assert not await h.batons.claim(stale_baton, stale_rev), \
                "a claim against a superseded revision must LOSE"
            assert h.batons.has_cas(), "FileBackend provides the +CAS tier"

            # --- the FULL PATH: resume_running loses its own claim and refuses ---
            real_load_rev = h.batons.load_rev

            async def racing_load_rev(bid):
                baton, at_rev = await real_load_rev(bid)
                interloper, cur = await real_load_rev(bid)   # see (2)
                interloper.owner = "winner-host/1/00000000"
                interloper.leased_at = 0.0
                assert await h.batons.claim(interloper, cur), "the interloper must win"
                return baton, at_rev

            h.batons.load_rev = racing_load_rev
            refused = None
            try:
                await h.resume_running(baton_id)
            except ValueError as e:
                refused = e
            return refused

        refused = asyncio.run(race())
        assert refused is not None, "a lost claim must refuse"
        # NO `or` fallback: this scenario exists for the CLAIM refusal specifically.
        # Accepting "is LEASED" too is what let it pass while never reaching the CAS.
        assert "lost the claim" in str(refused), refused
        assert "winner-host/1/00000000" in str(refused), refused
        assert "Do NOT retry blindly" in str(refused), refused
    print("PASS a lost CAS claim on FileBackend refuses, naming the current owner")


async def scenario_claim_is_advisory_without_cas() -> None:
    """A backend WITHOUT the +CAS tier still works — the claim falls back to a plain
    put — but the refusal says so, because "your store cannot prove this" is an
    operator fact, not an engine detail to hide."""
    class ScanOnly:                       # core + scan, no cas/get_rev
        def __init__(self):
            self._d = {}

        async def get(self, key):
            return self._d.get(key)

        async def put(self, key, value, *, ttl=None):
            self._d[key] = value

        async def delete(self, key):
            self._d.pop(key, None)

        async def scan(self, prefix):
            for k, v in list(self._d.items()):
                if k.startswith(prefix):
                    yield k, v

    store = ScanOnly()
    bs = BatonStore(store)
    assert not bs.has_cas()
    cp, comms = await _killed(store)
    loaded, rev = await bs.load_rev(cp.id)
    assert rev is None and loaded is not None, (rev, loaded)
    assert await bs.claim(loaded, None), "a non-CAS backend falls back to put"

    refused = None
    try:
        await Harness(comms, _graph(), baton_store=BatonStore(store)).resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None and "ADVISORY" in str(refused), refused
    print("PASS a non-CAS backend still claims (advisory) and the refusal says so")


def scenario_list_json_labels_the_lease() -> None:
    from yaah.harness.baton import Baton

    running = Baton(id="r", stage="b", status="running",
                    cursor_input=Envelope("result", {}), checkpointed_at=100.0,
                    owner="{}/{}/abcd1234".format(HOST, os.getpid()),
                    leased_at=100.0)
    j = _baton_json(running, {"lease_horizon": 3600})
    assert j["owner"] == running.owner and j["leased_at"] == 100.0, j
    assert j["lease_state"] == LIVE, j

    orphan = Baton(id="o", stage="b", status="running",
                   cursor_input=Envelope("result", {}), checkpointed_at=0.0,
                   owner="gone-host/1/abcd1234", leased_at=0.0)
    assert _baton_json(orphan, {"lease_horizon": 1})["lease_state"] == STALE
    assert _baton_json(orphan, {"lease_horizon": 10 ** 12})["lease_state"] == FOREIGN

    gate = Baton(id="g", stage="review", status="suspended", parked_at=1.0)
    assert _baton_json(gate, {})["lease_state"] == NONE, "a parked gate is unowned"

    # The LABEL honours root `lease_host` too — a container that set it must see its
    # own runs as `live`, not `foreign`. If only the recovery path read the key, the
    # operator's board and the engine's decision would disagree.
    pod = Baton(id="p", stage="b", status="running",
                cursor_input=Envelope("result", {}), checkpointed_at=100.0,
                owner="node-7/{}/abcd1234".format(os.getpid()), leased_at=100.0)
    assert _baton_json(pod, {"lease_host": "node-7"})["lease_state"] == LIVE
    assert _baton_json(pod, {"lease_horizon": 10 ** 12})["lease_state"] == FOREIGN
    print("PASS `yaah list --json` labels owner/leased_at/lease_state per tier")


def scenario_list_prose_shows_the_lease_and_gates_the_hint() -> None:
    """The OPERATOR-facing half: each RUNNING line carries `owner=`/`lease=`, and the
    `resume-run` hint appears ONLY when the lease is not live. An unconditional hint
    next to a healthy run is an invitation to double-drive it — which is the exact
    accident the lease exists to prevent."""
    from yaah.cli import _dispatch_list
    from yaah.harness.baton import Baton

    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "state")
        with open(os.path.join(d, "pipeline.json"), "w") as f:
            json.dump({"nodes": {"role:a": {"type": "transform", "target": "fn:json:loads"}},
                       "graph": {"start": "a", "stages": {"a": {"node": "role:a"}}}}, f)
        root = {"state": {"type": "file", "dir": state}, "pipeline": "pipeline.json"}

        async def seed():
            bs = BatonStore(FileBackend(state))
            for bid, owner, wiring in (
                    ("dead-1", dead_pid_owner(), None),
                    ("live-1", "{}/{}/aaaaaaaa".format(HOST, os.getpid()), None),
                    ("far-1", "other-host/9/bbbbbbbb", "a-stale-hash")):
                await bs.save(Baton(id=bid, stage="a", status="running",
                                    cursor_input=Envelope("result", {}),
                                    checkpointed_at=1.0, owner=owner, leased_at=1.0,
                                    wiring=wiring))
        asyncio.run(seed())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _dispatch_list({"root": "root.json"}, root, d)
        text = out.getvalue()

    lines = {ln.split("baton_id=")[1].split()[0]: ln
             for ln in text.splitlines() if ln.startswith("RUNNING")}
    assert set(lines) == {"dead-1", "live-1", "far-1"}, lines
    assert "lease=live" in lines["live-1"], lines["live-1"]
    assert "lease=stale(" in lines["dead-1"], lines["dead-1"]
    assert "owner={}/{}".format(HOST, os.getpid()) in lines["live-1"], lines["live-1"]
    assert "REWIRED" in lines["far-1"], lines["far-1"]
    assert "REWIRED" not in lines["dead-1"], lines["dead-1"]

    body = text.split("RUNNING")
    live_block = next(b for b in body if b.startswith(" baton_id=live-1"))
    assert "do NOT resume-run" in live_block, live_block
    assert "yaah resume-run" not in live_block, "a LIVE run must not be offered for recovery"
    dead_block = next(b for b in body if b.startswith(" baton_id=dead-1"))
    assert "yaah resume-run root.json dead-1" in dead_block, dead_block
    far_block = next(b for b in body if b.startswith(" baton_id=far-1"))
    assert "--allow-rewiring" in far_block, "a rewired checkpoint must hint the flag"
    print("PASS `yaah list` prose: owner/lease per line, resume-run hint only when not live")


def main() -> None:
    """Sync driver: each async scenario gets its OWN event loop via asyncio.run —
    which is also what makes the CAS-conflict scenario expressible, since it needs
    to drive two separate loops itself."""
    scenario_tiers_are_decided_from_the_owner_string()
    scenario_pid_probe_errno_arms()
    asyncio.run(scenario_dead_pid_on_this_host_resumes())
    asyncio.run(scenario_own_live_pid_refuses_and_force_overrides())
    asyncio.run(scenario_a_failed_recovery_releases_and_stays_recoverable())
    asyncio.run(scenario_gate_resume_claims_the_baton())
    asyncio.run(scenario_gate_claim_publishes_a_recoverable_checkpoint())
    scenario_self_tier_is_per_harness_not_per_process()
    asyncio.run(scenario_foreign_host_inside_and_outside_the_horizon())
    asyncio.run(scenario_lease_host_reaches_the_owner_and_the_tier())
    asyncio.run(scenario_park_clears_the_lease())
    scenario_cas_claim_conflict_on_filebackend()
    asyncio.run(scenario_claim_is_advisory_without_cas())
    scenario_list_json_labels_the_lease()
    scenario_list_prose_shows_the_lease_and_gates_the_hint()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
