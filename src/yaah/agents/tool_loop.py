"""run_tool_loop — drive a tool-capable backend through model-initiated calls.

Used by: Agent.invoke when an agent has `tools` and its backend supports tool
calls. Backend-agnostic.
Where: inside one agent's invoke() — invisible to the harness (the orchestrator
sees only the agent's final output).
Why: keep the loop, the Tool spec, and the `call_target` resolver shared; the
backend is the only swap point (litellm function-calling, a scripted test
backend, etc.). claude does NOT use this — it runs its own native tool-loop.

The backend contract (MED-002, 2026-06-23): the loop consumes the streaming
`ApiProvider` shape when available —

  async for ev in backend.stream({"messages", "tools", "model"}, **opts):
     ev["type"] in {"start","text_delta","toolcall_end","done","error"}

forwarding each StreamEvent to the optional `on_event` callback BEFORE
assembling it into the collected {text, calls} shape. That callback is the
seam the whole ApiProvider protocol was built for (H7 advisory watchers,
per-token tracing) — before MED-002 the events were assembled inside
backend.turn() and discarded, unreachable from any consumer.

Backends WITHOUT `.stream()` (turn-only test doubles, any external legacy
backend) fall back to the collected `.turn()` contract:

  await backend.turn(messages, tool_schemas, *, model, **opts) ->
     {"text": str} | {"calls": [{"id","name","args"}, ...]}

on the fallback path `on_event` simply never fires.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ..external_call import call_target
from ..trace import NullTracer, Span
from .tool import Tool


async def _fetch_turn(backend: Any, messages_list: List[dict], schemas: List[dict],
                      model: Optional[str], on_event: Optional[Callable[[dict], Any]],
                      opts: Dict[str, Any]) -> Tuple[Optional[str], List[dict]]:
    """Get one turn's (text, raw_calls) from the backend.

    Streaming backends (have `.stream`) are consumed event-by-event, forwarding
    each StreamEvent to `on_event` (the MED-002 seam), then assembling the
    collected shape. Turn-only backends fall back to `.turn()` (on_event unused).
    """
    if hasattr(backend, "stream"):
        ctx: Dict[str, Any] = {"messages": messages_list, "tools": schemas}
        if model is not None:
            ctx["model"] = model
        text_parts: List[str] = []
        raw_calls: List[dict] = []
        async for ev in backend.stream(ctx, **opts):
            if on_event is not None:
                r = on_event(ev)
                if inspect.isawaitable(r):
                    await r
            etype = ev.get("type")
            if etype == "text_delta":
                text_parts.append(ev.get("delta", ""))
            elif etype == "toolcall_end":
                raw_calls.append({"id": ev.get("id", ""), "name": ev.get("name", ""),
                                  "args": ev.get("args", {}) or {}})
            elif etype == "error":
                # Match the legacy turn() path (assemble_message raised on error):
                # a backend stream error aborts the loop so the stage fails loudly.
                raise RuntimeError("backend stream error: {}".format(ev.get("message", "")))
        return ("".join(text_parts) if text_parts else None), raw_calls
    # Legacy turn-only backend.
    turn = await backend.turn(messages_list, schemas, model=model, **opts)
    return turn.get("text"), (turn.get("calls") or [])


async def run_tool_loop(backend: Any, prompt: str = "",
                        tools: Optional[List[Tool]] = None, *, comms: Any = None,
                        model: Optional[str] = None, max_iters: int = 8,
                        messages: Optional[List[dict]] = None,         # B8
                        system: Optional[str] = None,                  # B8
                        return_meta: bool = False,                     # B8
                        on_event: Optional[Callable[[dict], Any]] = None,   # MED-002
                        tracer: Any = None, corr: str = "", parent: Optional[str] = None,
                        **opts: Any) -> Union[str, Tuple[str, Dict[str, Any]]]:
    """Drive a tool-capable backend through model-initiated tool calls.

    B8 (2026-06-22) added three backward-compatible kwargs:
    - `messages`: caller pre-builds the conversation (used by AgentLoopNode);
      when None, build the legacy `[{user: prompt}]` single-message list.
    - `system`: prepend a system-role message to the conversation. Replaces
      the older habit of passing `system=` to `backend.turn(**opts)` which
      LiteLLM rejected (OpenAI shape wants system in the messages list).
    - `return_meta`: when True, return `(text, {turns, outcome})` instead of
      bare str — exhaustion becomes outcome="max_turns_exhausted" instead of
      RuntimeError. Legacy callers (Agent.invoke) pass none of these and see
      identical behavior to pre-B8.
    """
    tools = list(tools or [])
    by_name = {t.name: t for t in tools}
    schemas = [t.to_function_schema() for t in tools]
    if messages is None:
        messages_list: List[dict] = [{"role": "user", "content": prompt}]
    else:
        messages_list = list(messages)
    if system is not None:
        messages_list = [{"role": "system", "content": system}] + messages_list
    tracer = tracer or NullTracer()

    turns_done = 0
    for _ in range(max_iters):
        turns_done += 1
        # MED-002: consume the streaming ApiProvider shape (forwarding each
        # event to on_event) when the backend supports it; fall back to the
        # collected turn() shape otherwise. _fetch_turn returns the same
        # (text, raw_calls) the inline `backend.turn()` used to.
        text_val, raw_calls = await _fetch_turn(
            backend, messages_list, schemas, model, on_event, opts)
        # Defensive (assessment cluster 3 B3): a malformed call structure
        # (missing `name`, not a dict, etc.) is FILTERED before subscripting —
        # a backend that returns garbage shouldn't crash the agent with KeyError.
        # Unknown tool NAMES are still handled below (with an "unknown tool" result).
        calls = [c for c in raw_calls if isinstance(c, dict) and c.get("name")]
        if not calls:
            # No calls this turn — either a final-text answer or an empty
            # response. Text-with-calls (Anthropic "reasoning + tool calls"
            # pattern) is NOT a final answer; the text is intermediate and the
            # calls are what must execute. The previous order (early-return on
            # any text) discarded calls when both were present.
            text = text_val
            if text is not None and text != "":
                if return_meta:
                    return text, {"turns": turns_done, "outcome": "completed"}
                return text
            if return_meta:
                return "", {"turns": turns_done, "outcome": "empty_response"}
            return ""
        # Record the assistant's tool-call turn in the OpenAI/litellm WIRE shape
        # (bug review H3) — the internal {id,name,args} the backend returned is NOT
        # what the provider expects back in history; turn 2 would send a malformed
        # message. The `tool` result messages below are already wire-shaped.
        messages_list.append({"role": "assistant", "content": None, "tool_calls": [
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
            messages_list.append({"role": "tool", "tool_call_id": call.get("id", call["name"]),
                                  "name": call["name"],
                                  "content": result if isinstance(result, str) else json.dumps(result)})
    # Exhausted. Legacy callers see RuntimeError so a hung loop screams loudly
    # in dev. return_meta callers get a structured outcome instead — useful for
    # AgentLoopNode which surfaces this as an envelope payload.
    if return_meta:
        return "", {"turns": turns_done, "outcome": "max_turns_exhausted"}
    raise RuntimeError(
        "tool loop exceeded max_iters ({}) — raise max_iters in the agent "
        "config, or check whether the agent is calling the same tool "
        "repeatedly without converging on a final answer".format(max_iters))
