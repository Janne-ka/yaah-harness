"""ShellNode — a worker that runs a fixed command and returns its outcome.

Used by: yaah.build (the 'shell' node type) for non-LLM steps (e.g. qa/test
runs, git via a command).
Where: stages that shell out.
Why: run a command from trusted config (NOT from the payload) and pass
{exit_code, ok, stdout, stdout_tail} onward.

The COMMAND is always trusted config. The working directory may come from the
payload (cwd_from) — a per-run worktree path is data, not code, so a repo-bound
stage runs in the task's worktree without baking the path into config.
tail_only drops the full stdout from the payload (keeps stdout_tail) so a chatty
test run doesn't ride multi-MB of output through every downstream hop.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import List, Optional, Union

from ..core import Node, Envelope, Kind, NodeConfig
from ..cwd import carry_cwd, resolve_cwd
from ._shell import _run, ShellTimeout


class ShellNode(Node):
    def __init__(self, command: Union[str, List[str]], *, cwd: Optional[str] = None,
                 cwd_from: Optional[str] = None, timeout: Optional[float] = None,
                 shell: bool = False, tail_only: bool = False, tail: int = 2000,
                 carry: Optional[List[str]] = None) -> None:
        self._command = command
        self._cwd = cwd
        self._cwd_from = cwd_from
        self._timeout = timeout
        self._shell = shell
        self._tail_only = tail_only
        self._tail = tail
        # payload keys to forward (a shell node otherwise emits only its result
        # fields) — e.g. carry the spec through a repo-bound RED run to the coder
        self._carry = list(carry or [])

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        cwd = resolve_cwd(input, self._cwd_from, self._cwd)  # per-run worktree, else static cwd
        timeout = config.timeout if config.timeout is not None else self._timeout  # #13
        try:
            code, text = await _run(self._command, cwd=cwd, timeout=timeout, shell=self._shell)
            fields = {"exit_code": code, "ok": (code == 0), "stdout_tail": text[-self._tail:]}
            if not self._tail_only:
                fields["stdout"] = text
        except ShellTimeout as t:
            # a hang is a FAILURE, never exit 0 — surface it structurally (a
            # distinct `timed_out` marker + exit 124) so the gate/downstream can
            # tell a timeout apart from a real nonzero exit.
            fields = {"exit_code": 124, "ok": False, "timed_out": True, "stdout_tail": t.note}
        # Forward the worktree path (so the next repo-bound stage stays in it) and
        # any explicitly-carried payload keys (e.g. the spec, on through to code).
        fields.update(carry_cwd(input, self._cwd_from))
        fields.update({k: input.payload[k] for k in self._carry if k in input.payload})
        return input.reply_with(Kind.RESULT, fields)
