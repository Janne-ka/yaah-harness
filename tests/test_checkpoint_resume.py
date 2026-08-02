"""Level 2 checkpoint durability — a run KILLED mid-flight is recoverable.

The engine persists a running checkpoint (the baton + its inter-stage input) after
each completed stage; `Harness.resume_running` re-drives a killed run from the
in-flight stage. These scenarios simulate a kill by having a stage raise a
BaseException (an infra blip / SIGKILL shape that `_settle` preserves rather than
evicts), snapshot the store, then drive a SECOND harness over the SAME store.

Covers: earlier stages are NOT re-executed and the in-flight stage IS; the run
completes with the correct final output; the checkpoint is deleted on completion
and on a gate park; resume_running refuses a non-running / unknown baton; a
checkpoint-write failure does not fail the run but warns ONCE (F9); a gate resume
clears awaiting/parked_at so the next checkpoint is clean (F5); clear() drops
running checkpoints (F2); the TTL sweep reclaims an abandoned running checkpoint.
See docs/durable-state.md §5.

Run: cd yaah && PYTHONPATH=src python3 tests/test_checkpoint_resume.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io

from yaah import Done, Envelope, Graph, Harness, InProcessComms, Stage, Suspended
from yaah.harness import Baton, BatonStore
from yaah.store import MemoryBackend


class InfraError(BaseException):
    """A non-`Exception` fault — the SIGKILL / transport-blip shape that bypasses
    the in-proc Exception→verdict convergence and reaches `_settle`, which
    PRESERVES the (checkpointed) baton instead of evicting it."""


class Step:
    """Appends its name to a running `steps` list, counting invocations so a test
    can assert which stages re-ran."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def invoke(self, env, config):
        self.calls += 1
        return env.reply("result", steps=list(env.payload.get("steps", [])) + [self.name])


class KillOnce:
    """Raises InfraError on its FIRST call (the kill), then behaves like Step —
    modelling the in-flight stage that was interrupted and re-runs cleanly on
    recovery."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def invoke(self, env, config):
        self.calls += 1
        if self.calls == 1:
            raise InfraError("killed mid-run at " + self.name)
        return env.reply("result", steps=list(env.payload.get("steps", [])) + [self.name])


class Gate:
    async def invoke(self, env, config):
        return env.reply("await", awaiting="human")


def _linear_graph() -> Graph:
    return Graph.of(
        Stage("a", node="role:a", then="b"),
        Stage("b", node="role:b", then="c"),
        Stage("c", node="role:c", then="d"),
        Stage("d", node="role:d"),
    )


def _harness(store, nodes) -> Harness:
    comms = InProcessComms()
    for role, node in nodes.items():
        comms.register(role, node)
    return Harness(comms, _linear_graph(), baton_store=BatonStore(store))


async def scenario_kill_then_recover() -> None:
    store = MemoryBackend()
    a, b, c, d = Step("a"), Step("b"), KillOnce("c"), Step("d")
    nodes = {"role:a": a, "role:b": b, "role:c": c, "role:d": d}

    # First run: A, B complete (each checkpointed), C is killed mid-flight.
    killed = None
    try:
        await _harness(store, nodes).run(Envelope("task", {"steps": []}))
    except BaseException as e:  # noqa: BLE001 — the simulated kill
        killed = e
    assert isinstance(killed, InfraError), killed
    assert (a.calls, b.calls, c.calls, d.calls) == (1, 1, 1, 0), \
        (a.calls, b.calls, c.calls, d.calls)

    # The store holds exactly one running checkpoint, cursored at the in-flight stage.
    running = await BatonStore(store).list_running()
    assert len(running) == 1, running
    cp = running[0]
    assert cp.stage == "c", cp.stage
    assert cp.cursor_input is not None and cp.cursor_input.payload["steps"] == ["a", "b"], cp
    assert cp.checkpointed_at is not None

    # A SECOND harness over the SAME store recovers it (same node instances so the
    # call counters carry across the "restart").
    h2 = _harness(store, nodes)
    out = await h2.resume_running(cp.id)
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b", "c", "d"], out.output.payload

    # Earlier stages NOT re-executed; the in-flight stage IS; the rest run once.
    assert (a.calls, b.calls, c.calls, d.calls) == (1, 1, 2, 1), \
        (a.calls, b.calls, c.calls, d.calls)

    # Checkpoint deleted on completion.
    assert await BatonStore(store).load(cp.id) is None
    assert await BatonStore(store).list_running() == []
    print("PASS kill mid-run → resume_running re-runs only the in-flight stage, completes")


async def scenario_checkpoint_cleared_on_gate_park() -> None:
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:gate", Gate())
    graph = Graph.of(Stage("a", node="role:a", then="gate"),
                     Stage("gate", node="role:gate"))
    h = Harness(comms, graph, baton_store=BatonStore(store))

    out = await h.run(Envelope("task", {"steps": []}))
    assert isinstance(out, Suspended), out

    # The park transitions the record running → suspended: it is a gate now, NOT a
    # resumable running checkpoint.
    assert await BatonStore(store).list_running() == []
    suspended = await BatonStore(store).list_suspended()
    assert len(suspended) == 1 and suspended[0].id == out.baton_id, suspended
    assert suspended[0].cursor_input is None, "a parked baton must not carry a running cursor"
    print("PASS gate park clears the running checkpoint (record becomes a suspended gate)")


async def scenario_refuse_non_running() -> None:
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:gate", Gate())
    graph = Graph.of(Stage("a", node="role:a", then="gate"),
                     Stage("gate", node="role:gate"))
    h = Harness(comms, graph, baton_store=BatonStore(store))
    out = await h.run(Envelope("task", {"steps": []}))

    # A suspended gate is not a running checkpoint — resume_running refuses it.
    refused = None
    try:
        await h.resume_running(out.baton_id)
    except ValueError as e:
        refused = e
    assert refused is not None and "not 'running'" in str(refused), refused

    # An unknown baton raises KeyError (same convention as resume()).
    missing = None
    try:
        await h.resume_running("does-not-exist")
    except KeyError as e:
        missing = e
    assert missing is not None, "unknown baton must raise"
    print("PASS resume_running refuses a suspended gate and an unknown baton")


async def scenario_checkpoint_write_failure_tolerated() -> None:
    store = MemoryBackend()
    a, b, c, d = Step("a"), Step("b"), Step("c"), Step("d")
    h = _harness(store, {"role:a": a, "role:b": b, "role:c": c, "role:d": d})

    # Every checkpoint write blows up — durability is best-effort, so the run must
    # still complete (Level 1 park remains the authoritative durability point).
    async def boom_save(_baton):
        raise RuntimeError("simulated store write failure")

    h.batons.save = boom_save

    # F9: the swallowed failure must not be SILENT — the first one warns on stderr
    # (crash recovery is off for the rest of the run), and only the first, since
    # three checkpoints fail here.
    warnings = io.StringIO()
    with contextlib.redirect_stderr(warnings):
        out = await h.run(Envelope("task", {"steps": []}))
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b", "c", "d"], out.output.payload
    err = warnings.getvalue()
    assert err.count("checkpoint write failed") == 1, err
    assert "crash recovery is OFF" in err, err
    assert "simulated store write failure" in err, err
    print("PASS a checkpoint-write failure is swallowed (run completes) and warns ONCE")


async def scenario_resume_clears_gate_fields() -> None:
    """F5: after a gate resume the record keeps running — and its next checkpoint
    must not still advertise the gate. `awaiting`/`parked_at` are nulled alongside
    `pending`, so `list_running()` shows a clean running checkpoint (the contract
    docs/cookbook/debugging.md states)."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:gate", Gate())
    comms.register("role:b", Step("b"))
    comms.register("role:c", Step("c"))
    graph = Graph.of(Stage("gate", node="role:gate", then="b"),
                     Stage("b", node="role:b", then="c"),
                     Stage("c", node="role:c"))
    h = Harness(comms, graph, baton_store=BatonStore(store))

    out = await h.run(Envelope("task", {"steps": []}))
    assert isinstance(out, Suspended), out
    parked = (await BatonStore(store).list_suspended())[0]
    assert parked.awaiting == "human" and parked.parked_at is not None, parked

    # Resume, but kill the stage AFTER the first post-gate checkpoint so a running
    # checkpoint is left in the store to inspect.
    comms.register("role:c", KillOnce("c"))
    try:
        await h.resume(out.baton_id, Envelope("resume", {"human": "approve"}))
    except InfraError:
        pass
    running = await BatonStore(store).list_running()
    assert len(running) == 1, running
    cp = running[0]
    assert cp.awaiting is None, "resume left a stale `awaiting` on the running checkpoint"
    assert cp.parked_at is None, "resume left a stale `parked_at` on the running checkpoint"
    assert cp.pending is None, cp
    assert cp.checkpointed_at is not None, cp
    assert await BatonStore(store).list_suspended() == []
    print("PASS resume clears awaiting/parked_at — the next checkpoint is clean")


