"""run_tool_loop — drive a tool-capable backend through model-initiated calls.

Used by: Agent.invoke when an agent has `tools` and its backend supports tool
calls. Backend-agnostic: it talks to the backend through one method, `turn`.
Where: inside one agent's invoke() — invisible to the harness (the orchestrator
sees only the agent's final output).
Why: keep the loop, the Tool spec, and the `call_target` resolver shared; only
`turn` is backend-specific (litellm function-calling, a scripted test backend,
etc.). claude does NOT use this — it runs its own native tool-loop.

The backend contract:
  await backend.turn(messages, tool_schemas, *, model, **opts) ->
     {"text": str}                          # final answer; loop returns it
     {"calls": [{"id","name","args"}, ...]}  # run these tools, then turn again

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, List, Optional

from ..external_call import call_target
from ..trace import NullTracer, Span
from .tool import Tool


async def run_tool_loop(backend: Any, prompt: str, tools: List[Tool], *, comms: Any = None,
                        model: Optional[str] = None, max_iters: int = 8,
                        tracer: Any = None, corr: str = "", parent: Optional[str] = None,
                        **opts: Any) -> str:
    by_name = {t.name: t for t in tools}
    schemas = [t.to_function_schema() for t in tools]
    messages: List[dict] = [{"role": "user", "content": prompt}]
    tracer = tracer or NullTracer()

    for _ in range(max_iters):
        turn = await backend.turn(messages, schemas, model=model, **opts)
        if "text" in turn and turn["text"] is not None:
            return turn["text"]
        # Defensive (assessment cluster 3 B3): a malformed call structure
        # (missing `name`, not a dict, etc.) is FILTERED before subscripting —
        # a backend that returns garbage shouldn't crash the agent with KeyError.
        # Unknown tool NAMES are still handled below (with an "unknown tool" result).
        raw_calls = turn.get("calls") or []
        calls = [c for c in raw_calls if isinstance(c, dict) and c.get("name")]
        if not calls:
            return turn.get("text") or ""
        # Record the assistant's tool-call turn in the OpenAI/litellm WIRE shape
        # (bug review H3) — the internal {id,name,args} the backend returned is NOT
        # what the provider expects back in history; turn 2 would send a malformed
        # message. The `tool` result messages below are already wire-shaped.
        messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": c.get("id", c["name"]), "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))}}
            for c in calls]})
        for call in calls:
            tool = by_name.get(call["name"])
            t0 = time.monotonic()
            if tool is None:
                result: Any = {"error": "unknown tool {!r}".format(call["name"])}
                status = "error"
            else:
                # A raising tool impl must NOT abort the agent's invoke
                # (assessment #10): the model gets the error as the tool result
                # and decides — retry with different args, route around it, or
                # answer without the tool. CancelledError stays a cancellation.
                try:
                    if callable(tool.impl):
                        # a per-invocation handler (e.g. envelope_get bound to THIS
                        # envelope) — closures can't be a call_target string, so a
                        # callable impl is invoked directly. May be sync or async.
                        import inspect
                        res = tool.impl(call.get("args", {}))
                        result = await res if inspect.isawaitable(res) else res
                    else:
                        result = await call_target(tool.impl, call.get("args", {}), comms=comms)
                    status = "ok"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    result = {"error": "tool {!r} failed: {!r}".format(call["name"], e)}
                    status = "error"
            t1 = time.monotonic()
            # tool_call span (R3): only carried when the `tools` capture is on
            await tracer.emit(Span.timed(
                "tool_call", corr=corr, parent=parent, t0=t0, t1=t1,
                tool=call["name"], status=status))
            messages.append({"role": "tool", "tool_call_id": call.get("id", call["name"]),
                             "name": call["name"],
                             "content": result if isinstance(result, str) else json.dumps(result)})
    raise RuntimeError("tool loop exceeded max_iters ({})".format(max_iters))
