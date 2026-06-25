"""Repro: a worktree dirty-guard refusal silently drops `workdir`, and the next
repo-bound shell node then runs with cwd=None.

Root cause of the s_factory BUG-697 green-baseline park (2026-06-24): a RESUMED
run hit role:worktree over a LEFTOVER worktree from a prior attempt (untracked
new test files + an unmerged branch commit). With force=False the dirty-guard
REFUSED and returned a failure-verdict envelope that LACKS `workdir`. The
worktree stage has no validator, so that envelope was traced "ok" and routed
straight into the green-baseline shell node, whose `cwd_from: "workdir"` then
resolved to None -> the relative `./scripts/run-test.sh` was looked up in the
LAUNCHER cwd -> FileNotFoundError. escalate:human parked the cryptic error.

Two independent defects, each gets a scenario below:
  A) the guard REFUSAL is swallowed as a passing stage (engine: a node that
     replies a failure verdict must not be routed onward as "ok").
  B) a shell node with `cwd_from` DECLARED but the key ABSENT silently falls
     back to the launcher cwd instead of failing loud (the "fail-loud over
     silent cwd fallback" convention).

Both scenarios are RED until the engine fix lands; clean-worktree runs (the
control) stay GREEN, proving the drop is specific to the guard-refusal path.

Run: cd yaah-harness && PYTHONPATH=src python3 tests/test_worktree_guard_drops_workdir.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from yaah import Done, Envelope
from yaah.build import build
from yaah.core import NodeConfig
from yaah.nodes.worktree_node import WorktreeNode


def _git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _init_repo(repo: str) -> None:
    os.makedirs(os.path.join(repo, "scripts"))
    script = os.path.join(repo, "scripts", "run-here.sh")
    with open(script, "w") as f:
        f.write("#!/bin/bash\npwd\n")
    os.chmod(script, 0o755)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _config(repo: str, wtroot: str) -> dict:
    # gate -> worktree -> shell(cwd_from:workdir), mirroring approve-spec ->
    # worktree -> green-baseline. The shell command is a RELATIVE executable, so
    # a dropped workdir reproduces the exact FileNotFoundError.
    return {
        "nodes": {
            "role:wt":   {"type": "worktree", "repo": repo, "base": "HEAD",
                          "root": wtroot, "carry": ["task"]},
            "role:gate": {"type": "human_gate", "ask": "approve?", "awaiting": "approve-spec"},
            "role:green": {"type": "shell", "command": ["./scripts/run-here.sh"],
                           "cwd_from": "workdir", "tail_only": False, "carry": ["task"]},
        },
        "graph": {
            "start": "gate", "sticky": ["task", "workdir"],
            "stages": {
                "gate":  {"node": "role:gate",
                          "branch": {"on": "decision", "routes": {}, "default": "wt"}},
                "wt":    {"node": "role:wt", "then": "green"},
                "green": {"node": "role:green", "then": None},
            },
        },
    }


def _precreate_dirty_worktree(repo: str, wtroot: str) -> None:
    """The BUG-697 leftover: a worktree + unmerged branch with an untracked file,
    so role:worktree's dirty-guard (force=False) refuses on the next add."""
    task, branch = "TASK-001", "yaah/TASK-001"
    wd = os.path.join(wtroot, task)
    os.makedirs(wtroot, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, wd, "HEAD")
    with open(os.path.join(wd, "new_feature.txt"), "w") as f:
        f.write("from a prior attempt\n")
    _git(wd, "add", "new_feature.txt")
    _git(wd, "commit", "-q", "-m", "prior attempt")  # unmerged commit
    with open(os.path.join(wd, "untracked.txt"), "w") as f:
        f.write("uncommitted\n")                      # + dirty worktree


async def _run_gated(tmp: str, dirty: bool):
    repo = os.path.join(tmp, "src"); os.makedirs(repo); _init_repo(repo)
    wtroot = os.path.join(tmp, "wt")
    if dirty:
        _precreate_dirty_worktree(repo, wtroot)
    h = build(_config(repo, wtroot))
    out = await h.run(Envelope("task", {"task": "TASK-001"}))
    assert type(out).__name__ == "Suspended", out
    return await h.resume(out.baton_id, Envelope("result", {"decision": "approve"}))


async def scenario_clean_worktree_is_the_control(tmp: str) -> None:
    """No leftover -> worktree adds cleanly -> green runs IN the worktree."""
    out = await _run_gated(tmp, dirty=False)
    assert isinstance(out, Done), out
    cwd = out.output.payload.get("stdout", "").strip()
    assert cwd.endswith(os.path.join("wt", "TASK-001")), ("green ran outside the worktree: %r" % cwd)


async def scenario_dirty_leftover_must_not_drop_workdir(tmp: str) -> None:
    """A refused dirty-guard must NOT silently hand a workdir-less envelope to the
    next repo-bound node. The fixed engine should surface the worktree guard
    refusal (dirty/unmerged) — NOT a cryptic FileNotFoundError from green run in
    the launcher cwd. RED until the fix lands."""
    try:
        out = await _run_gated(tmp, dirty=True)
    except Exception as e:
        msg = str(e)
        # The bug: green dies on a bare FileNotFoundError because workdir was
        # dropped. The FIX should attribute the failure to the worktree guard.
        assert "FileNotFoundError" not in msg, (
            "REPRO: workdir dropped by the swallowed guard refusal -> green "
            "ran with cwd=None. The failure must name the worktree guard "
            "(dirty/unmerged), not surface as: " + msg)
        assert ("dirty" in msg or "unmerged" in msg or "worktree" in msg or "workdir" in msg), msg
        return
    raise AssertionError("expected a loud worktree-guard failure, got: %r" % (out,))


def scenario_B_cwd_from_declared_but_absent_fails_loud() -> None:
    """Defect B in isolation: a repo-bound node DECLARES `cwd_from` but the key is
    ABSENT from the payload and no static `cwd` fallback is set. The silent
    fallback to the launcher cwd is exactly how BUG-697 ran the green-baseline in
    the wrong directory. `resolve_cwd` must FAIL LOUD naming the missing key — the
    'fail-loud over silent cwd fallback' convention. RED until the fix lands."""
    from yaah.core import Envelope
    from yaah.cwd import resolve_cwd

    # key present -> the declared worktree path (unchanged behaviour)
    env = Envelope("task", {"workdir": "/tmp/wt/TASK-1"})
    assert resolve_cwd(env, "workdir") == "/tmp/wt/TASK-1"
    # cwd_from unset -> static default still applies (unchanged behaviour)
    assert resolve_cwd(Envelope("task", {}), None, "/static") == "/static"
    # declared + absent + a static cwd -> fall back to the static cwd (legitimate)
    assert resolve_cwd(Envelope("task", {}), "workdir", "/static") == "/static"
    # declared + absent + NO static cwd -> the bug. Must raise naming the key.
    try:
        got = resolve_cwd(Envelope("task", {}), "workdir")
    except Exception as e:
        assert "workdir" in str(e), e
        return
    raise AssertionError(
        "resolve_cwd silently returned %r instead of failing loud on a declared "
        "cwd_from with no payload key and no static cwd" % (got,))


def main() -> None:
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        print("skip: git not available")
        return
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario_clean_worktree_is_the_control(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario_dirty_leftover_must_not_drop_workdir(tmp))
    scenario_B_cwd_from_declared_but_absent_fails_loud()
    print("ok")


if __name__ == "__main__":
    main()
