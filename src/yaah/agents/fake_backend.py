"""FakeBackend — a deterministic, offline ModelBackend for tests and the PoC.

Used by: tests, examples, and the runtime's `fake` provider.
Where: anywhere a real model isn't wanted (CI, local dev).
Why: return scripted responses in turn (then repeat the last), so the retry
loop and pipelines run reproducibly with no network.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence


class FakeBackend:
    def __init__(self, responses: Optional[Sequence[str]] = None, default: str = "") -> None:
        self._responses: List[str] = list(responses or [])
        self._default = default
        self._i = 0

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        if self._i < len(self._responses):
            out = self._responses[self._i]
            self._i += 1
            return out
        return self._default
