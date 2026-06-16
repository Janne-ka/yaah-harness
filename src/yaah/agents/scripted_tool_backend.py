"""ScriptedToolBackend — a deterministic tool-capable backend for tests.

Used by: tests of the agent tool-loop (and demos). Implements `turn` by replaying
a canned sequence of turn results (tool calls, then a final answer), so the loop
runs offline with no model and no network.
Where: anywhere a real function-calling model isn't wanted.
Why: prove run_tool_loop end to end — the model "decides" to call a tool, the
loop executes the tool's impl, feeds the result back, and the model "answers".

Exhaustion behavior is unified across the offline backends (assessment cluster
3 B2): same `on_exhaustion` knob as ScriptedBackend — `"default"` (return
{"text": self._default}, matching FakeBackend's "default" shape), `"raise"`
(IndexError), or `"repeat_last"` (legacy). Default is `"default"`.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence


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

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        return self._default  # also a plain ModelBackend, for tool-less use

    async def turn(self, messages: List[dict], tools: List[dict], *,
                   model: Optional[str] = None, **opts: Any) -> dict:
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
