"""`yaah run|resume --json` — ONE machine-readable outcome object on stdout, so a
debugger agent parses stage/failures/fix_hint instead of regex-scraping the prose
`pipeline failed: ...` line (the structured data exists a layer up in StageFailed).

Three outcomes, stable field names, additive:
  failed     -> {"outcome":"failed","stage","failures":[{code,message,fix_hint,data?}]}, exit 1
  done       -> {"outcome":"done","baton_id","payload":{...}},                          exit 0
  suspended  -> {"outcome":"suspended","baton_id","awaiting","concerns","ask"},         exit 0

Default (no --json) prose output is UNCHANGED — the falsifier assertions below run
the same pipelines without --json and confirm the old lines still print.

Run: cd yaah && PYTHONPATH=src python3 tests/test_cli_run_json.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ENV = {**os.environ, "PYTHONPATH": "src"}

# A pipeline that hard-FAILS at runtime (not at validate time): a shell node
# succeeds, then a shell_check validator runs `false` and fails the stage with
# no human gate -> StageFailed. Chosen because the dataflow linter can't prove it
# absent (unlike an unfilled render), so the failure is a genuine RUNTIME one.
FAILING_PIPELINE = {
    "nodes": {
        "role:do": {"type": "shell", "command": ["true"]},
        "role:check": {"type": "shell_check", "command": ["false"]},
    },
    "graph": {"start": "do", "stages": {
        "do": {"node": "role:do", "validators": ["role:check"],
               "max_attempts": 1, "then": None},
    }},
}

# An AGENT (not a checker) placed in `validators:[]`. Its reply is kind 'result'
# (no verdict shape), which `Verdict.from_envelope` would read as a fail with an
# EMPTY failures list -> an opaque "no failure detail" stage-fail (two authors each
# lost ~30min to exactly this). The fix names the misconfiguration:
# validator_not_a_checker + role + fix_hint. `parse:False` keeps the agent's reply a
# plain 'result' (with parse/self-check on, a FAILED output_schema check would emit a
# genuine verdict and mask the case we're diagnosing).
AGENT_IN_VALIDATORS_PIPELINE = {
    "nodes": {
        "role:do": {"type": "agent", "template": "do {{request}}",
                    "model": "fake:do", "stage": "do", "parse": False},
        "role:bogus": {"type": "agent", "template": "judge {{raw}}",
                       "model": "fake:judge", "stage": "bogus", "parse": False},
    },
    "graph": {"start": "do", "stages": {
        "do": {"node": "role:do", "validators": ["role:bogus"],
               "max_attempts": 1, "then": None},
    }},
}

# A legitimate checker (shell_check `true`) that PASSES — guards that the
# kind != VERDICT diagnostic does NOT misfire on a real checker's passing verdict
# (which IS Kind.VERDICT, so it takes the normal path).
PASSING_CHECKER_PIPELINE = {
    "nodes": {
        "role:do": {"type": "shell", "command": ["true"]},
        "role:check": {"type": "shell_check", "command": ["true"]},
    },
    "graph": {"start": "do", "stages": {
        "do": {"node": "role:do", "validators": ["role:check"],
               "max_attempts": 1, "then": None},
    }},
}

LINEAR_PIPELINE = {
    "nodes": {
        "role:writer": {"type": "agent", "template": "write a spec for {{request}}",
                        "model": "fake:writer", "stage": "writer", "parse": False},
    },
    "graph": {"start": "write", "stages": {
        "write": {"node": "role:writer", "then": None},
    }},
}

GATED_PIPELINE = {
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

ROOT = {
    "transport": {"type": "inproc"},
    "providers": {"fake": {"type": "fake", "default": "thinking"}},
    "default_provider": "fake",
    "state": {"type": "memory"},
    "pipeline": "pipeline.json",
    "input": {"request": "overdraft guard"},
    "run": True,
}


def _project(pipeline, root_extra=None):
    d = tempfile.mkdtemp()
    root = dict(ROOT)
    root.update(root_extra or {})
    json.dump(pipeline, open(os.path.join(d, "pipeline.json"), "w"))
    root_path = os.path.join(d, "root.json")
    json.dump(root, open(root_path, "w"))
    return d, root_path


def _run(argv):
    r = subprocess.run([sys.executable, "-m", "yaah.cli", *argv],
                       capture_output=True, text=True, env=ENV)
    return r.returncode, r.stdout, r.stderr


def test_failed_run_json():
    _, root = _project(FAILING_PIPELINE)
    rc, out, err = _run(["run", root, "--json"])
    o = json.loads(out)                       # stdout is EXACTLY one JSON object
    assert rc == 1, (rc, out, err)            # nonzero exit preserved
    assert o["outcome"] == "failed", o
    assert o["stage"] == "do", o
    f = o["failures"][0]
    assert f["code"] == "shell_exit", o
    assert "message" in f and "fix_hint" in f, o


def test_validator_not_a_checker_named():
    # THE FIX: an agent in `validators:[]` used to fail with an empty failures list
    # (opaque "no failure detail"). Now the stage-fail NAMES the misconfiguration —
    # code + role + kind + fix_hint — through the same run --json surface.
    _, root = _project(AGENT_IN_VALIDATORS_PIPELINE)
    rc, out, err = _run(["run", root, "--json"])
    o = json.loads(out)
    assert rc == 1, (rc, out, err)
    assert o["outcome"] == "failed" and o["stage"] == "do", o
    assert o["failures"], o                          # NOT the empty list from before
    f = o["failures"][0]
    assert f["code"] == "validator_not_a_checker", o
    assert "role:bogus" in f["message"], o           # names WHICH validator
    assert "'result'" in f["message"], o             # names the non-verdict kind it replied
    assert f["fix_hint"] and "checker" in f["fix_hint"], o
    assert f["data"] == {"role": "role:bogus", "kind": "result"}, o


def test_validator_not_a_checker_prose_names_it():
    # falsifier: the prose (non --json) path also carries the named detail now,
    # not the old "no failure detail".
    _, root = _project(AGENT_IN_VALIDATORS_PIPELINE)
    rc, out, err = _run(["run", root])
    assert rc == 1, (rc, out, err)
    assert "validator_not_a_checker" in err, (out, err)
    assert "no failure detail" not in err, (out, err)


def test_failed_run_prose_unchanged():
    # falsifier: without --json the old prose line still prints to stderr
    _, root = _project(FAILING_PIPELINE)
    rc, out, err = _run(["run", root])
    assert rc == 1, (rc, out, err)
    assert "pipeline failed:" in err and "shell_exit" in err, (out, err)
    assert out.strip() == "" or "RESULT" not in out, out  # no JSON on stdout


def test_passing_checker_still_passes():
    # no-regression: a real checker returns a PASSING Kind.VERDICT -> the stage
    # completes done. The kind != VERDICT guard must not touch this path.
    _, root = _project(PASSING_CHECKER_PIPELINE)
    rc, out, err = _run(["run", root, "--json"])
    o = json.loads(out)
    assert rc == 0, (rc, out, err)
    assert o["outcome"] == "done", o


def test_done_run_json():
    _, root = _project(LINEAR_PIPELINE)
    rc, out, err = _run(["run", root, "--json"])
    o = json.loads(out)
    assert rc == 0, (rc, out, err)
    assert o["outcome"] == "done", o
    assert "baton_id" in o and o["baton_id"], o
    assert "thinking" in o["payload"]["raw"], o    # the fake writer's text, structured


def test_done_run_prose_unchanged():
    _, root = _project(LINEAR_PIPELINE)
    rc, out, err = _run(["run", root])
    assert rc == 0 and "RESULT:" in out, (rc, out, err)


def test_suspended_run_json():
    d, root = _project(GATED_PIPELINE, {"state": {"type": "file", "dir": "state"}})
    rc, out, err = _run(["run", root, "--json"])
    o = json.loads(out)
    assert rc == 0, (rc, out, err)
    assert o["outcome"] == "suspended", o
    assert o["baton_id"], o
    assert o["awaiting"] == "spec:approve", o
    assert "Approve this spec?" in o["ask"], o

    # resume --json continues to completion and prints the done shape
    baton = o["baton_id"]
    dec = os.path.join(d, "decision.json")
    json.dump({"decision": "approve"}, open(dec, "w"))
    rc, out, err = _run(["resume", root, baton, dec, "--json"])
    o = json.loads(out)
    assert rc == 0 and o["outcome"] == "done", (rc, out, err, o)


def test_suspended_run_prose_unchanged():
    _, root = _project(GATED_PIPELINE, {"state": {"type": "file", "dir": "state"}})
    rc, out, err = _run(["run", root])
    assert rc == 0 and "GATE baton_id=" in out, (rc, out, err)


def main() -> None:
    test_failed_run_json()
    test_validator_not_a_checker_named()
    test_validator_not_a_checker_prose_names_it()
    test_failed_run_prose_unchanged()
    test_passing_checker_still_passes()
    test_done_run_json()
    test_done_run_prose_unchanged()
    test_suspended_run_json()
    test_suspended_run_prose_unchanged()
    print("ok")


if __name__ == "__main__":
    main()
