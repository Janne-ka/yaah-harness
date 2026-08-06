"""Runtime actions RETURN data — the programmatic operator surface.

The contract under test: `run_root`/`resume_gate` return the run's Outcome
(Done/Suspended/...), `list_gates` returns the suspended Batons, `baton_schema`
returns the decision-form dict (and raises ValueError on its error cases),
`clear_state` returns the harness clear result. None of them writes DATA to
stdout — rendering belongs to yaah.cli — so the MCP server and any embedding
app consume the same functions the CLI does instead of re-implementing them
"minus the printing".

The flow mirrors the cross-process gate story in ONE process: each action call
assembles fresh over the same durable file store (the rendezvous), exactly like
separate `yaah` invocations would.

Run: cd yaah && PYTHONPATH=src python3 tests/test_runtime_actions.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile

from yaah.adapters.stores.file_backend import FileBackend
from yaah.core import Envelope, Kind
from yaah.harness import Baton, BatonStore, Done, Suspended
from yaah import runtime as r

PIPELINE = {
    "nodes": {
        "role:writer": {"type": "agent", "template": "write a spec for {{request}}",
                        "model": "fake:writer", "stage": "writer", "parse": False},
        "role:gate": {"type": "human_gate", "ask": "Approve this spec?\n{{raw}}",
                      "awaiting": "spec:approve", "form": "approve_or_revise"},
    },
    "graph": {"start": "write", "stages": {
        "write": {"node": "role:writer", "then": "gate"},
        "gate": {"node": "role:gate",
                 "branch": {"on": "decision", "routes": {"revise": "write"}}},
    }},
}


def _root(tmp: str) -> dict:
    return {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "a draft"}},
        "default_provider": "fake",
        "state": {"type": "file", "dir": os.path.join(tmp, "state")},
        "pipeline": "pipeline.json",
        "input": {"request": "overdraft guard"},
        "run": True,
    }


def _call(coro):
    """Run an action; return (result, stdout_text). The stdout capture is the
    point — an action printing data would silently break the MCP surface."""
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        result = asyncio.run(coro)
    finally:
        sys.stdout = old
    return result, buf.getvalue()


def scenario_inline_pipeline_dict_runs() -> None:
    """An INLINE pipeline dict is valid per validate_root/schema/manual — but
    _assemble_harness crashed on it with a bare TypeError (_rel(base, dict)),
    a fail-loud violation found by the A/B design eval. It must just run.
    live_config + inline pipeline is meaningless (no file to re-read) and must
    be rejected LOUD, not crash."""
    root = {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake", "default": "done"}},
        "default_provider": "fake",
        "pipeline": {
            "nodes": {"echo": {"type": "agent", "template": "hi",
                               "model": "fake:x", "parse": False}},
            "graph": {"start": "s", "stages": {"s": {"node": "echo"}}},
        },
        "input": {},
        "run": True,
    }
    out, printed = _call(r.run_root(root, "."))
    assert isinstance(out, Done), out
    assert out.output.payload.get("raw") == "done", out.output.payload

    bad = dict(root, live_config=True)
    try:
        _call(r.run_root(bad, "."))
        raise AssertionError("live_config + inline pipeline must be rejected loud")
    except ValueError as e:
        assert "live_config" in str(e) and "inline" in str(e), e


def scenario_clear_batons_targeted() -> None:
    """`clear_batons` drops EXACTLY the named batons and leaves the rest; it refuses
    an unknown id or an unexpected status (ActionError, exit 1 at the CLI) and
    deletes NOTHING on refusal. The surgical orphan cleanup a driver runs when a
    killed run left duplicate gate batons — the counterpart to the global
    `clear_state` reset. F2: a Level 2 RUNNING checkpoint is also accepted, because
    this is the operator's only single-record delete path."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp)
        store = BatonStore(FileBackend(os.path.join(tmp, "state")))
        for i in range(3):
            asyncio.run(store.save(Baton(
                id="b-{}".format(i), stage="gate", awaiting="spec:approve",
                status="suspended", parked_at=float(i))))

        # unknown id → refuse, delete nothing (all-or-nothing validation)
        try:
            _call(r.clear_batons(root, tmp, ["b-0", "nope"]))
            raise AssertionError("expected ActionError for unknown id")
        except r.ActionError as e:
            assert "nope" in str(e), e
        gates, _ = _call(r.list_gates(root, tmp))
        assert {b.id for b in gates} == {"b-0", "b-1", "b-2"}, gates

        # an UNEXPECTED status (not suspended, not running) → refuse
        asyncio.run(store.save(Baton(id="b-done", stage="gate", status="done")))
        try:
            _call(r.clear_batons(root, tmp, ["b-done"]))
            raise AssertionError("expected ActionError for a 'done' baton")
        except r.ActionError as e:
            assert "not 'suspended' or 'running'" in str(e), e
        asyncio.run(store.delete("b-done"))

        # F2: a RUNNING checkpoint IS clearable — otherwise an abandoned one is
        # undeletable except by the all-or-nothing reset.
        asyncio.run(store.save(Baton(
            id="b-crashed", stage="two", status="running",
            cursor_input=Envelope(Kind.RESULT, {"raw": "one-out"}),
            checkpointed_at=1000.0)))
        result, _ = _call(r.clear_batons(root, tmp, ["b-crashed"]))
        assert result["batons_dropped"] == 1 and result["ids"] == ["b-crashed"], result
        running, _ = _call(r.list_checkpoints(root, tmp))
        assert running == [], running
        gates, _ = _call(r.list_gates(root, tmp))
        assert {b.id for b in gates} == {"b-0", "b-1", "b-2"}, gates

        # targeted delete: only the named batons go, the rest stay parked; the
        # action returns data (no stdout — rendering is the CLI's job)
        result, printed = _call(r.clear_batons(root, tmp, ["b-0", "b-2"]))
        assert result["batons_dropped"] == 2, result
        assert set(result["ids"]) == {"b-0", "b-2"}, result
        assert printed == "", printed
        gates, _ = _call(r.list_gates(root, tmp))
        assert {b.id for b in gates} == {"b-1"}, gates


