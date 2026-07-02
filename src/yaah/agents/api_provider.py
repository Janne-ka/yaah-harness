"""ApiProvider — the streaming model interface.

Used by: call sites that want event-level visibility (token deltas, tool-call
assembly, usage events) — run_tool_loop consumes provider.stream() directly,
and Agent.invoke's plain path collects it via complete(). Implemented by: every
shipped backend natively (FakeProvider, ScriptedProvider, FakeToolProvider,
ScriptedToolProvider, LiteLLMProvider, ClaudeCliProvider, RoutingProvider).
Where: the model seam. A backend author implements `stream()` and nothing
else. Module-level helpers `complete()` and `turn()` project a stream into
collected-result shapes (a string / a `{text, calls}` dict) for call sites
that don't want to drain the stream themselves; both are stream-first with a
fallback to a collected-only provider's native complete()/turn() (a test
double or external legacy backend), mirroring run_tool_loop's turn() fallback.
Why: a collected-result `complete() -> str` / `turn() -> {text|calls}` pair
can't surface partial outputs, makes streaming backends impossible to wire
cleanly, and forces the tool-loop to assemble tool calls from whatever ad-hoc
dict the backend chose. A single
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

Targets Python 3.9+.
"""
from __future__ import annotations

from abc import abstractmethod
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
    """Single-method streaming provider — the model seam every backend implements.

    Implementations MUST yield a `start` event first and a `done` (or
    `error`) event last. Between them: any number of `text_delta` events
    interleaved with `toolcall_end` events, in the order the provider
    produces them. Streams that aren't natively streaming (FakeProvider
    wrapping a canned string) yield a single `text_delta` with the full
    text, then `done` — consumers shouldn't have to care."""

    @abstractmethod
    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        ...


@runtime_checkable
class SupportsTurn(Protocol):
    """OPTIONAL tool-loop capability — a backend that runs one provider-native
    tool turn (`{text, calls}`) instead of delegating to the engine's tool loop.
    Kept explicit and separate from ApiProvider because it's optional: claude_cli
    has NO native turn (it runs its own tool loop), so Agent checks for this
    capability (`supports_turn`/hasattr) rather than assuming every provider has it.
    A tool-capable backend declares `class X(ApiProvider, SupportsTurn)`."""

    @abstractmethod
    async def turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
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


# --- Adapt any provider to a stream -----------------------------------------

def _prompt_of(context: Context) -> str:
    """The most recent user-role string message — the single-prompt a collected
    `complete()` expects when we only have a messages list."""
    for msg in reversed(context.get("messages") or []):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return msg["content"]
    return ""


async def stream_of(provider: Any, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
    """Yield `provider`'s event stream. A provider with a native `stream()` is
    used as-is; a collected-only provider (just `complete()`/`turn()` — a test
    double or an external legacy backend, possibly sitting behind a router) is
    wrapped into a one-shot stream so any stream consumer (assemble_message,
    RoutingProvider.stream) works against it uniformly.

    `**opts` (incl. the `on_usage` cost callback) is forwarded verbatim: a
    collected-only provider is expected to accept `**opts` like every shipped
    backend does, and MAY fire `on_usage` itself to report cost."""
    if hasattr(provider, "stream"):
        async for ev in provider.stream(context, **opts):
            yield ev
        return
    yield {"type": "start"}
    tools = context.get("tools")
    if tools and hasattr(provider, "turn"):
        out = await provider.turn(list(context.get("messages") or []), list(tools),
                                  model=context.get("model"), **opts) or {}
        for c in out.get("calls") or []:
            yield {"type": "toolcall_end", "id": c.get("id", ""),
                   "name": c.get("name", ""), "args": c.get("args", {}) or {}}
        if out.get("text"):
            yield {"type": "text_delta", "delta": out["text"]}
    elif hasattr(provider, "complete"):
        text = await provider.complete(_prompt_of(context), model=context.get("model"), **opts)
        if text:
            yield {"type": "text_delta", "delta": text}
    yield {"type": "done", "stop_reason": "end_turn"}


# --- Module-level helpers (bridge to old return shapes) ---------------------

async def complete(provider: Any, prompt: str, *,
                   model: Optional[str] = None, system: Optional[str] = None,
                   **opts: Any) -> str:
    """Collect a model reply into a single string (the `complete()` shape).

    Routes through `stream_of`, so a collected-only provider (a test double or
    an external legacy backend) works too — that fallback lives in ONE place."""
    ctx: Context = {"messages": [{"role": "user", "content": prompt}]}
    if system is not None:
        ctx["system"] = system
    if model is not None:
        ctx["model"] = model
    msg = await assemble_message(stream_of(provider, ctx, **opts))
    return "".join(b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text")


async def turn(provider: Any, messages: List[Dict[str, Any]],
               tools: List[Dict[str, Any]], *,
               model: Optional[str] = None, system: Optional[str] = None,
               **opts: Any) -> Dict[str, Any]:
    """Collect a model reply into a `{text, calls}` dict (the `turn()` shape).

    Routes through `stream_of` (same one-place fallback as `complete()`); calls
    are projected from tool_use content blocks into the `{id, name, args}` shape
    the tool-loop expects; text is joined across any text blocks."""
    ctx: Context = {"messages": list(messages), "tools": list(tools)}
    if system is not None:
        ctx["system"] = system
    if model is not None:
        ctx["model"] = model
    msg = await assemble_message(stream_of(provider, ctx, **opts))
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
