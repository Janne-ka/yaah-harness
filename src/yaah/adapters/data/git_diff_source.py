"""GitDiffSource — fetch a git diff, scoped to the changed lines (± N context).

Used by: the 'get' node (GetNode) in a repo-bound pipeline — after the code
agent edits the worktree, fetch the diff so review/eval read only what changed.
Where: local hosts holding the repo/worktree.
Why: the headline 'smart get'. `git diff --unified=N` returns each changed hunk
with N lines of surrounding context — so a reviewer sees the change and just
enough around it, never whole files riding down the chain. N is config: 0 = only
changed lines, 3 = a little context, more for wider blast radius.

Config: context (default 3), intent_to_add (run `git add -N -A` first so NEW
files show up in the diff — the code agent leaves changes uncommitted), repo
(fallback when the call gives no cwd). The cwd (worktree) and a ref/range come
per call (from GetNode: cwd_from the payload, key = the ref). The git command is
fixed; only paths/refs/context vary.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, List, Optional, Sequence


class GitDiffSource:
    def __init__(self, *, repo: Optional[str] = None, context: int = 3,
                 intent_to_add: bool = False, timeout: Optional[float] = None,
                 spawn: Optional[Callable[..., Awaitable[Any]]] = None) -> None:
        self._repo = repo
        self._context = context
        self._intent_to_add = intent_to_add
        self._timeout = timeout
        # `spawn` is the external dependency, injected for testability: an async
        # (*argv, stdout=, stderr=) -> process callable. Defaults to
        # asyncio.create_subprocess_exec. Tests pass a fake-process spawner to
        # assert the git argv (cmd construction, -C, refs, paths) and cover the
        # exit/timeout->kill path without a real git repo.
        self._spawn = spawn or asyncio.create_subprocess_exec

    async def fetch(self, key: str = "", *, cwd: Optional[str] = None,
                    context: Optional[int] = None, paths: Optional[Sequence[str]] = None,
                    **_: Any) -> str:
        repo = cwd or self._repo
        ctx = self._context if context is None else int(context)
        if self._intent_to_add:
            # mark new/untracked files as intent-to-add so `git diff` includes them
            rc, text = await self._git(repo, ["add", "-N", "-A"])
            if rc:  # don't let a git failure ride downstream as if it were a diff (M2)
                raise RuntimeError("git add -N -A failed (rc={}): {}".format(rc, text.strip()))
        args: List[str] = ["diff", "--unified={}".format(ctx)]
        # Assessment cluster 5 #4: a `key` like "-rf" used to reach `git diff` as
        # a flag (no shell, so no command injection — but git interprets it as an
        # option = flag smuggling). Refs/ranges never legitimately start with
        # '-', and `paths` is already separated by `--`. Reject loud rather than
        # let git silently do something unintended.
        if key:  # a ref or range, e.g. "HEAD" or "main..HEAD"; empty = working tree
            if key.startswith("-"):
                raise ValueError(
                    "git ref/range {!r} may not start with '-' (flag-injection guard)".format(key))
            args.append(key)
        if paths:
            args += ["--"] + list(paths)
        rc, text = await self._git(repo, args)
        # `git diff` (no --exit-code) returns 0 even with changes; non-zero = a real
        # error (not a repo / bad ref / no git). Raise so the reviewer never reviews
        # git's error text mistaken for a diff (bug review M2).
        if rc:
            raise RuntimeError("git diff failed (rc={}): {}".format(rc, text.strip()))
        return text

    async def _git(self, repo: Optional[str], args: List[str]):
        cmd = ["git"] + (["-C", repo] if repo else []) + args
        proc = await self._spawn(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            raise
        return proc.returncode, out.decode(errors="replace")
