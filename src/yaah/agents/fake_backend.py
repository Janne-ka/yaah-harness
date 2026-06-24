"""FakeBackend — a deterministic, offline ApiProvider for tests and the PoC.

Used by: tests, examples, and the runtime's `fake` provider.
Where: anywhere a real model isn't wanted (CI, local dev).
Why: return scripted responses in turn (then repeat the last), so the retry
loop and pipelines run reproducibly with no network.

After B2 step 1 (provider unification): this is now a native ApiProvider —
the canonical method is `stream()`, and `complete()` is a thin wrapper that
delegates to the module-level helper so legacy callers keep working until
B6 removes the wrapper. Both paths advance the SAME response cursor (one
shared state machine), so tests can mix them without surprise.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional, Sequence

from . import api_provider as _ap


class FakeBackend:
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

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        return await _ap.complete(self, prompt, model=model, **opts)
