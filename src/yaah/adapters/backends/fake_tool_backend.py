"""FakeToolBackend — scripted ToolBackend for testing AgentLoopNode without an LLM.

Used by: the spike/yaah-as-harness example, plus any future tests of the agent
loop. Drives the loop via a list of canned turn responses, e.g.:
    [{"calls": [{"name": "read", "args": {"path": "foo"}, "id": "1"}]},
     {"text": "Done."}]

Where: adapters (the loop is in adapters; its fake backend is too).
Why: proves REPLACEABILITY of the backend seam — the loop runs against scripted
responses the same way it would run against ClaudeCliBackend with stream-json
parsing or a future Anthropic-API backend. If the fake works, the protocol is
right; if a real backend doesn't, the bug is in that backend's translation,
not in the loop.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


class FakeToolBackend:
    def __init__(self, *, turns: Sequence[Dict[str, Any]]) -> None:
        # Each turn is one of:
        #   {"text": "..."}                        -> final answer
        #   {"calls": [{"name": ..., "args": ..., "id": ...}, ...]}  -> tool calls
        #   {"text": "...", "calls": [...]}        -> assistant said something AND called tools
        self._turns: List[Dict[str, Any]] = list(turns)
        self._cursor = 0

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        # Satisfies ModelBackend.complete for backends that also drive non-loop stages.
        # The scripted turns are loop-shaped; complete() returns the first text-bearing turn.
        for spec in self._turns:
            if "text" in spec:
                return str(spec["text"])
        return ""

    async def turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
        if self._cursor >= len(self._turns):
            # Out of script — return an empty final so the loop terminates cleanly.
            return {"text": "(fake: scripted turns exhausted)"}
        spec = self._turns[self._cursor]
        self._cursor += 1
        return dict(spec)  # defensive copy so the loop can't mutate our script