async def scenario_clear_drops_running_checkpoints() -> None:
    """F2: `Harness.clear()` is the graceful reset — it must drop Level 2 running
    checkpoints too, or a crashed run's record is undeletable except by wiping the
    store by hand. Counted separately from suspended gates so `batons_dropped`
    keeps its original meaning."""
    store = MemoryBackend()
    a, b, c, d = Step("a"), Step("b"), KillOnce("c"), Step("d")
    h = _harness(store, {"role:a": a, "role:b": b, "role:c": c, "role:d": d})
    try:
        await h.run(Envelope("task", {"steps": []}))
    except InfraError:
        pass
    assert len(await BatonStore(store).list_running()) == 1

    result = await h.clear()
    assert result["checkpoints_dropped"] == 1, result
    assert result["batons_dropped"] == 0, result      # no parked gate in this run
    assert await BatonStore(store).list_running() == []
    print("PASS clear() drops running checkpoints and counts them separately")


async def scenario_ttl_sweep_reclaims_running_checkpoint() -> None:
    store = MemoryBackend()
    bs = BatonStore(store)
    stale = Baton(id="stale", stage="c", status="running",
                  cursor_input=Envelope("result", {"steps": ["a", "b"]}),
                  checkpointed_at=1000.0, ttl=60.0)
    fresh = Baton(id="fresh", stage="c", status="running",
                  cursor_input=Envelope("result", {"steps": ["a", "b"]}),
                  checkpointed_at=1000.0, ttl=60.0)
    await bs.save(stale)
    await bs.save(fresh)

    # `now` = 2000: stale (checkpointed at 1000, ttl 60) is expired; bump fresh's
    # checkpoint so it is NOT.
    fresh.checkpointed_at = 1990.0
    await bs.save(fresh)

    dead = await bs.sweep_expired(2000.0)
    assert dead == ["stale"], dead
    assert await bs.load("stale") is None
    assert await bs.load("fresh") is not None
    print("PASS TTL sweep reclaims an abandoned running checkpoint, keeps a fresh one")


async def main() -> None:
    await scenario_kill_then_recover()
    await scenario_checkpoint_cleared_on_gate_park()
    await scenario_refuse_non_running()
    await scenario_checkpoint_write_failure_tolerated()
    await scenario_resume_clears_gate_fields()
    await scenario_clear_drops_running_checkpoints()
    await scenario_ttl_sweep_reclaims_running_checkpoint()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
