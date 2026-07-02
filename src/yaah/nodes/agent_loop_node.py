"""AgentLoopNode — bounded tool-use loop with author-declared tool catalog.

Used by: yaah.build (the 'agent_loop' node type). Wraps a tool-capable backend
in a turn-by-turn loop where the agent emits tool calls and the harness
dispatches them via the same `call_target` resolver transforms use.
Where: a stage that needs the harness shape (agent emits tool call → harness
dispatches → agent observes → loop). Sibling to `agent` (the one-shot stage).
Why: a backend can take a turn (`turn(messages, tools)` over its stream), but
nothing drove a loop against it. This is the missing primitive. Workers-not-
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
`http(s):` is an HTTP call.

B8 (2026-06-22): the inline tool-use loop was deleted. The dict catalog is
converted to `Tool` instances at construction, and `invoke()` delegates the
whole loop to `run_tool_loop(..., return_meta=True)`. All the hardening
(OpenAI/litellm wire shape, tracer span emission, malformed-call filter,
callable-impl branch, CancelledError re-raise) is now shared with the
original `Agent` class — one canonical loop, two node shapes.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core import Node, Envelope, Kind, NodeConfig
from ..agents.tool import Tool
from ..agents.tool_loop import run_tool_loop


class AgentLoopNode(Node):
    def __init__(
        self,
        *,
        backend: Any,                          # has `.turn(messages, tools)`
        tools: Dict[str, Dict[str, Any]],      # name -> {description, input_schema, dispatch}
        comms: Any = None,                     # required if any tool uses `node:` dispatch
        prompt_source: Any = None,             # required if system_prompt is a 'file:' ref
        max_turns: int = 10,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        # The loop drives the backend via stream() (preferred) or turn()
        # (fallback) — see run_tool_loop._fetch_turn. Either capability is
        # enough; a complete()-only backend can't drive a tool loop.
        if not (hasattr(backend, "stream") or hasattr(backend, "turn")):
            raise TypeError(
                "AgentLoopNode needs a backend the tool loop can drive — one with "
                "`.stream(context)` or `.turn(messages, tools)`. Got {!r}, which has "
                "neither (only .complete()). Either swap the backend or use a plain "
                "`agent` node for one-shot stages.".format(type(backend).__name__))
        # Validate dispatch BEFORE the Tool-comprehension below, so a missing
        # 'dispatch' surfaces as ValueError (the documented contract) instead
        # of a KeyError on `spec["dispatch"]`.
        for name, spec in tools.items():
            if "dispatch" not in spec:
                raise ValueError(
                    "tool {!r} in agent_loop catalog is missing 'dispatch' "
                    "(e.g. 'fn:mymodule:myfunc' or 'node:my_role')".format(name))
            # MED-011: a non-dict input_schema (author typo like "object" instead
            # of {"type": "object"}) would be accepted here and deferred-crash deep
            # in the provider with an opaque error. Fail fast at construction.
            sch = spec.get("input_schema")
            if sch is not None and not isinstance(sch, dict):
                raise ValueError(
                    "tool {!r} has a non-dict input_schema ({!r}) — it must be a "
                    "JSON Schema object, e.g. {{'type': 'object', 'properties': {{...}}}}"
                    .format(name, type(sch).__name__))
        self._backend = backend
        # Convert the dict catalog to Tool instances once at construction.
        # Tool.impl accepts a string (a call_target target) — run_tool_loop
        # routes fn:/node:/http: through the same resolver transforms use.
        # NOTE: Tool's field is `schema` (not `input_schema`) — the dict-key
        # name is the author-facing label; the dataclass field is internal.
        self._tools = [
            Tool(name=name,
                 description=spec.get("description", ""),
                 schema=spec.get("input_schema", {"type": "object", "properties": {}}),
                 impl=spec["dispatch"])
            for name, spec in tools.items()
        ]
        self._comms = comms
        self._prompt_source = prompt_source
        self._max_turns = max_turns
        self._system = system_prompt
        self._model = model
        self._resolved_system: Optional[str] = None    # lazy-resolved on first invoke

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        goal = input.payload.get("goal") or input.payload.get("input") or ""
        system = await self._system_text()
        # B8: delegate to the canonical loop. The system prompt becomes a
        # system-role message (OpenAI/Anthropic convention); the goal becomes
        # the initial user message; the cursor + tool dispatch + tracer wiring
        # live in run_tool_loop.
        answer, meta = await run_tool_loop(
            self._backend,
            tools=self._tools,
            messages=[{"role": "user", "content": goal}],
            system=system,
            comms=self._comms,
            model=self._model,
            max_iters=self._max_turns,
            return_meta=True,
            corr=input.correlation_id or "",
        )
        return input.reply_with(Kind.RESULT, {
            **input.payload,
            "answer": answer,
            "turns": meta["turns"],
            "outcome": meta["outcome"],
        })

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
