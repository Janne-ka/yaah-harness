"""LiteLLMProvider — an ApiProvider that calls many providers via litellm.

Used by: the runtime's `litellm` provider (and apps) to reach OpenAI / Gemini /
Bedrock / etc. through one API.
Where: hosts with `pip install litellm` + a provider key.
Why: one provider-agnostic call for non-Claude models; litellm is imported
lazily so it's only required if this backend is actually used.

A native ApiProvider: `stream()` is its completion method. Two wire shapes,
chosen by the `stream` opt (provider block or per-call; default OFF):

- DEFAULT (single-shot): one acompletion call, the full response projected into
  the StreamEvent vocabulary (text_delta + toolcall_end + done). Battle-tested
  extraction (`_as_dict`/`_first_message` handle the pydantic ModelResponse);
  usage always rides the response. Stays the default until real chunk streaming
  has live mileage.
- `stream: true` (SSE chunks): acompletion(stream=True, stream_options=
  {include_usage: True}), each chunk's delta projected as it arrives —
  text fragments → per-chunk `text_delta` (the live-monitoring heartbeat's
  raw material), indexed tool_call fragments ASSEMBLED across chunks into
  `toolcall_end` (tools do NOT silently downgrade to single-shot — the
  silent-underdelivery footgun), usage from the final chunk when the provider
  honors include_usage (else `done` has no usage — the cost report then shows a
  zero-token call, the documented forensic signal). A mid-stream SDK exception
  PROPAGATES (the engine's consumers keep partial state local, so the call
  fails as cleanly as a pre-yield raise).

`turn()` is kept as the tool-loop entry. Collected-text callers use the
module-level `api_provider.complete()`.

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


# Request-timeout default — network-cut / stall protection, the litellm sibling of
# ClaudeCliProvider's readline inactivity watchdog. litellm.acompletion accepts a
# `timeout` (float seconds), forwarded to the underlying provider client (httpx):
# for the single-shot path it bounds the whole request; for the `stream: true`
# path it bounds the read between SSE chunks — so a mid-stream network cut can't
# hang the chunk loop forever. Unarmed (`timeout=None`) an internet cut freezes
# the calling stage undetectably (the incident this guards). 15 minutes matches
# the claude default: generous enough for a slow large completion, but finite.
# Resolution: an explicit `timeout` (from provider default_opts or per-call opts,
# INCLUDING None) wins — None opts out to the SDK's own default; absent → this
# constant is injected.
_DEFAULT_REQUEST_TIMEOUT = 900.0  # seconds


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
    to prompt_tokens / completion_tokens across providers.

    The two CACHED input classes are reported separately from `tokens_in`
    (same provider-agnostic contract as claude_cli_provider._map_usage) because
    they bill at different rates — cache read ~0.1x the input rate, cache write
    ~1.25x — and pricing them at the full input rate over-reports cache-heavy
    stages several-fold.

    UNLIKE claude's raw usage, litellm's `prompt_tokens` is cache-INCLUSIVE: the
    OpenAI dialect counts `prompt_tokens_details.cached_tokens` inside it, and
    litellm's anthropic shim likewise sums input + cache-read + cache-creation
    into it. So the plain-rate remainder is prompt_tokens MINUS the two cache
    classes, clamped at 0 (a provider that reports a cache count without folding
    it into prompt_tokens would otherwise go negative and under-bill)."""
    if on_usage is None:
        return
    resp = _as_dict(resp)
    usage = resp.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = _as_dict(details)
    cache_read = int(usage.get("cache_read_input_tokens")
                     or details.get("cached_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens")
                      or details.get("cache_creation_tokens") or 0)
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    resp_model = resp.get("model")
    on_usage({"tokens_in": max(prompt - cache_read - cache_write, 0),
              "tokens_cache_read": cache_read,
              "tokens_cache_write": cache_write,
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
        # `stream` is OUR wire-shape switch (see module docstring), popped before
        # the SDK call — the streaming path re-adds it deliberately alongside
        # stream_options; the single-shot path must never forward it.
        chunked = bool(merged.pop("stream", False))
        _strip_agent_opts(merged)
        # Arm the request timeout unless the caller set one explicitly. Present
        # (even None) wins — None opts out to litellm's own default; absent →
        # the finite constant so an unconfigured consumer survives a network cut
        # instead of hanging the stage forever. (See _DEFAULT_REQUEST_TIMEOUT.)
        if "timeout" not in merged:
            merged["timeout"] = _DEFAULT_REQUEST_TIMEOUT

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

        if chunked:
            async for ev in self._iter_chunks(kwargs, on_usage, model):
                yield ev
            return

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

    async def _iter_chunks(self, kwargs: Dict[str, Any],
                           on_usage: Optional[Callable[..., Any]],
                           model: Optional[str]) -> AsyncIterator[StreamEvent]:
        """The `stream: true` wire shape: iterate SSE chunks as they arrive.

        Projection per chunk (defensive parity with the single-shot path — junk
        is skipped, never a crash): delta.content → `text_delta`; delta.tool_calls
        fragments accumulate BY INDEX (id+name arrive on the first fragment, the
        arguments JSON in pieces) and flush as assembled `toolcall_end`s before
        `done`; usage is reported from whichever chunk carries it (the final one,
        when the provider honors include_usage). A mid-stream SDK exception
        propagates — partial consumer state is local everywhere, so the call
        fails as cleanly as a pre-yield raise."""
        # include_usage by default so cost survives streaming; an AUTHOR-supplied
        # stream_options wins on conflicts (opt-out, or dodging a provider that
        # rejects the field's contents) — never silently clobbered.
        so: Dict[str, Any] = {"include_usage": True}
        if isinstance(kwargs.get("stream_options"), dict):
            so.update(kwargs["stream_options"])
        resp = await self._resolve()(**dict(kwargs, stream=True, stream_options=so))
        pending: Dict[int, Dict[str, Any]] = {}   # index → {id, name, arguments}
        order: List[int] = []
        usage: Optional[Dict[str, Any]] = None
        resolved_model: Optional[str] = None      # provider-RESOLVED name off the chunks,
        saw_calls = False                         # so cost labels match the single-shot path
        async for chunk in resp:
            c = _as_dict(chunk)
            if not c:
                continue
            m = c.get("model")
            if isinstance(m, str) and m:
                resolved_model = m
            u = c.get("usage")
            if isinstance(u, dict):
                usage = u
            choices = c.get("choices")
            first = choices[0] if isinstance(choices, list) and choices \
                and isinstance(choices[0], dict) else {}
            delta = first.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                yield {"type": "text_delta", "delta": text}
            for frag in delta.get("tool_calls") or []:
                if not isinstance(frag, dict):
                    continue
                idx = frag.get("index", 0)
                slot = pending.get(idx)
                if slot is None:
                    slot = pending[idx] = {"id": "", "name": "", "arguments": ""}
                    order.append(idx)
                if frag.get("id"):
                    slot["id"] = frag["id"]
                fn = frag.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["arguments"] += fn["arguments"]
            if first.get("finish_reason"):
                saw_calls = saw_calls or first["finish_reason"] == "tool_calls"
        for idx in order:                          # flush assembled calls, in arrival order
            slot = pending[idx]
            if not slot["id"] or not slot["name"]:
                continue                           # malformed fragment set — skip, don't crash
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}                          # partial/broken accumulated JSON degrades
            saw_calls = True
            yield {"type": "toolcall_end", "id": slot["id"], "name": slot["name"],
                   "args": args}
        if usage is not None:
            _report_usage(on_usage, {"usage": usage, "model": resolved_model}, model)
        done: Dict[str, Any] = {"type": "done",
                                "stop_reason": "tool_use" if saw_calls else "end_turn"}
        if usage is not None:
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
