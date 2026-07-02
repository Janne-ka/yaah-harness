"""PromptSource — the interface for fetching a prompt by key.

Used by: Agent (fetches its template via a source) and the runtime (builds one
from the root config's `prompt_sources`). Implemented by: Static/File/Http/
Langfuse/Routing sources.
Where: the seam where a node's prompt comes from file / cloud / Langfuse.
Why: one interface so the prompt store is config, not code.

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PromptSource(Protocol):
    @abstractmethod
    async def get(self, key: str, **opts: Any) -> str:
        ...
