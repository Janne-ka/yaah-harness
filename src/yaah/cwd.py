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
    """The working directory for this run: the payload value at `cwd_from`, or
    `default` when cwd_from is unset or the key is absent. Callers that build an
    opts dict guard the result with `if cwd:` (no key → no cwd → process default);
    the shell nodes pass `default=self._cwd` so a static cwd still applies."""
    if not cwd_from:
        return default
    return input.payload.get(cwd_from, default)


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
