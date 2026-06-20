"""Cross-PROCESS human gate over a durable FileStore.

The payoff of durable state: one OS process suspends a run at a human gate (the
baton persisted to a file store), a SEPARATE process lists the open gate and
resumes it to completion. Three real `python -m yaah.runtime` invocations share
only the state directory — no broker, fully deterministic.

Run: cd yaah && PYTHONPATH=src python3 tests/test_gates_cross_process.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

PIPELINE = {
    "nodes": {
        # a gate stage whose validator always fails -> escalates to a human
        "role:gate": {"type": "agent", "template": "decide:", "model": "fake:gate", "stage": "gate", "parse": False},
        "role:check": {"type": "expect_field", "key": "ok", "equals": True},
    },
    "graph": {"start": "gate", "stages": {
        "gate": {"node": "role:gate", "validators": ["role:check"],
                 "max_attempts": 1, "escalate": "human"},
    }},
}
ROOT = {
    "transport": {"type": "inproc"},
    "providers": {"fake": {"type": "fake", "default": "thinking"}},
    "default_provider": "fake",
    "state": {"type": "file", "dir": "state"},   # durable -> survives process exit
    "pipeline": "pipeline.json",
    "input": "input.json",
    "run": True,
}


def _run(tmp, *args):
    env = dict(os.environ, PYTHONPATH=SRC)
    root = os.path.join(tmp, "root.json")
    p = subprocess.run([sys.executable, "-B", "-m", "yaah.runtime", root, *args],
                       cwd=tmp, env=env, capture_output=True, text=True)
    assert p.returncode == 0, "exit {}: {}\n{}".format(p.returncode, p.stdout, p.stderr)
    return p.stdout


# the REAL spec-gate shape (writer -> human_gate; revise loops back, approve
# ends the run) — the cross-process approve AND revise branches of the gate
# coverage list, still deterministic (fake provider).
SPEC_PIPELINE = {
    "nodes": {
        "role:writer": {"type": "agent", "template": "write a spec for {{request}}",
                        "model": "fake:writer", "stage": "writer", "parse": False},
        "role:gate": {"type": "human_gate", "ask": "Approve this spec?\n{{raw}}",
                      "awaiting": "spec:approve"},
    },
    "graph": {"start": "write", "stages": {
        "write": {"node": "role:writer", "then": "gate"},
        "gate": {"node": "role:gate",
                 "branch": {"on": "decision", "routes": {"revise": "write"}}},
    }},
}


def scenario_spec_gate_revise_then_approve_cross_process() -> None:
    """Both gate branches across processes: park -> --list shows the SPEC TEXT
    under decision -> --resume revise loops back to the writer and parks again
    -> --resume approve drives to Done. Every step a separate OS process."""
    with tempfile.TemporaryDirectory() as tmp:
        json.dump(SPEC_PIPELINE, open(os.path.join(tmp, "pipeline.json"), "w"))
        json.dump(ROOT, open(os.path.join(tmp, "root.json"), "w"))
        json.dump({"request": "overdraft guard"}, open(os.path.join(tmp, "input.json"), "w"))
        json.dump({"decision": "revise", "feedback": "tighten AC-2"},
                  open(os.path.join(tmp, "revise.json"), "w"))
        json.dump({"decision": "approve"}, open(os.path.join(tmp, "approve.json"), "w"))

        out1 = _run(tmp)
        assert "Suspended" in out1, out1
        m = re.search(r"baton_id=(\S+)", out1)
        assert m, "no baton id in:\n" + out1
        baton_id = m.group(1)

        # the mailbox shows the QUESTION with the spec text under decision
        out2 = _run(tmp, "--list")
        assert "awaiting=spec:approve" in out2, out2
        assert "Approve this spec?" in out2, out2
        assert "thinking" in out2, out2          # the fake writer's spec text

        # revise: loops back through the writer, parks at the gate again
        out3 = _run(tmp, "--resume", baton_id, "revise.json")
        assert "Suspended" in out3, out3
        m = re.search(r"baton_id=(\S+)", out3)
        assert m, out3

        # approve: no matching route -> terminal Done
        out4 = _run(tmp, "--resume", m.group(1), "approve.json")
        assert "Done" in out4, out4
        out5 = _run(tmp, "--list")
        assert "(no suspended gates)" in out5, out5


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        json.dump(PIPELINE, open(os.path.join(tmp, "pipeline.json"), "w"))
        json.dump(ROOT, open(os.path.join(tmp, "root.json"), "w"))
        json.dump({}, open(os.path.join(tmp, "input.json"), "w"))
        json.dump({"ok": True, "text": "approved"}, open(os.path.join(tmp, "decision.json"), "w"))

        # process 1: run -> parks at the human gate, persisted to the file store
        out1 = _run(tmp)
        assert "Suspended" in out1, out1
        m = re.search(r"baton_id=(\S+)", out1)
        assert m, "no baton id in:\n" + out1
        baton_id = m.group(1)

        # process 2: a DIFFERENT process lists the open gate from the shared store
        out2 = _run(tmp, "--list")
        assert baton_id in out2 and "awaiting=human:gate" in out2, out2

        # process 3: a DIFFERENT process delivers the decision and drives to Done
        out3 = _run(tmp, "--resume", baton_id, "decision.json")
        assert "Done" in out3, out3
        assert "approved" in out3, out3

        # the gate is gone from the store now
        out4 = _run(tmp, "--list")
        assert "(no suspended gates)" in out4, out4

    scenario_spec_gate_revise_then_approve_cross_process()
    print("ok")


if __name__ == "__main__":
    main()
