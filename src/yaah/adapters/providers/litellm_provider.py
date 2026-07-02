"""LiteLLMProvider — an ApiProvider that calls many providers via litellm.

Used by: the runtime's `litellm` provider (and apps) to reach OpenAI / Gemini /
Bedrock / etc. through one API.
Where: hosts with `pip install litellm` + a provider key.
Why: one provider-agnostic call for non-Claude models; litellm is imported
lazily so it's only required if this backend is actually used.

A native ApiProvider: `stream()` is its completion method — it calls litellm's
acompletion once and projects the response into the StreamEvent vocabulary
(text_delta + toolcall_end + done). `turn()` is kept as the tool-loop entry.
Collected-text callers use the module-level `api_provider.complete()`.

Real chunk-by-chunk streaming (passing `stream=True` to acompletion and
iterating the SSE chunks) is a FOLLOW-UP — the upgrade requires updating
every injected test stub to return an async iterator instead of a
ModelResponse, and no current consumer needs token-level deltas. The
protocol shape today is sufficient for unifying the backend surface; the
wire-level upgrade lights up when a consumer demands it.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

from ...agents.api_provider import ApiProvider, Context, StreamEvent, SupportsTurn, turn as collect_turn


# Agent-plumbing opts that claude-native backends consume but are NOT litellm /
# provider API args (assessment #9): forwarding them 400s the request — and `cwd`
# / `mcp` would ship host paths and infra endpoints to an external API. Popped
# before every acompletion call.
_AGENT_ONLY_OPTS = ("cwd", "mcp", "allowed_tools", "permission_mode")


def _strip_agent_opts(merged: Dict[str, Any]) -> None:
    for key in _AGENT_ONLY_OPTS:
        merged.pop(key, None)


def _as_dict(resp: Any) -> Dict[str, Any]:
    """Normalize a litellm response to a plain dict before extraction.

    The real SDK returns a `ModelResponse` (pydantic) whose `choices` items are
    `Choices`/`Message` OBJECTS, not dicts — so the dict-style readers below would
    silently see `{}` and return empty content. `model_dump()` (pydantic v2) /
    `dict()` (v1) flatten it to nested dicts. Test stubs already pass dicts, which
    pass through untouched."""
    if isinstance(resp, dict):
        return resp
    for attr in ("model_dump", "dict"):
        fn = getattr(resp, attr, None)
        if callable(fn):
            try:
                d = fn()
            except Exception:
                continue
            if isinstance(d, dict):
                return d
    return {}


def _report_usage(on_usage: Optional[Callable[..., Any]], resp: Any, model: Optional[str]) -> None:
    """Feed the cost bridge (R4) from a litellm response. No-op if no callback
    (cost capture off) or the response carries no usage. litellm normalizes usage
    to prompt_tokens / completion_tokens across providers."""
    if on_usage is None:
        return
    resp = _as_dict(resp)
    usage = resp.get("usage") or {}
    resp_model = resp.get("model")
    on_usage({"tokens_in": usage.get("prompt_tokens", 0),
              "tokens_out": usage.get("completion_tokens", 0),
              "model": resp_model or model})


class LiteLLMProvider(ApiProvider, SupportsTurn):
    def __init__(self, *, acompletion: Optional[Callable[..., Awaitable[Any]]] = None,
                 **default_opts: Any) -> None:
        # `acompletion` is the one external dependency, injected for testability:
        # an async (model=, messages=, **opts) -> response callable. Defaults to
        # the real `litellm.acompletion`, imported lazily (only when used). Tests
        # pass a stub so this backend runs without the litellm SDK / network.
        self._acompletion = acompletion
        self._default_opts = default_opts

    def _resolve(self) -> Callable[..., Awaitable[Any]]:
        if self._acompletion is not None:
            return self._acompletion
        import litellm  # pragma: no cover - real SDK shim (lazy, integration-only)
        return litellm.acompletion  # pragma: no cover

    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        return self._iter(context, opts)

    async def _iter(self, context: Context, opts: Dict[str, Any]) -> AsyncIterator[StreamEvent]:
        yield {"type": "start"}
        merged = dict(self._default_opts)
        merged.update(opts)
        on_usage = merged.pop("on_usage", None)  # cost bridge (R4) — not an SDK arg
        _strip_agent_opts(merged)

        model = context.get("model") or "gpt-4o-mini"
        messages: List[Dict[str, Any]] = list(context.get("messages") or [])
        tools = context.get("tools") or []
        system = context.get("system")
        # OpenAI/LiteLLM convention: system prompt is a system-role message, not
        # a top-level kwarg. Legacy turn() forwarded `system` as an opt (broken on
        # the SDK side); the new shape injects it correctly. No existing test
        # asserts the broken behavior.
        if system:
            messages = [{"role": "system", "content": system}] + messages

        kwargs: Dict[str, Any] = dict(merged, model=model, messages=messages)
        if tools:
            kwargs["tools"] = tools

        # Exceptions from the SDK propagate naturally (legacy behavior preserved).
        # Consumers that want in-stream error events can wrap the iteration.
        resp = await self._resolve()(**kwargs)
        _report_usage(on_usage, resp, model)
        msg = _first_message(resp)

        text = msg.get("content")
        if isinstance(text, str) and text:
            yield {"type": "text_delta", "delta": text}

        saw_calls = False
        for tc in msg.get("tool_calls") or []:
            # Defensive (assessment cluster 3 B3 + B4): a malformed tool_call
            # (missing id / function / name) is skipped rather than crashing the
            # agent; a streamed/partial `arguments` that won't parse degrades to
            # {} rather than aborting the turn with JSONDecodeError.
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name")
            tc_id = tc.get("id")
            if not name or not tc_id:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            saw_calls = True
            yield {"type": "toolcall_end", "id": tc_id, "name": name, "args": args}

        done: Dict[str, Any] = {"type": "done",
                                "stop_reason": "tool_use" if saw_calls else "end_turn"}
        usage = _as_dict(resp).get("usage")
        if isinstance(usage, dict):
            done["usage"] = usage
        yield done

    async def turn(self, messages: List[dict], tools: List[dict], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
        out = await collect_turn(self, messages, tools, model=model, **opts)
        # Legacy LiteLLM turn() contract: always returns either {"calls": [...]}
        # OR {"text": str} — never both, never empty. The module-level helper is
        # more lenient (returns whichever blocks were present); apply the legacy
        # strip + None-fallback here so existing consumers stay green.
        if "calls" in out:
            out.pop("text", None)
        else:
            out.setdefault("text", "")
        return out


def _first_message(resp: Any) -> Dict[str, Any]:
    """The first choice's `message` dict, or {} on a malformed/empty response."""
    resp = _as_dict(resp)
    if not resp:
        return {}
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    first = choices[0] if isinstance(choices[0], dict) else None
    return (first.get("message") or {}) if first else {}


def _first_content(resp: Any) -> str:
    """The first choice's message content as a string. Empty on
    missing/None/non-string content (the filtered-response and empty-choices
    cases the assessment flagged)."""
    msg = _first_message(resp)
    content = msg.get("content")
    return content if isinstance(content, str) else ""
