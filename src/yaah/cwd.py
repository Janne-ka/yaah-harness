"""cwd helpers — resolve & forward a repo-bound node's working directory.

Used by: the execution nodes that can run in a per-run worktree — Agent
(agents/agent.py) and the shell / get / post nodes (yaah/nodes/) — via build's
`cwd_from` config key.
Where: the domain layer (NOT the kernel). A worktree path is per-run DOMAIN data
carried in the Envelope PAYLOAD, never a kernel/transport header — so the kernel
stays unaware of it. These two functions are the one place that convention lives.
Why: every repo-bound node did the same two things by hand — read the cwd from
the payload, and re-emit the key so it survives `reply()`'s fresh payload (the
forwarding that, missed, already caused "workdir not propagating"). Centralised so
a new repo-bound node can't get it subtly wrong.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .core import Envelope


def resolve_cwd(input: Envelope, cwd_from: Optional[str],
                default: Optional[str] = None) -> Optional[str]:
    """The working directory for this run: the payload value at `cwd_from`, else
    the static `default`. When `cwd_from` is unset, `default` applies as before.
    But a DECLARED `cwd_from` whose key is absent AND with no static `default` is
    a contract violation, not a "use the process cwd" fallback: the stage was
    told its cwd comes from the payload, and running it in the launcher's cwd
    silently is how BUG-697 ran a repo-bound stage in the wrong directory. Fail
    loud — name the missing key — so the failure surfaces at THIS stage, not as a
    cryptic error in a relative command two stages later (fail-loud over silent
    cwd fallback)."""
    if not cwd_from:
        return default
    cwd = input.payload.get(cwd_from)
    if cwd is not None:
        return cwd
    if default is not None:
        return default
    raise ValueError(
        "cwd_from {!r} is declared but the payload has no such key and no static "
        "`cwd` is set — this repo-bound stage has no working directory. An "
        "upstream stage must put the path in payload[{!r}] (e.g. a `worktree` "
        "stage), or give the node a static `cwd`.".format(cwd_from, cwd_from))


def carry_cwd(input: Envelope, cwd_from: Optional[str]) -> Dict[str, Any]:
    """The `{cwd_from: path}` to fold into a node's reply so the worktree path
    reaches the next repo-bound stage (reply() starts a fresh payload, dropping
    it otherwise). Empty when there's nothing to forward. Nodes that carry the
    whole input payload forward (get/post/transform) don't need this."""
    if cwd_from:
        cwd = input.payload.get(cwd_from)
        if cwd is not None:
            return {cwd_from: cwd}
    return {}
