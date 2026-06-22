"""ApiProvider — the streaming model interface that will replace ModelBackend.

Used by: future call sites that want event-level visibility (token deltas,
tool-call assembly, usage events). Implemented by: forthcoming native
streaming backends (B2+) and LegacyBackendAdapter (this file) which wraps a
current ModelBackend/ToolBackend.
Where: a NEW seam alongside `model_backend.py` — both protocols coexist
through Phase 1b. Module-level helpers `complete()` and `turn()` give
callers the OLD return shapes while routing through the new protocol, so
migration happens one consumer at a time.
Why: the current `complete() -> str` / `turn() -> {text|calls}` pair is a
collected-result shape. It can't surface partial outputs, makes streaming
backends impossible to wire cleanly, and forces the tool-loop to assemble
tool calls from whatever ad-hoc dict the backend chose. A single
`stream(context) -> AsyncIterator[StreamEvent]` matches what every
provider's wire format already is, and is the shape Pi-ai converged on
after the same evolution.

The protocol — one method, event stream:
    async for event in provider.stream(context, **opts):
        if event["type"] == "text_delta":
            ...

Event types (tagged via "type" field):
- start         : the response has begun                {"type": "start"}
- text_delta    : a chunk of assistant text             {"type": "text_delta", "delta": str}
- toolcall_end  : a tool call has been fully assembled  {"type": "toolcall_end", "id", "name", "args"}
- done          : the turn ended cleanly                {"type": "done", "stop_reason": str, "usage"?: dict}
- error         : the provider raised                   {"type": "error", "message": str}

Content-block shape for AssistantMessage.content (Anthropic-style):
- {"type": "text", "text": str}
- {"type": "thinking", "thinking": str}
- {"type": "tool_use", "id": str, "name": str, "input": dict}

This module is ADDITIVE — no existing code path imports it yet. Migration
order is documented in `.notes/phase-1-resume-context.md` (B2 onward).

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, runtime_checkable

try:
    from typing import Literal, TypedDict
except ImportError:  # pragma: no cover - 3.9 has both, this is a belt-and-braces guard
    from typing_extensions import Literal, TypedDict  # type: ignore


# --- Event shapes -----------------------------------------------------------

class _StartEvent(TypedDict):
    type: Literal["start"]


class _TextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    delta: str


class _ToolCallEndEvent(TypedDict, total=False):
    type: Literal["toolcall_end"]
    id: str
    name: str
    args: Dict[str, Any]


class _DoneEvent(TypedDict, total=False):
    type: Literal["done"]
    stop_reason: str       # "end_turn" | "tool_use" | "max_tokens" | "error" | ...
    usage: Dict[str, Any]  # provider-native usage record, opaque to consumers


class _ErrorEvent(TypedDict):
    type: Literal["error"]
    message: str


# Public alias: a stream yields one of these. Kept as `Dict[str, Any]` at the
# protocol surface for ease of consumption (no `cast()` at every call site);
# the TypedDicts above document the shapes.
StreamEvent = Dict[str, Any]


# --- Content-block & message shapes -----------------------------------------

class TextBlock(TypedDict):
    type: Literal["text"]
    text: str


class ThinkingBlock(TypedDict):
    type: Literal["thinking"]
    thinking: str


class ToolUseBlock(TypedDict):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]


ContentBlock = Dict[str, Any]  # union of the three above; see assemble_message()


class AssistantMessage(TypedDict, total=False):
    role: Literal["assistant"]
    content: List[ContentBlock]
    stop_reason: str
    usage: Dict[str, Any]


# --- Input context ----------------------------------------------------------

class Context(TypedDict, total=False):
    """Input shape for ApiProvider.stream(). All fields optional so callers
    can pass partial contexts and providers fill defaults."""
    system: Optional[str]
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    model: Optional[str]


# --- The protocol -----------------------------------------------------------

@runtime_checkable
class ApiProvider(Protocol):
    """Single-method streaming provider. Replaces ModelBackend over Phase 1b.

    Implementations MUST yield a `start` event first and a `done` (or
    `error`) event last. Between them: any number of `text_delta` events
    interleaved with `toolcall_end` events, in the order the provider
    produces them. Streams that aren't natively streaming (FakeBackend
    wrapping a canned string) yield a single `text_delta` with the full
    text, then `done` — consumers shouldn't have to care."""

    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        ...


# --- Assembly: events -> AssistantMessage ----------------------------------

async def assemble_message(events: AsyncIterator[StreamEvent]) -> AssistantMessage:
    """Drain an event stream into one AssistantMessage. The dual of `stream`.

    Tool-call events become tool_use blocks; consecutive text deltas merge
    into a single text block (matching how Anthropic's API emits content)."""
    content: List[ContentBlock] = []
    text_buf: List[str] = []
    stop_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None

    def _flush_text() -> None:
        if text_buf:
            content.append({"type": "text", "text": "".join(text_buf)})
            text_buf.clear()

    async for ev in events:
        kind = ev.get("type")
        if kind == "start":
            continue
        if kind == "text_delta":
            text_buf.append(ev.get("delta", ""))
        elif kind == "toolcall_end":
            _flush_text()
            content.append({
                "type": "tool_use",
                "id": ev.get("id", ""),
                "name": ev.get("name", ""),
                "input": ev.get("args", {}) or {},
            })
        elif kind == "done":
            _flush_text()
            stop_reason = ev.get("stop_reason")
            usage = ev.get("usage")
        elif kind == "error":
            raise RuntimeError("provider error: {}".format(ev.get("message", "")))

    msg: AssistantMessage = {"role": "assistant", "content": content}
    if stop_reason is not None:
        msg["stop_reason"] = stop_reason
    if usage is not None:
        msg["usage"] = usage
    return msg


