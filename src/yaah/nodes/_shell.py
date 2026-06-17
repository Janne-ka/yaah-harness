"""_run — shared subprocess helper for the shell nodes.

Used by: ShellNode and ShellCheck.
Where: yaah.nodes only (private helper, not exported).
Why: one place to run a command and capture (exit_code, combined output),
honouring cwd / timeout / shell mode.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import shlex
from typing import List, Optional, Tuple, Union

# Default ceiling when no `timeout` is configured. An UNSET timeout used to be
# None → asyncio.wait_for(..., None) → a hung command (a wedged test runner, a git
# op blocked on a lock prompt) wedged the WHOLE run forever, with no span and no
# progress. Generous (a big suite finishes well inside 20 min) but never infinite;
# override per stage/node via `timeout`.
_DEFAULT_SHELL_TIMEOUT = 1200.0


class ShellTimeout(Exception):
    """A shell command exceeded its timeout. The shell nodes CATCH this and turn
    it into a structured result/verdict — distinct from a real nonzero exit, so a
    RED gate (`expect_nonzero`) can't mistake a hang for 'tests failed' — instead
    of letting a bare TimeoutError ride the generic error path."""
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        # communicate() was cancelled by wait_for, so buffered output is not
        # recoverable — the load-bearing fact is simply that it timed out.
        self.note = "command exceeded {:.0f}s timeout (killed; output not captured)".format(seconds)
        super().__init__(self.note)


async def _run(command: Union[str, List[str]], *, cwd: Optional[str], timeout: Optional[float],
               shell: bool) -> Tuple[int, str]:
    eff = timeout if timeout is not None else _DEFAULT_SHELL_TIMEOUT
    if shell:
        # Assessment cluster 4 #3: a list passed with shell=True used to
        # `" ".join(command)` — silently dropping quoting so `["echo", "hello
        # world"]` became `echo hello world` (two tokens). `shlex.quote` preserves
        # each element's intent so list+shell behaves predictably. The shell
        # is still trusted-config; this fixes the silent quoting footgun, not a
        # payload-injection vector (cwd is the only payload-derived input and it
        # is passed via kwarg, not concatenated).
        cmd = command if isinstance(command, str) else " ".join(shlex.quote(a) for a in command)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    else:
        args = shlex.split(command) if isinstance(command, str) else list(command)
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=eff)
    except asyncio.TimeoutError:
        # wait_for cancels the await but leaves the child running — kill and reap
        # it so timed-out test/qa steps don't leak an orphan + its stdout pipe.
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        raise ShellTimeout(eff)
    return proc.returncode, out.decode(errors="replace")
