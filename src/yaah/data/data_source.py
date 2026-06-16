"""DataSource — the interface for fetching DATA by key (the 'get' seam).

Used by: GetNode (the 'get' node type) and the runtime (builds one from the root
config's `data_sources`). Implemented by: GitDiff / File / Routing sources.
Where: the seam where a stage's input data comes from — a worktree diff, a file
slice, a cloud blob — instead of being passed whole down the chain.
Why: the prompt layer has a pluggable `get` for prompts; this is the same idea
for data. A review/eval stage should read only the changed lines (± N context),
not whole files — what to fetch and how much is config, not code. Mirrors
PromptSource.get so the two layers feel the same.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataSource(Protocol):
    async def fetch(self, key: str, **opts: Any) -> str:
        ...