# --- Module-level helpers (bridge to old return shapes) ---------------------

async def complete(provider: ApiProvider, prompt: str, *,
                   model: Optional[str] = None, system: Optional[str] = None,
                   **opts: Any) -> str:
    """Same return shape as ModelBackend.complete() — a single string.

    Drives the provider with a one-message user prompt, collects every
    text block into one string. Migration helper: call sites can swap
    `backend.complete(prompt)` for `complete(provider, prompt)` without
    changing the rest of the code."""
    ctx: Context = {"messages": [{"role": "user", "content": prompt}]}
    if system is not None:
        ctx["system"] = system
    if model is not None:
        ctx["model"] = model
    msg = await assemble_message(provider.stream(ctx, **opts))
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")


async def turn(provider: ApiProvider, messages: List[Dict[str, Any]],
               tools: List[Dict[str, Any]], *,
               model: Optional[str] = None, system: Optional[str] = None,
               **opts: Any) -> Dict[str, Any]:
    """Same return shape as ToolBackend.turn() — `{text, calls}` dict.

    Calls are projected from tool_use content blocks into the
    `{id, name, args}` shape the existing tool-loop expects. Text is
    joined across any text blocks. Migration helper for the tool-loop."""
    ctx: Context = {"messages": list(messages), "tools": list(tools)}
    if system is not None:
        ctx["system"] = system
    if model is not None:
        ctx["model"] = model
    msg = await assemble_message(provider.stream(ctx, **opts))
    text_parts: List[str] = []
    calls: List[Dict[str, Any]] = []
    for block in msg.get("content", []):
        bt = block.get("type")
        if bt == "text":
            text_parts.append(block.get("text", ""))
        elif bt == "tool_use":
            calls.append({"id": block.get("id", ""), "name": block.get("name", ""),
                          "args": block.get("input", {}) or {}})
    out: Dict[str, Any] = {}
    if text_parts:
        out["text"] = "".join(text_parts)
    if calls:
        out["calls"] = calls
    return out


# --- Adapter: legacy ModelBackend / ToolBackend -> ApiProvider --------------

class LegacyBackendAdapter:
    """Wrap an old ModelBackend (or ToolBackend) as an ApiProvider.

    Non-streaming sources collapse to a single text_delta. The whole point
    is that existing FakeBackend / ScriptedBackend / ClaudeCliBackend /
    LiteLLMBackend implementations keep working as the new protocol gains
    consumers, with zero changes to their code. Once every consumer has
    migrated, B2's per-backend native implementations replace the adapter
    one backend at a time."""

    def __init__(self, backend: Any) -> None:
        if not hasattr(backend, "complete"):
            raise TypeError(
                "LegacyBackendAdapter requires a ModelBackend (has .complete()); "
                "got {!r}".format(type(backend).__name__))
        self._backend = backend

    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        # `_iter` is an async generator function — calling it (no await) returns
        # the generator, which IS an AsyncIterator. Matches the Protocol signature
        # so consumers can `async for ev in provider.stream(ctx)` directly.
        return self._iter(context, opts)

    async def _iter(self, context: Context, opts: Dict[str, Any]) -> AsyncIterator[StreamEvent]:
        yield {"type": "start"}
        model = context.get("model")
        system = context.get("system")
        tools = context.get("tools") or []
        messages = context.get("messages") or []
        try:
            if tools:
                if not hasattr(self._backend, "turn"):
                    raise TypeError("legacy backend {!r} has no .turn() — cannot serve tool calls".format(
                        type(self._backend).__name__))
                kw: Dict[str, Any] = {**opts}
                if model is not None:
                    kw["model"] = model
                if system is not None:
                    kw["system"] = system
                result = await self._backend.turn(messages, tools, **kw)
                text = result.get("text")
                calls = result.get("calls") or []
                if text:
                    yield {"type": "text_delta", "delta": text}
                for call in calls:
                    if not isinstance(call, dict) or not call.get("name"):
                        continue
                    yield {"type": "toolcall_end", "id": call.get("id", call.get("name", "")),
                           "name": call.get("name", ""), "args": call.get("args", {}) or {}}
                yield {"type": "done", "stop_reason": "tool_use" if calls else "end_turn"}
            else:
                # No tools — collapse to one message via .complete(). Build the prompt
                # from the messages list (last user message), keeping legacy semantics.
                prompt = _last_user_text(messages)
                kw = {**opts}
                if model is not None:
                    kw["model"] = model
                if system is not None:
                    # Legacy ModelBackend.complete() has no `system` arg; pass via opts.
                    # Backends that ignore unknown kwargs are fine; those that don't
                    # would have already broken in legacy code paths.
                    kw["system"] = system
                text = await self._backend.complete(prompt, **kw)
                if text:
                    yield {"type": "text_delta", "delta": text}
                yield {"type": "done", "stop_reason": "end_turn"}
        except Exception as exc:  # pragma: no cover - exercised in test_api_provider
            yield {"type": "error", "message": "{}: {}".format(type(exc).__name__, exc)}


def _last_user_text(messages: List[Dict[str, Any]]) -> str:
    """Best-effort: pull the most recent user-role string content from a
    messages list. Matches the convention complete()/turn() callers
    already use (a single-shot user prompt)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            # Anthropic-style content blocks: concatenate text blocks.
            if isinstance(content, list):
                return "".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
    return ""
