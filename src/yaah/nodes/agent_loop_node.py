"""AgentLoopNode — bounded tool-use loop with author-declared tool catalog.

Used by: yaah.build (the 'agent_loop' node type). Wraps a ToolBackend in a
turn-by-turn loop where the agent emits tool calls and the harness dispatches
them via the same `call_target` resolver transforms use — so a tool and a
transform run through one code path.
Where: a stage that needs the harness shape (agent emits tool call → harness
dispatches → agent observes → loop). Sibling to `agent` (the one-shot stage).
Why: YAAH had the `ToolBackend.turn(messages, tools)` PROTOCOL but no node
that drove a loop against it. This is the missing primitive. Workers-not-
citizens is preserved: the agent has agency only within the catalog the
AUTHOR declared, not within whatever the backend or an MCP server might
expose.

Tool catalog shape (author-declared in the stage config):
    "tools": {
      "<tool_name>": {
        "description": "...",                # shown to the model
        "input_schema": {...},                # JSON schema; model's API contract
        "dispatch": "fn:module:func"          # or "node:role" / "http(s)://..."
      },
      ...
    }

Dispatch goes through `call_target` (the same machinery transforms use): `fn:`
is a direct in-process call (microseconds), `node:` is a Comms request,
`http(s):` is an HTTP call. The author chooses per tool.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import Envelope, Kind
from ..external_call import call_target


class AgentLoopNode:
    def __init__(
        self,
        *,
        backend: Any,                          # ToolBackend — has `.turn(messages, tools)`
        tools: Dict[str, Dict[str, Any]],      # name -> {description, input_schema, dispatch}
        comms: Any = None,                     # required if any tool uses `node:` dispatch
        prompt_source: Any = None,             # required if system_prompt is a 'file:' ref
        max_turns: int = 10,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        if not hasattr(backend, "turn"):
            raise TypeError(
                "AgentLoopNode requires a ToolBackend (one with `.turn(messages, tools)`). "
                "Got {!r}, which only has .complete(). Either swap the backend or use a "
                "plain `agent` node for one-shot stages.".format(type(backend).__name__))
        for name, spec in tools.items():
            if "dispatch" not in spec:
                raise ValueError(
                    "tool {!r} in agent_loop catalog is missing 'dispatch' "
                    "(e.g. 'fn:mymodule:myfunc' or 'node:my_role')".format(name))
        self._backend = backend
        self._tools = tools
        self._comms = comms
        self._prompt_source = prompt_source
        self._max_turns = max_turns
        self._system = system_prompt
        self._model = model
        self._resolved_system: Optional[str] = None    # lazy-resolved on first invoke

    async def invoke(self, input_envelope: Envelope, config: Dict[str, Any]) -> Envelope:
        goal = input_envelope.payload.get("goal") or input_envelope.payload.get("input") or ""
        system = await self._system_text()
        messages: List[Dict[str, Any]] = [{"role": "user", "content": goal}]
        # Tool specs rendered ONCE per stage invocation, not per turn — cache-friendly.
        tool_specs = [
            {"name": name, "description": spec.get("description", ""),
             "input_schema": spec.get("input_schema", {"type": "object", "properties": {}})}
            for name, spec in self._tools.items()
        ]

        for turn_idx in range(self._max_turns):
            response = await self._backend.turn(
                messages, tool_specs,
                model=self._model,
                system=system,
            )
            if "text" in response and not response.get("calls"):
                # Agent emitted a final answer — done.
                return _result(input_envelope, response["text"], turn_idx + 1, "completed")
            calls = response.get("calls") or []
            if not calls:
                # Neither text nor calls — malformed turn; treat as final-empty.
                return _result(input_envelope, "", turn_idx + 1, "empty_response")
            messages.append({"role": "assistant", "content": response.get("text", ""), "calls": calls})
            tool_results: List[Dict[str, Any]] = []
            for call in calls:
                name = call.get("name") or ""
                args = call.get("args", {}) or {}
                call_id = call.get("id", "")
                if name not in self._tools:
                    tool_results.append({"id": call_id, "name": name,
                                          "content": "unknown tool {!r} — not in declared catalog "
                                                     "{}".format(name, sorted(self._tools)),
                                          "is_error": True})
                    continue
                target = self._tools[name]["dispatch"]
                try:
                    # Tool errors flow back as observations, not loop crashes — agent learns + adapts.
                    result = await call_target(target, args, comms=self._comms,
                                                reply_to=input_envelope)
                    tool_results.append({"id": call_id, "name": name,
                                          "content": str(result), "is_error": False})
                except Exception as exc:
                    tool_results.append({"id": call_id, "name": name,
                                          "content": "{}: {}".format(type(exc).__name__, exc),
                                          "is_error": True})
            messages.append({"role": "tool", "results": tool_results})

        return _result(input_envelope, "", self._max_turns, "max_turns_exhausted")

    async def _system_text(self) -> Optional[str]:
        if self._resolved_system is not None:
            return self._resolved_system
        s = self._system
        if isinstance(s, str) and s.startswith("file:"):
            if self._prompt_source is None:
                raise ValueError(
                    "agent_loop system_prompt uses 'file:' but no prompt_source "
                    "was passed to the node; pass prompt_source= in the builder.")
            s = await self._prompt_source.get(s[5:])
        self._resolved_system = s
        return s


def _result(input_env: Envelope, answer: str, turns: int, outcome: str) -> Envelope:
    return input_env.reply_with(Kind.RESULT, {**input_env.payload, "answer": answer,
                                                "turns": turns, "outcome": outcome})