CKPT_PIPELINE = {
    "nodes": {
        "role:one": {"type": "agent", "template": "one", "model": "fake:x", "parse": False},
        "role:two": {"type": "agent", "template": "two", "model": "fake:x", "parse": False},
    },
    "graph": {"start": "one", "stages": {
        "one": {"node": "role:one", "then": "two"},
        "two": {"node": "role:two"},
    }},
}


def scenario_resume_run_recovers_crash() -> None:
    """`resume_run` recovers a run KILLED mid-flight from its running checkpoint
    (Level 2), cross-process: a checkpoint written by a crashed process is picked up
    by a FRESH assemble over the same durable store and re-driven to completion.
    `list_checkpoints` surfaces it before recovery (the `yaah list` running section)
    and it is gone after. A suspended gate is NOT a running checkpoint — resume_run
    refuses it."""
    import time
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "pipeline.json"), "w") as f:
            json.dump(CKPT_PIPELINE, f)
        root = dict(_root(tmp), pipeline="pipeline.json", input={})
        root["providers"] = {"fake": {"type": "fake", "default": "done"}}

        # A crashed process left a running checkpoint cursored at the in-flight
        # stage "two" (stage "one" already completed — its output is the cursor).
        store = BatonStore(FileBackend(os.path.join(tmp, "state")))
        asyncio.run(store.save(Baton(
            id="crashed", stage="two", status="running",
            cursor_input=Envelope(Kind.RESULT, {"raw": "one-out"}),
            checkpointed_at=time.time())))

        # list_checkpoints surfaces it (the recovery view) — distinct from the
        # suspended-gate mailbox, which is empty here.
        running, printed = _call(r.list_checkpoints(root, tmp))
        assert [b.id for b in running] == ["crashed"], running
        assert running[0].stage == "two", running[0]
        gates, _ = _call(r.list_gates(root, tmp))
        assert gates == [], gates
        assert printed == "", printed

        # resume_run re-drives from "two" to completion; the checkpoint is deleted.
        done, printed = _call(r.resume_run(root, tmp, "crashed"))
        assert isinstance(done, Done), done
        assert "RESULT" not in printed and "GATE" not in printed, printed
        running, _ = _call(r.list_checkpoints(root, tmp))
        assert running == [], running

        # A suspended gate is not a running checkpoint — resume_run refuses it.
        asyncio.run(store.save(Baton(id="parked", stage="gate", status="suspended",
                                     parked_at=time.time())))
        try:
            _call(r.resume_run(root, tmp, "parked"))
            raise AssertionError("resume_run must refuse a suspended baton")
        except ValueError as e:
            assert "not 'running'" in str(e), e

        # F3: an UNKNOWN id is a domain refusal (ActionError), NOT a bare KeyError
        # — KeyError is in no surface's error boundary, so it printed a traceback.
        try:
            _call(r.resume_run(root, tmp, "typo-id"))
            raise AssertionError("resume_run must refuse an unknown id")
        except r.ActionError as e:
            assert "typo-id" in str(e) and "yaah list" in str(e), e
        # ...and the same hole existed on the plain gate resume.
        try:
            _call(r.resume_gate(root, tmp, "typo-id", {"decision": "approve"}))
            raise AssertionError("resume_gate must refuse an unknown id")
        except r.ActionError as e:
            assert "typo-id" in str(e) and "yaah list" in str(e), e


