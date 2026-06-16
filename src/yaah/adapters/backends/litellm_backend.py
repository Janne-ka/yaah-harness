"""LiteLLMBackend — a ModelBackend that calls many providers via litellm.

Used by: the runtime's `litellm` provider (and apps) to reach OpenAI / Gemini /
Bedrock / etc. through one API.
Where: hosts with `pip install litellm` + a provider key.
Why: one provider-agnostic call for non-Claude models; litellm is imported
lazily so it's only required if this backend is actually used.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional


# Agent-plumbing opts that claude-native backends consume but are NOT litellm /
# provider API args (assessment #9): forwarding them 400s the request — and `cwd`
# / `mcp` would ship host paths and infra endpoints to an external API. Popped
# before every acompletion call.
_AGENT_ONLY_OPTS = ("cwd", "mcp", "allowed_tools", "permission_mode")


def _strip_agent_opts(merged: Dict[str, Any]) -> None:
    for key in _AGENT_ONLY_OPTS:
        merged.pop(key, None)


def _report_usage(on_usage: Optional[Callable[..., Any]], resp: Any, model: Optional[str]) -> None:
    """Feed the cost bridge (R4) from a litellm response. No-op if no callback
    (cost capture off) or the response carries no usage. litellm normalizes usage
    to prompt_tokens / completion_tokens across providers."""
    if on_usage is None:
        return
    usage = (resp.get("usage") if isinstance(resp, dict) else None) or {}
    resp_model = resp.get("model") if isinstance(resp, dict) else None
    on_usage({"tokens_in": usage.get("prompt_tokens", 0),
              "tokens_out": usage.get("completion_tokens", 0),
              "model": resp_model or model})


class LiteLLMBackend:
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

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        merged = dict(self._default_opts)
        merged.update(opts)
        on_usage = merged.pop("on_usage", None)  # cost bridge (R4) — not an SDK arg
        _strip_agent_opts(merged)
        resp = await self._resolve()(
            model=model or "gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            **merged,
        )
        _report_usage(on_usage, resp, model)
        # Defensive (assessment cluster 3 B5): an empty/filtered choices list, a
        # missing `message`, or a None `content` must NOT raise IndexError/KeyError
        # mid-pipeline — return "" so a downstream validator decides the failure.
        return _first_content(resp)

    async def turn(self, messages: List[dict], tools: List[dict], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
        """One step of the tool-loop via native function-calling. Returns either
        {"calls": [...]} (the model wants tools run) or {"text": ...} (final)."""
        merged = dict(self._default_opts)
        merged.update(opts)
        on_usage = merged.pop("on_usage", None)  # cost bridge (R4) — not an SDK arg
        _strip_agent_opts(merged)
        resp = await self._resolve()(
            model=model or "gpt-4o-mini", messages=messages, tools=tools, **merged)
        _report_usage(on_usage, resp, model)
        msg = _first_message(resp)
        tool_calls = msg.get("tool_calls") or []
        calls = []
        for tc in tool_calls:
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
            calls.append({"id": tc_id, "name": name, "args": args})
        if calls:
            return {"calls": calls}
        return {"text": msg.get("content") or ""}


def _first_message(resp: Any) -> Dict[str, Any]:
    """The first choice's `message` dict, or {} on a malformed/empty response."""
    if not isinstance(resp, dict):
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
