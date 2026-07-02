"""FakeProvider — a deterministic, offline ApiProvider for tests and the PoC.

Used by: tests, examples, and the runtime's `fake` provider.
Where: anywhere a real model isn't wanted (CI, local dev).
Why: return scripted responses in turn (then repeat the last), so the retry
loop and pipelines run reproducibly with no network.

A native ApiProvider: `stream()` is its only completion method (the one model
seam). Callers that want collected text go through the module-level
`api_provider.complete()`, which drives stream() — so there is a single
response cursor / state machine, no second path to keep in sync.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional, Sequence

from . import api_provider as _ap


class FakeProvider(_ap.ApiProvider):
    def __init__(self, responses: Optional[Sequence[str]] = None, default: str = "") -> None:
        self._responses: List[str] = list(responses or [])
        self._default = default
        self._i = 0

    def stream(self, context: _ap.Context, **opts: Any) -> AsyncIterator[_ap.StreamEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_ap.StreamEvent]:
        yield {"type": "start"}
        if self._i < len(self._responses):
            text = self._responses[self._i]
            self._i += 1
        else:
            text = self._default
        if text:
            yield {"type": "text_delta", "delta": text}
        yield {"type": "done", "stop_reason": "end_turn"}
