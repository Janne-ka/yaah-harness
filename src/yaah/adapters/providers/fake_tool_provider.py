"""FakeToolProvider — scripted ApiProvider for testing AgentLoopNode without an LLM.

Used by: the spike/yaah-as-harness example, plus tests of the agent loop.
Drives the loop via a list of canned turn responses, e.g.:
    [{"calls": [{"name": "read", "args": {"path": "foo"}, "id": "1"}]},
     {"text": "Done."}]

Where: adapters (the loop is in adapters; its fake backend is too).
Why: proves REPLACEABILITY of the backend seam — the loop runs against scripted
responses the same way it would run against ClaudeCliProvider with stream-json
parsing or a future Anthropic-API backend. If the fake works, the protocol is
right; if a real backend doesn't, the bug is in that backend's translation,
not in the loop.

A native ApiProvider: `stream()` walks the script one turn at a time, emitting
start → optional text_delta → zero or more toolcall_end → done. `turn()` is
kept as the tool-loop entry (the capability marker AgentLoopNode / `supports_turn`
key on). Collected-text callers use the module-level `api_provider.complete()`.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from ...agents.api_provider import ApiProvider, Context, StreamEvent, SupportsTurn, turn as collect_turn


class FakeToolProvider(ApiProvider, SupportsTurn):
    def __init__(self, *, turns: Sequence[Dict[str, Any]]) -> None:
        # Each turn is one of:
        #   {"text": "..."}                        -> final answer
        #   {"calls": [{"name": ..., "args": ..., "id": ...}, ...]}  -> tool calls
        #   {"text": "...", "calls": [...]}        -> assistant said something AND called tools
        self._turns: List[Dict[str, Any]] = list(turns)
        self._cursor = 0

    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[StreamEvent]:
        yield {"type": "start"}
        if self._cursor >= len(self._turns):
            # Out of script — yield a synthetic final so the loop terminates cleanly.
            yield {"type": "text_delta", "delta": "(fake: scripted turns exhausted)"}
            yield {"type": "done", "stop_reason": "end_turn"}
            return
        spec = self._turns[self._cursor]
        self._cursor += 1
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

    async def turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
        # Kept as the tool-capability marker (Agent._supports_turn keys on `turn`);
        # the body delegates to the stream bridge like every collected shape.
        return await collect_turn(self, messages, tools, model=model, **opts)
