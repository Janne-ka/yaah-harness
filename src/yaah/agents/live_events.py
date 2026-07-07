"""live_events — the StreamEvent→`llm_progress` bridge (the MED-002 seam, wired).

Used by: the Agent, when its tracer has the `live` capture enabled — it builds one
bridge per model call and passes it as `on_event` to the tool loop / `complete()`.
Where: beside the other agent↔trace glue (the agent already owns its `model_call`
spans); the projection lives in `trace.contributors.live`.
Why: monitoring. An operator watching a cloud run needs "what is it doing NOW —
alive or hung?", which the stage-level spans can't answer mid-call. The bridge
turns a provider's live StreamEvents into point-in-time `llm_progress` spans
(t0 == t1) on the EXISTING tracer → the same configured sinks (console / file /
NATS). No second channel; no flag → no bridge → zero overhead.

Event mapping (semantic pulses, never per-token spam):
  start          → event=start                (the model turn began)
  text_delta     → event=progress, chars=N    (heartbeat: cumulative chars,
                                               throttled to one per `min_interval_s`;
                                               the FIRST delta always emits)
  toolcall_end   → event=tool_call, tool=name (a tool call was assembled)
  notice         → kind=tool_use   → event=tool_call, tool=name
                   kind=tool_result → event=tool_result, tool=name
                                              (a provider's PASSIVE report of its
                                               own internal tool loop — claude_cli;
                                               the two pulses BRACKET the tool's
                                               execution window. NOT throttled: one
                                               pulse per tool event, semantically
                                               meaningful; revisit if a real run
                                               proves it chatty)
  done           → event=done, stop_reason=…  (turn ended; NO usage/tokens here —
                                               the `model_call` span owns cost, so
                                               progress spans can't double-count)
  error          → event=error, message=…

KNOWN LIMITS (documented, not hidden — verify per backend before trusting the
heartbeat):
  - A backend on the non-streaming fallback path (`.turn()`-only) yields no
    events, so no pulses fire there at all.
  - litellm's DEFAULT wire shape resolves in a single provider call — `start`,
    then NOTHING until the call completes, then one `progress` + `done`. Set
    `stream: true` on the provider block for real SSE chunking (incremental
    deltas → a real heartbeat); the default stays single-shot until chunking
    has live mileage (see litellm_provider's docstring for the trade-offs).
  - claude_cli streams text incrementally (a real heartbeat) and surfaces its
    INTERNAL tool loop as passive `notice` events → `tool_call`/`tool_result`
    pulses that BRACKET each tool execution. The gap between those two pulses
    is genuinely quiet (the tool is running) — bracketed, not filled.
  - A model silently thinking without emitting deltas cannot be distinguished
    from a hang by this layer — the pulse only proves liveness while output
    flows.

Targets Python 3.9+.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from ..trace import Span

# One pulse per interval while text streams: frequent enough to answer "alive?",
# sparse enough that a file/NATS sink sees a handful of lines per call, not spam.
DEFAULT_MIN_INTERVAL_S = 2.0


def make_live_bridge(tracer: Any, corr: str, *,
                     parent: Optional[str] = None,
                     stage: Optional[str] = None,
                     clock: Callable[[], float] = time.monotonic,
                     min_interval_s: float = DEFAULT_MIN_INTERVAL_S
                     ) -> Callable[[Dict[str, Any]], Any]:
    """Build a per-model-call `on_event` callback that emits `llm_progress` spans.

    NEVER RAISES out of the callback: a broken tracer/sink or a malformed event
    is swallowed — the liveness signal is optional, the model call is not.
    Throttle state (cumulative chars, last-emit clock) is closed over, so build
    a FRESH bridge per call, not per agent."""
    state = {"chars": 0, "last": None}  # type: Dict[str, Any]

    async def _pulse(event: str, **fields: Any) -> None:
        now = clock()
        attrs: Dict[str, Any] = {"event": event}
        if stage is not None:
            attrs["stage"] = stage
        attrs.update(fields)
        await tracer.emit(Span.timed("llm_progress", corr=corr, parent=parent,
                                     t0=now, t1=now, status="ok", attrs=attrs))

    async def on_event(ev: Dict[str, Any]) -> None:
        try:
            etype = ev.get("type") if isinstance(ev, dict) else None
            if etype == "start":
                await _pulse("start")
            elif etype == "text_delta":
                state["chars"] += len(ev.get("delta") or "")
                now = clock()
                last = state["last"]
                if last is None or now - last >= min_interval_s:
                    state["last"] = now
                    await _pulse("progress", chars=state["chars"])
            elif etype == "toolcall_end":
                await _pulse("tool_call", tool=str(ev.get("name", "")))
            elif etype == "notice":
                # a provider's PASSIVE observation of its own internal activity
                # (claude_cli's tool_use/tool_result — never executable calls);
                # inert to every collector, meaningful only here.
                kind = ev.get("kind")
                if kind == "tool_use":
                    await _pulse("tool_call", tool=str(ev.get("tool", "")))
                elif kind == "tool_result":
                    await _pulse("tool_result", tool=str(ev.get("tool", "")))
            elif etype == "done":
                await _pulse("done", stop_reason=str(ev.get("stop_reason", "")))
            elif etype == "error":
                # size-bound the message: it lands in every configured sink
                await _pulse("error", message=str(ev.get("message", ""))[:200])
        except Exception:
            pass  # a monitoring pulse must never break the model call

    return on_event
