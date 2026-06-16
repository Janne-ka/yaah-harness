"""envelope_get — a built-in agent tool that lets the model SELECTIVELY pull data
from its own invocation envelope (R9).

Used by: Agent.invoke, which binds one of these to the CURRENT envelope per call
(a closure — the data is per-invocation, so the tool can't be a static call_target
string; tool_loop invokes a callable impl directly).
Where: agent config. The idea: the envelope may carry the WHOLE thing (e.g. full
files); the agent stays cheap by being PICKY — it fetches only the slices it needs,
on demand, instead of being handed everything inline. This is the model-initiated
twin of the build_*_context transforms (which pre-build context) — and the knob an
A/B arm turns: same rich envelope, but a tight prompt + a capped tool make a weak
model pull a slim view while a strong arm pulls full.
Why governed: an `expose` ALLOW-LIST (the model may read `diff`/`spec`, never
`baton`/auth headers — the leak rule), optional `filters`, and a hard `max_chars`
cap so a pull can't blow the context window.

Targets Python 3.9+.
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Union

from ..core import Envelope
from ..filters import Filter
from .tool import Tool

_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["payload", "header"],
                   "description": "where to read from"},
        "key": {"type": "string", "description": "the field to fetch"},
        "filter": {"type": "object",
                   "description": "optional {name, ...params} transform on the value"},
    },
    "required": ["key"],
}


def make_envelope_get_tool(
    envelope: Envelope,
    *,
    expose: Dict[str, List[str]],
    filters: Optional[Dict[str, Union[Filter, Callable[..., Any]]]] = None,
    max_chars: int = 20000,
) -> Tool:
    """Build an `envelope_get` Tool bound to `envelope`.

    Args:
        envelope: the per-invocation envelope the tool reads from.
        expose: allow-list `{"payload": [...keys], "header": [...keys]}` — only
            listed keys are readable. SECURITY: an over-broad list is the YAAH
            equivalent of the IaC `0.0.0.0/0` mistake — start empty, add only
            what the agent needs for this stage. NEVER expose `baton` or auth
            headers; that lets the model spoof system state.
        filters: name → Filter port instance OR plain `(value, **params) -> value`
            callable (sync or async). The model references filters by NAME with
            allowed params; it never supplies logic. AUTHOR-vetted only.
        max_chars: hard cap on returned text (default 20000). Pulls larger get
            truncated and the response carries `truncated: true`. Never set this
            higher than the model's context window.
    """
    allow_payload = set(expose.get("payload", []))
    allow_header = set(expose.get("header", []))
    filters = filters or {}

    async def _handler(args: Dict[str, Any]) -> Dict[str, Any]:
        source = args.get("source", "payload")
        key = args.get("key")
        if source == "header":
            if key not in allow_header:
                return {"error": "header {!r} not exposed".format(key),
                        "allowed": sorted(allow_header)}
            value: Any = envelope.headers.get(key)
        else:
            if key not in allow_payload:
                return {"error": "payload key {!r} not exposed".format(key),
                        "allowed": sorted(allow_payload)}
            value = envelope.payload.get(key)
        if value is None:
            return {"value": None, "note": "absent"}
        spec = args.get("filter")
        if spec and isinstance(spec, dict):
            fname = spec.get("name")
            fn = filters.get(fname)
            if fn is None:
                return {"error": "unknown filter {!r}".format(fname),
                        "available": sorted(filters)}
            params = {k: v for k, v in spec.items() if k != "name"}
            if hasattr(fn, "apply"):
                value = await fn.apply(value, **params)
            else:
                res = fn(value, **params)
                value = await res if inspect.isawaitable(res) else res
        text = value if isinstance(value, str) else _json(value)
        truncated = len(text) > max_chars
        return {"value": text[:max_chars], "truncated": truncated, "chars": len(text)}

    return Tool(
        name="envelope_get",
        impl=_handler,
        description=("Selectively fetch one field from your task envelope. "
                     "Readable payload keys: {}; header keys: {}. Use this to pull "
                     "only the data you need (e.g. a file's diff) instead of "
                     "assuming it.").format(sorted(allow_payload), sorted(allow_header)),
        schema=_SCHEMA,
    )


def _json(value: Any) -> str:
    import json
    return json.dumps(value, indent=2, default=str)