def scenario_root_ttl_keys_reach_the_baton() -> None:
    """The ROOT half of the split sweep window. `baton_ttl` / `checkpoint_ttl` are root
    keys that `_seed_task` turns into `harness.run(ttl=…, checkpoint_ttl=…)` kwargs —
    a three-hop thread (root -> run_kw -> Baton) that nothing exercised, so a break
    anywhere in it would have shown up only as a fleet silently sweeping on the wrong
    window months later. What the two windows then DO is
    `test_checkpoint_resume.py::scenario_checkpoint_ttl_sweeps_the_run_not_the_gate`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "pipeline.json"), "w") as f:
            json.dump(PIPELINE, f)
        # `lease_horizon` must come down WITH `checkpoint_ttl`: `validate_budgets`
        # rejects a horizon longer than the checkpoint window (a foreign host's
        # crashed run would be swept before it was old enough to declare stale).
        # That coherence rule is asserted here too — it is easy to break by lowering
        # one number and not the other.
        # `lease_host` rides along: it is the third lease/TTL root key threaded through
        # `_assemble_harness`, and a break in that threading is a TypeError at build.
        root = dict(_root(tmp), baton_ttl=100000, checkpoint_ttl=1, lease_horizon=1,
                    lease_host="node-7")

        # the seeding hop, in isolation: absent keys pass NOTHING (the harness
        # defaults stay the one place the numbers are written down).
        _task, run_kw = r._seed_task(root, tmp)
        assert run_kw == {"ttl": 100000, "checkpoint_ttl": 1}, run_kw
        assert r._seed_task(_root(tmp), tmp)[1] == {}, "absent root keys pass no kwargs"

        # ...and end to end: the run parks, and the PERSISTED baton carries both.
        out, _printed = _call(r.run_root(root, tmp))
        assert isinstance(out, Suspended), out
        store = BatonStore(FileBackend(os.path.join(tmp, "state")))
        baton = asyncio.run(store.load(out.baton_id))
        assert (baton.ttl, baton.checkpoint_ttl) == (100000, 1), baton
    print("PASS root `baton_ttl`/`checkpoint_ttl` reach the persisted baton's two windows")


def scenario_clear_does_not_create_the_run_dir() -> None:
    """A TEARDOWN verb must not build anything. `clear_state` assembles a harness only
    to reach the clear/flush primitives, and the assembly resolves `run_dir` — which
    used to `os.makedirs` it, so `yaah clear` created the run's artifact root on a root
    whose run may never have happened. A `run` still creates it (that is where nodes
    write), so the two are asserted together."""
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "pipeline.json"), "w") as f:
            json.dump(PIPELINE, f)
        root = dict(_root(tmp), run_dir="artifacts/run-1")
        made = os.path.join(tmp, "artifacts", "run-1")

        _call(r.clear_state(root, tmp))
        assert not os.path.exists(made), "clear must not CREATE {}".format(made)

        _call(r.run_root(root, tmp))          # parks at the gate; run_dir is real work
        assert os.path.isdir(made), "a run still creates its artifact root"

        # and a clear AFTER the run leaves the existing directory alone (it drops
        # store state, not artifacts).
        _call(r.clear_state(root, tmp))
        assert os.path.isdir(made), made
    print("PASS `clear` resolves run_dir without creating it; `run` still creates it")


def main() -> None:
    scenario_inline_pipeline_dict_runs()
    scenario_clear_batons_targeted()
    scenario_resume_run_recovers_crash()
    scenario_root_ttl_keys_reach_the_baton()
    scenario_clear_does_not_create_the_run_dir()
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "pipeline.json"), "w") as f:
            json.dump(PIPELINE, f)
        root = _root(tmp)

        # run_root returns the OUTCOME — here a park at the human gate. No
        # GATE/RESULT prints: those are the CLI's rendering, not the action's.
        out, printed = _call(r.run_root(root, tmp))
        assert isinstance(out, Suspended), out
        assert out.baton_id and out.awaiting == "spec:approve", out
        assert "GATE" not in printed and "RESULT" not in printed, printed

        # list_gates returns the suspended Batons (data, not prose).
        gates, printed = _call(r.list_gates(root, tmp))
        assert [b.id for b in gates] == [out.baton_id], gates
        assert gates[0].awaiting == "spec:approve", gates[0]
        assert printed == "", printed
        # a genuinely-parked run carries a wall-clock parked_at, and it reaches the
        # list JSON contract as a non-null float (the disambiguation key, M26).
        parked_at = r._baton_json(gates[0])["parked_at"]
        assert isinstance(parked_at, float) and parked_at > 0, parked_at

        # baton_schema returns the decision-form contract.
        schema, printed = _call(r.baton_schema(root, tmp, out.baton_id))
        assert schema["form"] == "approve_or_revise", schema
        assert schema["baton_id"] == out.baton_id, schema
        assert schema["awaiting"] == "spec:approve", schema
        assert schema["schema"]["properties"]["decision"]["enum"] == ["approve", "revise"]
        assert printed == "", printed

        # ...and RAISES on its error cases (the CLI maps these to exit 1).
        for baton_id, expect in [("missing", "no baton")]:
            try:
                _call(r.baton_schema(root, tmp, baton_id))
                raise AssertionError("expected ValueError for {!r}".format(baton_id))
            except ValueError as e:
                assert expect in str(e), e
        store = BatonStore(FileBackend(os.path.join(tmp, "state")))
        asyncio.run(store.save(Baton(id="b-empty", stage=None, status="suspended",
                                     pending=None)))
        asyncio.run(store.save(Baton(
            id="b-noform", stage="x", status="suspended",
            pending=Envelope(Kind.AWAIT, {"ask": "legacy", "awaiting": "human"}))))
        for baton_id, expect in [("b-empty", "no parked envelope"),
                                 ("b-noform", "declared form")]:
            try:
                _call(r.baton_schema(root, tmp, baton_id))
                raise AssertionError("expected ValueError for {!r}".format(baton_id))
            except ValueError as e:
                assert expect in str(e), e
        asyncio.run(store.delete("b-empty"))
        asyncio.run(store.delete("b-noform"))

        # resume_gate returns the next Outcome — approve has no matching route,
        # so the run completes: a Done carrying the final output envelope.
        done, printed = _call(r.resume_gate(root, tmp, out.baton_id,
                                            {"decision": "approve"}))
        assert isinstance(done, Done), done
        assert done.output is not None and done.output.payload, done
        assert "RESULT" not in printed and "GATE" not in printed, printed

        # clear_state returns the clear result instead of printing it.
        _call(r.run_root(root, tmp))          # park a fresh run to clear
        cleared, printed = _call(r.clear_state(root, tmp))
        assert cleared is not None, cleared
        assert "CLEARED" not in printed, printed
        gates, _ = _call(r.list_gates(root, tmp))
        assert gates == [], gates

    print("PASS runtime actions return data (run/list/baton-schema/resume/clear)")


if __name__ == "__main__":
    main()
