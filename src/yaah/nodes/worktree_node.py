"""WorktreeNode — give a task an isolated git worktree (and tear it down).

Used by: yaah.build (the 'worktree' node type) as the first stage of a
repo-bound pipeline (a code-change app's code/test/qa stages), and as a
cleanup/merge-prep stage.
Where: local hosts that hold the source repo.
Why: each task's edits must be isolated from the working tree and from other
tasks — a git worktree on a fresh branch is exactly that. The node emits
{workdir, branch, repo} into the payload; downstream repo-bound nodes read
`workdir` as their cwd (ShellNode/ShellCheck cwd_from, Agent cwd_from). This is
the harness's answer to a per-task sandbox, without the bash plumbing.

Config (trusted, build-time): repo (source repo path), base (ref, default
"HEAD"), root (parent dir for worktrees), branch_prefix (default "yaah/"),
op ("add" | "remove"), force (default false). The task id comes from the payload
(task_key, default "task") — data, not config. The git commands are fixed; only
paths/refs vary.

Dirty-guard: every destructive path (remove, and add's stale-wipe of a prior
run) REFUSES when the worktree has uncommitted changes or the task branch holds
commits no other ref reaches — fail the stage and keep the work, never delete
it (incident: a completed, green, uncommitted refix destroyed by cleanup).
`force: true` is the explicit opt-out.

Targets Python 3.9+.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Optional

from ..core import Node, Envelope, Failure, Kind, NodeConfig, Verdict
from ._shell import _run

# `task` comes from PAYLOAD — the one place payload data reaches a destructive
# filesystem op (`shutil.rmtree(workdir)`, `git worktree remove --force`,
# `branch -D`) and a git CLI argv (`-b <branch>`). Without validation, a payload
# like `task = "../../etc"` makes `os.path.join(root, task)` resolve to /etc and
# the cleanup wipes it; `task = "-rf"` reaches git as a flag; empty/whitespace
# collapses workdir to root and deletes the whole worktree set. Allow-list:
# `[A-Za-z0-9_.-]{1,80}` (covers TASK-001 / TEAM-112 / bug-643 styles), no
# `..` even within the charset, no leading `-` (flag-injection).
_SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


def _safe_task_name(raw: str) -> str:
    """Return `raw` iff it's filesystem-safe to use as a worktree directory
    name and a git branch suffix; otherwise raise ValueError. Charset
    `[A-Za-z0-9_.-]`, length 1..80, no `..` (path traversal), no leading `-`
    (flag-injection in the git argv)."""
    if not _SAFE_TASK_RE.match(raw):
        raise ValueError(
            "task {!r} fails the safe-name allow-list "
            "(expected [A-Za-z0-9_.-]{{1,80}})".format(raw))
    if ".." in raw:
        raise ValueError("task {!r} contains '..' (path-traversal)".format(raw))
    if raw.startswith("-"):
        raise ValueError("task {!r} starts with '-' (flag-injection)".format(raw))
    return raw


class WorktreeNode(Node):
    def __init__(self, *, repo: str, base: str = "HEAD", root: Optional[str] = None,
                 branch_prefix: str = "yaah/", op: str = "add", task_key: str = "task",
                 timeout: Optional[float] = None, carry: Optional[list] = None,
                 force: bool = False) -> None:
        self._repo = repo
        self._base = base
        self._root = root or os.path.join(repo, ".yaah-worktrees")
        self._branch_prefix = branch_prefix
        self._op = op
        self._task_key = task_key
        self._timeout = timeout
        self._force = force
        # payload keys to forward (worktree otherwise emits only workdir/branch/...)
        # — e.g. carry the spec/task on to the repo-bound coder downstream
        self._carry = list(carry or [])

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        raw = input.payload.get(self._task_key, "task")
        try:
            task = _safe_task_name(str(raw))
        except ValueError as e:
            # Fail loud as a node verdict — the destructive ops below assume the
            # name is safe, and we'd rather have a hard failure than a near-miss.
            return Verdict.failed(Failure(
                "worktree_unsafe_task", str(e),
                "set the payload {!r} to a [A-Za-z0-9_.-] identifier".format(self._task_key)
            )).to_envelope(input)
        workdir = os.path.join(self._root, task)
        branch = self._branch_prefix + task
        if self._op == "remove":
            return await self._remove(input, workdir, branch)
        return await self._add(input, task, workdir, branch)

    async def _guard(self, input: Envelope, workdir: str, branch: str) -> Optional[Envelope]:
        """The dirty-guard: a failure envelope if deleting workdir/branch would
        destroy work (uncommitted changes, or commits only this branch reaches);
        None when deletion is safe or `force: true` opted out."""
        if self._force:
            return None
        if os.path.exists(workdir):
            code, text = await _run(["git", "-C", workdir, "status", "--porcelain"],
                                    cwd=None, timeout=self._timeout, shell=False)
            if code == 0 and text.strip():
                return Verdict.failed(Failure(
                    "worktree_dirty",
                    "worktree {} has uncommitted changes — refusing to delete it".format(workdir),
                    "commit/stash the work (or remove it yourself; `force: true` on the node overrides)"
                )).to_envelope(input)
        if await self._branch_unmerged(branch):
            return Verdict.failed(Failure(
                "worktree_unmerged",
                "branch {} holds commits no other ref reaches — refusing to delete it".format(branch),
                "merge or push the branch (or `force: true` on the node overrides)"
            )).to_envelope(input)
        return None

    async def _branch_unmerged(self, branch: str) -> bool:
        """True iff `branch` exists and has commits unreachable from every OTHER
        local/remote ref — i.e. deleting it would orphan work."""
        ref = "refs/heads/" + branch
        code, _ = await _git(self._repo, ["rev-parse", "--verify", "-q", ref], self._timeout)
        if code != 0:
            return False
        code, out = await _git(self._repo, ["for-each-ref", "--format=%(refname)",
                                            "refs/heads", "refs/remotes"], self._timeout)
        others = [r for r in out.split() if r != ref] if code == 0 else []
        argv = ["rev-list", "--count", branch] + (["--not"] + others if others else [])
        code, out = await _git(self._repo, argv, self._timeout)
        return code == 0 and int(out.strip() or 0) > 0

    async def _add(self, input: Envelope, task: str, workdir: str, branch: str) -> Envelope:
        os.makedirs(self._root, exist_ok=True)
        # Make the op repeatable: drop any stale worktree/branch from a prior run —
        # but only after the dirty-guard says the prior run left nothing to lose.
        refused = await self._guard(input, workdir, branch)
        if refused is not None:
            return refused
        await _git(self._repo, ["worktree", "remove", "--force", workdir], self._timeout)
        if os.path.exists(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        await _git(self._repo, ["branch", "-D", branch], self._timeout)
        code, text = await _git(
            self._repo, ["worktree", "add", "-b", branch, workdir, self._base], self._timeout)
        if code != 0:
            return Verdict.failed(Failure(
                "worktree_add", "git worktree add failed (exit {})".format(code), text[-2000:]
            )).to_envelope(input)
        extra = {k: input.payload[k] for k in self._carry if k in input.payload}
        return input.reply(Kind.RESULT, workdir=workdir, branch=branch,
                           repo=self._repo, base=self._base, **extra)

    async def _remove(self, input: Envelope, workdir: str, branch: str) -> Envelope:
        refused = await self._guard(input, workdir, branch)
        if refused is not None:
            return refused
        code, text = await _git(self._repo, ["worktree", "remove", "--force", workdir], self._timeout)
        return input.reply(Kind.RESULT, removed=workdir, ok=(code == 0))


async def _git(repo: str, args: list, timeout: Optional[float]):
    return await _run(["git", "-C", repo] + args, cwd=None, timeout=timeout, shell=False)
