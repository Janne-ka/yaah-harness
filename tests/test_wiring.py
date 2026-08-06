"""Wiring fingerprint — a graph EDITED between the kill and the recovery is refused.

A recovery re-drives `baton.stage` against the CURRENT graph, so a topology edit
in between makes the cursor mean something else. `wiring_fingerprint` stamps the
topology on the baton at mint and re-stamps it at every checkpoint;
`resume_running` refuses on a mismatch and the gate `resume` warns (a parked human
must not lose their decision because a stage was added elsewhere) — except when the
parked stage itself vanished, which is a clean refusal instead of the bare KeyError
that path used to raise.

Covers: a stage renamed between the kill and the recovery refuses NAMING it; a
rerouted branch refuses; a prompt/model edit does NOT (behaviour drift is
config_fingerprint's job — "fix the prompt, resume the run" must keep working);
`--allow-rewiring` proceeds, and the checkpoint then RE-STAMPS the wiring so the
flag is not needed again on the next crash; a gate resume across an edited graph
warns and completes; a gate resume onto a VANISHED stage refuses cleanly; a
pre-upgrade baton with no stamp is recovered with a note.

Run: cd yaah && PYTHONPATH=src python3 tests/test_wiring.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io

from yaah import Done, Envelope, Graph, Harness, InProcessComms, Stage, Suspended
from yaah.harness import BatonStore, wiring_fingerprint
from yaah.store import MemoryBackend

from test_checkpoint_resume import Gate, InfraError, KillOnce, Step, dead_pid_owner


def _nodes(comms=None):
    """The three-stage cast. `b` is the one that gets killed; its KillOnce raises
    only on its FIRST call, so the SAME comms must be reused for the recovery
    harness (exactly as test_checkpoint_resume models a restart)."""
    comms = comms or InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:b", KillOnce("b"))
    comms.register("role:c", Step("c"))
    return comms


def _graph(then_of_a="b", c_name="c") -> Graph:
    return Graph.of(Stage("a", node="role:a", then=then_of_a),
                    Stage("b", node="role:b", then=c_name),
                    Stage(c_name, node="role:c"))


async def _killed_checkpoint(store, graph):
    """Run `graph` until the kill in stage `b`; return (checkpoint, comms)."""
    comms = _nodes()
    h = Harness(comms, graph, baton_store=BatonStore(store), owner=dead_pid_owner())
    try:
        await h.run(Envelope("task", {"steps": []}))
    except InfraError:
        pass
    running = await BatonStore(store).list_running()
    assert len(running) == 1, running
    return running[0], comms


def scenario_fingerprint_covers_topology_not_behaviour() -> None:
    """The exact surface: renaming a stage, retargeting a `then`, rerouting a
    branch, or changing a fork/fan-in shape all move the hash; the stage's
    behavioural knobs do not."""
    base = _graph()
    assert wiring_fingerprint(base) == wiring_fingerprint(_graph()), "pure function"
    assert wiring_fingerprint(base) != wiring_fingerprint(_graph(c_name="report")), \
        "a renamed stage must move the fingerprint"
    assert wiring_fingerprint(base) != wiring_fingerprint(_graph(then_of_a="c")), \
        "a retargeted `then` must move the fingerprint"

    routed = Graph.of(Stage("a", node="role:a",
                            branch={"on": "ok", "routes": {"true": "b"}, "default": "c"}),
                      Stage("b", node="role:b", then="c"),
                      Stage("c", node="role:c"))
    rerouted = Graph.of(Stage("a", node="role:a",
                              branch={"on": "ok", "routes": {"true": "c"}, "default": "c"}),
                        Stage("b", node="role:b", then="c"),
                        Stage("c", node="role:c"))
    assert wiring_fingerprint(routed) != wiring_fingerprint(rerouted), \
        "a rerouted branch must move the fingerprint"

    # ...and the behavioural knobs must NOT: an operator fixing a prompt, a model,
    # or a timeout and resuming the run is the single most common recovery there is.
    tuned = Graph.of(Stage("a", node="role:a", then="b", max_attempts=5,
                           error_retries=9, feedback=True,
                           validators=["role:check"], escalate="human"),
                     Stage("b", node="role:b", then="c"),
                     Stage("c", node="role:c"))
    assert wiring_fingerprint(tuned) == wiring_fingerprint(base), \
        "retry/validator/escalate knobs are behaviour, not wiring"

    # dict ORDER is not identity — two loads of the same config must agree
    shuffled = Graph(stages={"c": base.stages["c"], "a": base.stages["a"],
                             "b": base.stages["b"]}, start="a")
    assert wiring_fingerprint(shuffled) == wiring_fingerprint(base), "canonical sort"
    print("PASS wiring fingerprint covers topology, excludes behaviour, is canonical")


async def scenario_renamed_stage_refuses_naming_it() -> None:
    store = MemoryBackend()
    cp, comms = await _killed_checkpoint(store, _graph())

    # The stage the cursor sits on is renamed away while the run is dead.
    edited = Graph.of(Stage("a", node="role:a", then="build"),
                      Stage("build", node="role:b", then="c"),
                      Stage("c", node="role:c"))
    h2 = Harness(comms, edited, baton_store=BatonStore(store))
    refused = None
    try:
        await h2.resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None, "a rewired graph must refuse"
    assert "DIFFERENT graph topology" in str(refused), refused
    assert "'b'" in str(refused) and "no longer exists" in str(refused), refused
    print("PASS a stage renamed between the kill and the recovery refuses, naming it")


async def scenario_rerouted_branch_refuses() -> None:
    """The nastier case: every stage still EXISTS, so nothing would crash — the run
    would just quietly continue down wiring it never took."""
    store = MemoryBackend()
    start = Graph.of(Stage("a", node="role:a", then="b"),
                     Stage("b", node="role:b",
                           branch={"on": "ok", "routes": {"true": "c"}, "default": "c"}),
                     Stage("c", node="role:c"))
    cp, comms = await _killed_checkpoint(store, start)

    rerouted = Graph.of(Stage("a", node="role:a", then="b"),
                        Stage("b", node="role:b",
                              branch={"on": "ok", "routes": {"true": "a"}, "default": "c"}),
                        Stage("c", node="role:c"))
    h2 = Harness(comms, rerouted, baton_store=BatonStore(store))
    refused = None
    try:
        await h2.resume_running(cp.id)
    except ValueError as e:
        refused = e
    assert refused is not None and "DIFFERENT graph topology" in str(refused), refused
    assert "still exists" in str(refused), refused
    print("PASS a rerouted branch refuses even though every stage still exists")


async def scenario_prompt_edit_resumes_clean() -> None:
    """A node's PROMPT/model/timeout changed — behaviour, not wiring. Recovery must
    not even mention it: refusing here would make --allow-rewiring reflexive."""
    store = MemoryBackend()
    cp, comms = await _killed_checkpoint(store, _graph())

    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:b", Step("b-rewritten"))   # a different implementation entirely
    comms.register("role:c", Step("c"))
    h2 = Harness(comms, _graph(), baton_store=BatonStore(store))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = await h2.resume_running(cp.id)
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b-rewritten", "c"], out.output.payload
    assert "topology" not in err.getvalue(), err.getvalue()
    print("PASS a prompt/model/implementation edit resumes clean (no wiring refusal)")


async def scenario_allow_rewiring_proceeds() -> None:
    store = MemoryBackend()
    cp, comms = await _killed_checkpoint(store, _graph())
    edited = Graph.of(Stage("a", node="role:a", then="b"),
                      Stage("b", node="role:b", then="report"),   # c renamed, cursor is at b
                      Stage("report", node="role:c"))
    h2 = Harness(comms, edited, baton_store=BatonStore(store))
    out = await h2.resume_running(cp.id, allow_rewiring=True)
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b", "c"], out.output.payload
    print("PASS --allow-rewiring proceeds against the edited graph")


async def scenario_gate_resume_across_edited_graph_warns() -> None:
    """A parked human's decision must not be lost to an unrelated graph edit — so
    the gate resume WARNS and completes."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:gate", Gate())
    comms.register("role:c", Step("c"))
    graph = Graph.of(Stage("gate", node="role:gate", then="c"),
                     Stage("c", node="role:c"))
    h = Harness(comms, graph, baton_store=BatonStore(store))
    out = await h.run(Envelope("task", {"steps": []}))
    assert isinstance(out, Suspended), out

    edited = Graph.of(Stage("gate", node="role:gate", then="c"),
                      Stage("c", node="role:c", then="extra"),
                      Stage("extra", node="role:c"))
    h2 = Harness(comms, edited, baton_store=BatonStore(store))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        done = await h2.resume(out.baton_id, Envelope("resume", {"human": "approve"}))
    assert isinstance(done, Done), done
    assert "DIFFERENT graph topology" in err.getvalue(), err.getvalue()
    assert done.output.payload["steps"] == ["c", "c"], done.output.payload
    print("PASS a gate resume across an edited graph warns and still delivers the decision")


