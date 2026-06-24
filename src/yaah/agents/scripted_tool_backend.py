"""ScriptedToolBackend — a deterministic tool-capable ApiProvider for tests.

Used by: tests of the agent tool-loop (and demos). `stream()` walks a canned
sequence of turn results (tool calls, then a final answer), so the loop runs
offline with no model and no network.
Where: anywhere a real function-calling model isn't wanted.
Why: prove the tool loop end to end — the model "decides" to call a tool,
the loop executes the tool's impl, feeds the result back, and the model
"answers".

Exhaustion behavior is unified across the offline backends (assessment
cluster 3 B2): same `on_exhaustion` knob as ScriptedBackend — `"default"`
(yield a synthetic final text equal to self._default, matching FakeBackend's
"default" shape), `"raise"` (IndexError — raises through naturally; error
events are for soft errors, exhaustion is exceptional), or `"repeat_last"`
(legacy). Default is `"default"`.

After B2.4 (provider unification): native ApiProvider. `stream()` is the
canonical method; `complete()` / `turn()` are thin wrappers (the legacy
`complete()` always returned `self._default` regardless of script — that
behavior is preserved since the script is loop-shaped).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, List, Optional, Sequence

from . import api_provider as _ap


class ScriptedToolBackend:
    def __init__(self, turns: Sequence[dict], default: str = "",
                 *, on_exhaustion: str = "default") -> None:
        if on_exhaustion not in ("default", "raise", "repeat_last"):
            raise ValueError(
                "on_exhaustion must be 'default'|'raise'|'repeat_last', got {!r}"
                .format(on_exhaustion))
        # each item is a turn result: {"calls": [...]} or {"text": "..."}
        self._turns: List[dict] = list(turns)
        self._i = 0
        self._default = default
        self._on_exhaustion = on_exhaustion

    def stream(self, context: _ap.Context, **opts: Any) -> AsyncIterator[_ap.StreamEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_ap.StreamEvent]:
        yield {"type": "start"}
        spec = self._next_turn()  # may raise IndexError on exhaustion='raise'
        text = spec.get("text")
        calls = spec.get("calls") or []
        if text:
            yield {"type": "text_delta", "delta": str(text)}
        for call in calls:
            if not isinstance(call, dict) or not call.get("name"):
                continue
            yield {"type": "toolcall_end",
                   "id": call.get("id", call.get("name", "")),
                   "name": call.get("name", ""),
                   "args": call.get("args", {}) or {}}
        yield {"type": "done", "stop_reason": "tool_use" if calls else "end_turn"}

    def _next_turn(self) -> dict:
        """Shared cursor logic — same algorithm legacy turn() used so
        on_exhaustion semantics are identical through both stream() and turn()."""
        if self._i < len(self._turns):
            out = self._turns[self._i]
            self._i += 1
            return out
        if self._on_exhaustion == "raise":
            raise IndexError(
                "ScriptedToolBackend exhausted (turns played: {})".format(len(self._turns)))
        if self._on_exhaustion == "repeat_last":
            return self._turns[-1] if self._turns else {"text": self._default}
        return {"text": self._default}

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        # Tool-less `complete()` returns self._default and does NOT touch the
        # script — the stream/turn path consumes script turns (which are
        # loop-shaped), so keeping complete() script-free stops non-loop
        # callers from accidentally consuming a tool turn.
        return self._default

    async def turn(self, messages: List[dict], tools: List[dict], *,
                   model: Optional[str] = None, **opts: Any) -> dict:
        return await _ap.turn(self, messages, tools, model=model, **opts)
