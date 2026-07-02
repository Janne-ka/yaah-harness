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


def main() -> None:
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