async def scenario_gate_resume_onto_vanished_stage_refuses_cleanly() -> None:
    """The one gate case that is NOT a warning: the parked stage is gone, so there
    is nowhere to deliver the decision. This path used to raise a bare KeyError."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:gate", Gate())
    comms.register("role:c", Step("c"))
    graph = Graph.of(Stage("gate", node="role:gate", then="c"),
                     Stage("c", node="role:c"))
    h = Harness(comms, graph, baton_store=BatonStore(store))
    out = await h.run(Envelope("task", {"steps": []}))

    without_gate = Graph.of(Stage("c", node="role:c"))
    h2 = Harness(comms, without_gate, baton_store=BatonStore(store))
    refused = None
    try:
        await h2.resume(out.baton_id, Envelope("resume", {"human": "approve"}))
    except KeyError as e:                    # the OLD failure mode — must not happen
        raise AssertionError("vanished gate stage raised a bare KeyError: {!r}".format(e))
    except ValueError as e:
        refused = e
    assert refused is not None, "a vanished gate stage must refuse"
    assert "NO LONGER EXISTS" in str(refused) and "'gate'" in str(refused), refused
    # ...and the gate is STILL PARKED — a refusal must not consume the decision.
    assert len(await BatonStore(store).list_suspended()) == 1
    print("PASS a gate resume onto a vanished stage refuses cleanly, gate stays parked")


async def scenario_pre_upgrade_baton_skips_with_a_note() -> None:
    store = MemoryBackend()
    cp, comms = await _killed_checkpoint(store, _graph())
    stored = await BatonStore(store).load(cp.id)
    stored.wiring = None                     # a record written before the stamp existed
    await BatonStore(store).save(stored)

    h2 = Harness(comms, _graph(c_name="renamed"),
                 baton_store=BatonStore(store))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        out = await h2.resume_running(cp.id)
    assert isinstance(out, Done), out
    assert "predates the wiring fingerprint" in err.getvalue(), err.getvalue()
    print("PASS a pre-upgrade baton recovers WITHOUT a topology check, with a note")


async def scenario_checkpoint_restamps_the_wiring() -> None:
    """`--allow-rewiring` is a ONE-TIME assertion, not a permanent condition.

    Before the re-stamp, `wiring` was written only at mint, so a run recovered onto an
    edited graph kept claiming the ORIGINAL topology while actually driving the new
    one. Every subsequent crash refused again, and the operator re-asserted
    compatibility against a graph that was no longer the one that had run — the flag
    became reflexive, which is exactly what costs a guard its meaning.

    `_checkpoint` now stamps the DRIVING harness's fingerprint, so the record means
    "the topology this cursor was produced by". Two kills in one run prove it: kill,
    recover with the flag onto an edited graph, kill again, and the second recovery
    needs no flag."""
    store = MemoryBackend()
    comms = InProcessComms()
    comms.register("role:a", Step("a"))
    comms.register("role:b", KillOnce("b"))
    comms.register("role:c", KillOnce("c"))
    comms.register("role:d", Step("d"))
    original = Graph.of(Stage("a", node="role:a", then="b"),
                        Stage("b", node="role:b", then="c"),
                        Stage("c", node="role:c", then="d"),
                        Stage("d", node="role:d"))
    edited = Graph.of(Stage("a", node="role:a", then="b"),
                      Stage("b", node="role:b", then="c"),
                      Stage("c", node="role:c", then="report"),   # d renamed
                      Stage("report", node="role:d"))

    try:
        await Harness(comms, original, baton_store=BatonStore(store),
                      owner=dead_pid_owner()).run(Envelope("task", {"steps": []}))
    except InfraError:
        pass
    running = await BatonStore(store).list_running()
    assert len(running) == 1, running
    cp = running[0]
    assert cp.stage == "b" and cp.wiring == wiring_fingerprint(original), cp

    # Recover onto the EDITED graph with the flag; `b` re-runs clean, then `c` kills.
    h2 = Harness(comms, edited, baton_store=BatonStore(store), owner=dead_pid_owner())
    try:
        await h2.resume_running(cp.id, allow_rewiring=True)
    except InfraError:
        pass
    running = await BatonStore(store).list_running()
    assert len(running) == 1, running
    cp2 = running[0]
    assert cp2.id == cp.id and cp2.stage == "c", cp2
    assert cp2.wiring == wiring_fingerprint(edited), \
        "the checkpoint must carry the graph that actually RAN, not the mint's"

    # ...so THIS recovery needs no flag at all.
    out = await Harness(comms, edited, baton_store=BatonStore(store)).resume_running(cp2.id)
    assert isinstance(out, Done), out
    assert out.output.payload["steps"] == ["a", "b", "c", "d"], out.output.payload
    print("PASS a checkpoint re-stamps the wiring, so --allow-rewiring is not needed twice")


async def main() -> None:
    scenario_fingerprint_covers_topology_not_behaviour()
    await scenario_checkpoint_restamps_the_wiring()
    await scenario_renamed_stage_refuses_naming_it()
    await scenario_rerouted_branch_refuses()
    await scenario_prompt_edit_resumes_clean()
    await scenario_allow_rewiring_proceeds()
    await scenario_gate_resume_across_edited_graph_warns()
    await scenario_gate_resume_onto_vanished_stage_refuses_cleanly()
    await scenario_pre_upgrade_baton_skips_with_a_note()
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
