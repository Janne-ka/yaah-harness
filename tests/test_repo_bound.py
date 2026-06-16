"""Repo-bound RED -> code -> GREEN loop in an isolated git worktree.

Proves the new primitives end-to-end with NO model: WorktreeNode (isolation),
ShellNode/ShellCheck `cwd_from` (run in the worktree), ExpectField (the RED gate
asserts tests fail first), and the green gate (tests pass after the fix). The
"code" step is an envelope-style `transform` (fn:) that writes the fix — standing
in for a real claude code agent, which the app config (repo.claude.json) wires instead.

Run: cd yaah && PYTHONPATH=src python3 tests/test_repo_bound.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from yaah import Done, Envelope
from yaah.build import build

# The buggy source: add() subtracts. The test below fails until it's fixed.
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "import calc\nassert calc.add(2, 3) == 5, calc.add(2, 3)\nprint('ok')\n"
SPEC = "calc.add(a, b) must return the SUM a + b (it currently subtracts)."


def write_fix(input, config):
    """The 'code' stage: write the correct implementation into the worktree.

    Stands in for a repo-bound code agent. Asserts the spec reached it (carried
    through worktree -> RED), and returns workdir so the green gate (which reads
    this node's OUTPUT) runs in the same worktree.
    """
    assert input.payload.get("spec") == SPEC, "spec must survive worktree->red to the coder"
    workdir = input.payload["workdir"]
    with open(os.path.join(workdir, "calc.py"), "w", encoding="utf-8") as f:
        f.write(FIXED)
    return {"workdir": workdir, "wrote": "calc.py", "spec": input.payload["spec"]}


def _init_repo(repo: str) -> None:
    files = {"calc.py": BUGGY, "test_calc.py": TEST}
    for name, body in files.items():
        with open(os.path.join(repo, name), "w", encoding="utf-8") as f:
            f.write(body)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"], ["git", "add", "."],
                ["git", "commit", "-q", "-m", "init (buggy)"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True)


def _config(repo: str, wtroot: str) -> dict:
    # -B: don't write .pyc. BUGGY and FIXED are the same byte length, so a cached
    # bytecode from the RED run (same size + same-second mtime) would shadow the
    # rewritten source and the GREEN run would import the stale buggy module.
    test_cmd = [sys.executable, "-B", "test_calc.py"]
    return {
        "nodes": {
            "role:wt":      {"type": "worktree", "repo": repo, "base": "HEAD", "root": wtroot, "carry": ["task", "spec"]},
            "role:red-run": {"type": "shell", "command": test_cmd, "cwd_from": "workdir", "tail_only": True, "carry": ["task", "spec"]},
            "role:red-gate": {"type": "expect_field", "key": "ok", "equals": False},
            "role:code":    {"type": "transform", "target": "fn:test_repo_bound:write_fix", "call": "envelope"},
            "role:green":   {"type": "shell_check", "command": test_cmd, "cwd_from": "workdir"},
        },
        "graph": {
            "start": "wt",
            "stages": {
                "wt":   {"node": "role:wt", "then": "red"},
                # RED: run tests, gate that they FAILED before any code exists.
                "red":  {"node": "role:red-run", "validators": ["role:red-gate"],
                         "max_attempts": 1, "then": "code"},
                # code + GREEN: write the fix, gate that tests now PASS (the refix loop).
                "code": {"node": "role:code", "validators": ["role:green"],
                         "max_attempts": 1, "then": None},
            },
        },
    }


async def scenario(tmp: str) -> None:
    repo = os.path.join(tmp, "src")
    os.makedirs(repo)
    _init_repo(repo)
    harness = build(_config(repo, os.path.join(tmp, "wt")))
    out = await harness.run(Envelope("task", {"task": "TASK-001", "spec": SPEC}))

    assert isinstance(out, Done), out
    assert out.output.payload.get("spec") == SPEC, "spec carried end to end"
    workdir = out.output.payload["workdir"]
    # the worktree is isolated: the fix landed there, the source repo is untouched
    assert open(os.path.join(workdir, "calc.py")).read() == FIXED
    assert open(os.path.join(repo, "calc.py")).read() == BUGGY, "source repo must be untouched"
    assert os.path.basename(workdir) == "TASK-001"


async def scenario_unsafe_task_rejected() -> None:
    # assessment #3: payload `task` reaches `os.path.join(root, task)` then
    # `shutil.rmtree(workdir)` + `git worktree remove --force` + `branch -D` —
    # an unsanitized value like `../../etc` would resolve to /etc and wipe it.
    # No git needed: validation must fire BEFORE any shell op.
    from yaah.core import NodeConfig
    from yaah.nodes.worktree_node import WorktreeNode, _safe_task_name

    # Pure unit: the allow-list rejects every known injection vector.
    for bad in [
        "../escape", "../../etc", "/abs/path", "..",
        "-rf", "-flag",                                # flag-injection
        "", " ", "weird name",                         # empty / whitespace / space
        "name\x00with-nul", "with/slash", "with\\bs",  # path separators
        "x" * 81,                                      # >80 chars
    ]:
        try:
            _safe_task_name(bad)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for {!r}".format(bad))
    # Good shapes still pass.
    for ok in ["TASK-001", "TEAM-112", "bug-643", "task_under_score", "v1.2.3"]:
        assert _safe_task_name(ok) == ok

    # Node-level: a payload with an unsafe task short-circuits to a failure
    # verdict envelope; NO git subprocess is invoked.
    wt = WorktreeNode(repo="/nonexistent-repo-no-git-call-should-reach-here",
                      root="/tmp/yaah-unused-root")
    env = Envelope("task", {"task": "../../escape"}, {"correlation_id": "c"})
    out = await wt.invoke(env, NodeConfig())
    assert out.kind == "verdict", out                                          # short-circuited
    assert out.payload.get("status") == "fail"
    assert out.payload["failures"][0]["code"] == "worktree_unsafe_task"


async def scenario_dirty_guard(tmp: str) -> None:
    # cleanup-safety class: cleanup must never destroy uncommitted/unmerged work.
    from yaah.core import NodeConfig
    from yaah.nodes.worktree_node import WorktreeNode

    repo = os.path.join(tmp, "src")
    os.makedirs(repo)
    _init_repo(repo)
    wtroot = os.path.join(tmp, "wt")
    cfg = NodeConfig()
    env = Envelope("task", {"task": "TASK-G1"})

    add = WorktreeNode(repo=repo, root=wtroot)
    out = await add.invoke(env, cfg)
    workdir = out.payload["workdir"]

    # 1) uncommitted file in the worktree -> re-add REFUSES, work survives
    marker = os.path.join(workdir, "green-refix.py")
    with open(marker, "w") as f:
        f.write("precious uncommitted work\n")
    out = await add.invoke(env, cfg)
    assert out.payload.get("status") == "fail", out.payload
    assert out.payload["failures"][0]["code"] == "worktree_dirty", out.payload
    assert os.path.exists(marker), "the guard must not have deleted anything"

    # 2) remove REFUSES on the same dirty worktree
    rm = WorktreeNode(repo=repo, root=wtroot, op="remove")
    out = await rm.invoke(env, cfg)
    assert out.payload["failures"][0]["code"] == "worktree_dirty", out.payload
    assert os.path.exists(marker)

    # 3) committed-but-unmerged: commit the work on the task branch -> still refuses
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "add", "."], cwd=workdir, env=e, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "refix"], cwd=workdir, env=e, check=True)
    out = await rm.invoke(env, cfg)
    assert out.payload["failures"][0]["code"] == "worktree_unmerged", out.payload

    # 4) merge the branch into the default branch -> remove now proceeds
    subprocess.run(["git", "merge", "-q", "yaah/TASK-G1"], cwd=repo, env=e, check=True)
    out = await rm.invoke(env, cfg)
    assert out.payload.get("ok") is True, out.payload
    assert not os.path.exists(workdir)

    # 5) force: true is the explicit opt-out — dirty worktree deleted anyway
    out = await add.invoke(env, cfg)
    with open(os.path.join(out.payload["workdir"], "scratch.txt"), "w") as f:
        f.write("expendable\n")
    forced = WorktreeNode(repo=repo, root=wtroot, force=True)
    out = await forced.invoke(env, cfg)
    assert out.payload.get("workdir"), out.payload  # re-added clean, no refusal


def main() -> None:
    asyncio.run(scenario_unsafe_task_rejected())                  # pure unit; always runs
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        print("skip: git not available")
        return
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario_dirty_guard(tmp))
    print("ok")


if __name__ == "__main__":
    main()
