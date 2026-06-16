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


async def _run(command: Union[str, List[str]], *, cwd: Optional[str], timeout: Optional[float],
               shell: bool) -> Tuple[int, str]:
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
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # wait_for cancels the await but leaves the child running — kill and reap
        # it so timed-out test/qa steps don't leak an orphan + its stdout pipe.
        proc.kill()
        try:
            await proc.wait()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode, out.decode(errors="replace")
