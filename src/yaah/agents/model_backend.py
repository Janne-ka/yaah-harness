"""ModelBackend / ToolBackend — the interface an Agent calls to talk to a model.

Used by: Agent (its body calls backend.complete or, for tool-loop stages,
backend.turn). Implemented by: FakeBackend, ScriptedBackend, ClaudeCliBackend,
LiteLLMBackend, ScriptedToolBackend, RoutingBackend.
Where: the provider-agnostic seam (design §7).
Why: one interface so the model/provider is a construction- or config-time
choice, never hardcoded in the Agent.

Two-method contract, both runtime_checkable Protocols (assessment elegance #3):
- `ModelBackend.complete(prompt) -> str` — every backend has this.
- `ToolBackend(ModelBackend).turn(messages, tools) -> {text|calls}` — adds
  function-calling capability. Previously `turn` lived only as a docstring
  in tool_loop.py and was duck-typed via `hasattr(backend, "turn")` in the
  Agent. Promoting it to the type system makes the capability explicit and
  the structural check (`isinstance(backend, ToolBackend)`) replaces the
  scattered hasattr. Backends without function-calling stay valid by not
  implementing `turn` — the same runtime branch the Agent already takes.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ModelBackend(Protocol):
    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        ...


@runtime_checkable
class ToolBackend(ModelBackend, Protocol):
    async def turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
        ...
