"""ShellCheck — a validator that runs a command and passes on the expected exit.

Used by: yaah.build (the 'shell_check' node type) as a stage validator. The
GREEN/qa gate: tests must pass → expect_exit=0 (the default). The RED gate:
tests must fail before code exists → expect_nonzero=True. cwd_from reads the
per-run worktree path from the payload (the command stays trusted config).
target_from names ONE payload key whose validated value(s) are APPENDED to the
trusted command as additional argument(s) — the controlled exception that lets
the gate run against the path the agent actually wrote (e.g. the test file it
created) instead of a pre-guessed one. Strict path-shape validation; any invalid
value ERRORS the node (never runs the gate against the wrong/no target). Unset =
old behaviour; key absent = command unchanged (see _target).
Where: validator slots in a stage (e.g. qa on the code stage = the refix loop).
Why: turn a command's exit code into a Verdict the harness retry loop understands.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import List, Optional, Union

from ..core import Node, Envelope, Failure, NodeConfig, Verdict
from ..cwd import resolve_cwd
from ._shell import _run, ShellTimeout
from ._target import append_targets, resolve_target_args


class ShellCheck(Node):
    def __init__(self, command: Union[str, List[str]], *, expect_exit: int = 0,
                 expect_nonzero: bool = False, cwd: Optional[str] = None,
                 cwd_from: Optional[str] = None, timeout: Optional[float] = None,
                 shell: bool = False, target_from: Optional[str] = None) -> None:
        self._command = command
        self._expect = expect_exit
        self._expect_nonzero = expect_nonzero
        self._cwd = cwd
        self._cwd_from = cwd_from
        self._timeout = timeout
        self._shell = shell
        # payload key whose validated value(s) append to the trusted command as
        # additional argument(s); None = the command never changes (see _target).
        self._target_from = target_from

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        cwd = resolve_cwd(input, self._cwd_from, self._cwd)  # per-run worktree, else static cwd
        timeout = config.timeout if config.timeout is not None else self._timeout  # #13
        # Append the validated payload target(s) to the trusted command (raises →
        # the node ERRORS; never runs the gate with a wrong/absent target).
        command = append_targets(self._command, resolve_target_args(input.payload, self._target_from))
        try:
            code, text = await _run(command, cwd=cwd, timeout=timeout, shell=self._shell)
        except ShellTimeout as t:
            # a timeout is NEVER a pass — not even for an expect_nonzero (RED) gate,
            # which would otherwise read a hang as 'tests failed as required'.
            return Verdict.failed(Failure("shell_timeout", t.note, "")).to_envelope(input)
        ok = (code != 0) if self._expect_nonzero else (code == self._expect)
        if ok:
            return Verdict.passed().to_envelope(input)
        want = "nonzero" if self._expect_nonzero else self._expect
        return Verdict.failed(Failure(
            "shell_exit", "exit {} != expected {}".format(code, want), text[-2000:]
        )).to_envelope(input)
