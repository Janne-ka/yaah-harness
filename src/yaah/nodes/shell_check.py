"""ShellCheck — a validator that runs a command and passes on the expected exit.

Used by: yaah.build (the 'shell_check' node type) as a stage validator. The
GREEN/qa gate: tests must pass → expect_exit=0 (the default). The RED gate:
tests must fail before code exists → expect_nonzero=True. cwd_from reads the
per-run worktree path from the payload (the command stays trusted config).
Where: validator slots in a stage (e.g. qa on the code stage = the refix loop).
Why: turn a command's exit code into a Verdict the harness retry loop understands.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import List, Optional, Union

from ..core import Envelope, Failure, NodeConfig, Verdict
from ..cwd import resolve_cwd
from ._shell import _run


class ShellCheck:
    def __init__(self, command: Union[str, List[str]], *, expect_exit: int = 0,
                 expect_nonzero: bool = False, cwd: Optional[str] = None,
                 cwd_from: Optional[str] = None, timeout: Optional[float] = None,
                 shell: bool = False) -> None:
        self._command = command
        self._expect = expect_exit
        self._expect_nonzero = expect_nonzero
        self._cwd = cwd
        self._cwd_from = cwd_from
        self._timeout = timeout
        self._shell = shell

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        cwd = resolve_cwd(input, self._cwd_from, self._cwd)  # per-run worktree, else static cwd
        timeout = config.timeout if config.timeout is not None else self._timeout  # #13
        code, text = await _run(self._command, cwd=cwd, timeout=timeout, shell=self._shell)
        ok = (code != 0) if self._expect_nonzero else (code == self._expect)
        if ok:
            return Verdict.passed().to_envelope(input)
        want = "nonzero" if self._expect_nonzero else self._expect
        return Verdict.failed(Failure(
            "shell_exit", "exit {} != expected {}".format(code, want), text[-2000:]
        )).to_envelope(input)
