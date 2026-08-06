"""Liveness lease — telling a CRASHED run from one still live in another process.

Every checkpoint write stamps `owner` (`<host>/<pid>/<nonce>`) and `leased_at` on
the baton, so `resume_running` can probe instead of asking the operator to assert
death. Three tiers (LeaseState): no owner -> allow with a note; same host -> a real
`os.kill(pid, 0)` probe; foreign host -> the lease AGE against `lease_horizon`.
The decision is then CLAIMED with a compare-and-set, so two operators racing the
same recovery cannot both win.

Covers: a dead pid on this host resumes; our OWN live pid refuses; `--force`
overrides that refusal loudly; a foreign host inside the horizon refuses and
outside it resumes; a park CLEARS the lease (a parked gate has no owning process);
a lost CAS claim on FileBackend across two event loops refuses naming the winner;
the `yaah list --json` lease labels; and the `yaah list` PROSE line, where the
`resume-run` hint must be suppressed for a live lease.

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
from yaah.harness.lease_state import FOREIGN, LIVE, NONE, STALE, mint_owner
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

    # THE ROOT PATH. `_lease_kw` passes each knob only when the root actually set it,
    # so an unset key leaves the Harness default as the single place it is written
    # down — and `build()` carries it through to the Harness.
    from yaah.build.build import _lease_kw, build
    assert _lease_kw(None, None) == {}, "unset root keys must pass NOTHING"
    assert _lease_kw(60, "node-7") == {"lease_horizon": 60.0, "lease_host": "node-7"}
    assert _lease_kw(None, "node-7") == {"lease_host": "node-7"}
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
