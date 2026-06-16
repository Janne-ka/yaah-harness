"""context_broker — a built-in agent tool that delegates "what's relevant about
X?" to a cheap (Haiku-grade) RAG-ish broker NODE configured elsewhere (R12).

Used by: Agent.invoke, which binds one of these to the CURRENT envelope per call
(a closure — the broker dispatches over Comms to a node role the author named).
Where: agent config. R9's `envelope_get` is the deterministic primitive ("give me
field X verbatim"); this layer is the FUZZY companion ("the agent describes what
it needs in natural language, a cheap broker returns the slice"). Both are
optional, opt-in, and per-invocation; an agent can have one, the other, neither,
or both.

Two routing paths (governed by the same payload allow-list as R9, so the broker
can never leak more than envelope_get could):
  1. FAST PATH — if the model passes `field: "<name>"` and `<name>` is in the
     allow-list AND present on the envelope, the broker serves it locally
     (no model call). The expensive agent never pays for a broker call just to
     fetch a verbatim field.
  2. FUZZY PATH — otherwise the broker dispatches a request to the configured
     node role (`broker_role`, e.g. `"role:context-broker"`) with the query plus
     a snapshot of the allow-listed envelope payload, and returns the node's
     `slice` (or `raw`) reply, capped at `max_chars`.

Isolation preserved (the broker is its own worker, a separate node — same
agent-isolation rule the engine uses everywhere). The broker NODE itself is
a regular yaah agent that the author configures in the pipeline JSON.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core import Envelope, Kind
from .tool import Tool

_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "what you need — natural language; e.g. "
                                 "'the part of the diff that touches auth'"},
        "field": {"type": "string",
                  "description": "OPTIONAL exact field name for a fast verbatim "
                                 "fetch (no broker call). If omitted, the broker "
                                 "interprets `query` and returns a slice."},
    },
    "required": ["query"],
}


def make_context_broker_tool(
    envelope: Envelope,
    *,
    broker_role: str,
    comms: Any,
    expose: Dict[str, List[str]],
    max_chars: int = 20000,
    name: str = "context_broker",
    tracer: Optional[Any] = None,
) -> Tool:
    """Build a `context_broker` Tool bound to `envelope` and `comms`.

    Args:
        envelope: the per-invocation envelope (both paths read from it).
        broker_role: node role for fuzzy queries (e.g. `"role:context-broker"`).
            Must be a yaah agent the author configured elsewhere in the pipeline.
        comms: the bus that resolves `node:` requests to broker_role.
        expose: payload allow-list (same shape as R9 `envelope_get`). SECURITY:
            governs BOTH paths uniformly — fast path looks up by `field` here;
            fuzzy path snapshots ONLY these keys into the broker request. So a
            broker can NEVER leak more than envelope_get already could. The
            same "never expose baton/auth" rule applies.
        max_chars: hard cap on returned text (default 20000).
        name: tool name surfaced to the model. Default `"context_broker"`.
        tracer: the CALLING agent's tracer. This tool is a NON-terminal
            requester: when the broker's reply carries R6 carriage records in
            `headers["trace"]` (drained at the broker's serve boundary), they
            are re-ingested here so they ride out with the calling stage's own
            reply instead of being dropped with the headers (assessment #6 —
            the "4 emitted, 2 survive" loss).
    """
    allow_payload = set(expose.get("payload", []))

    async def _handler(args: Dict[str, Any]) -> Dict[str, Any]:
        query = args.get("query") or ""
        field = args.get("field")

        # FAST PATH — verbatim allow-listed field, no model call.
        if field:
            if field not in allow_payload:
                return {"error": "field {!r} not exposed".format(field),
                        "allowed": sorted(allow_payload)}
            value = envelope.payload.get(field)
            if value is None:
                return {"value": None, "note": "absent", "fast_path": True}
            text = value if isinstance(value, str) else _json(value)
            truncated = len(text) > max_chars
            return {"value": text[:max_chars], "fast_path": True,
                    "truncated": truncated, "chars": len(text)}

        if not query.strip():
            return {"error": "query is empty (and no field given)"}

        # FUZZY PATH — ask the broker node. Snapshot ONLY the allow-listed
        # payload so the broker can't read more than envelope_get would.
        snapshot = {k: envelope.payload[k]
                    for k in allow_payload if k in envelope.payload}
        ask = Envelope(
            Kind.RESULT,
            {"query": query, "envelope": snapshot},
            {"correlation_id": envelope.correlation_id,
             "parent": envelope.id},
        )
        try:
            reply = await comms.request(broker_role, ask)
        except Exception as e:  # broker miss/error: surface, don't crash the agent
            return {"error": "broker {!r} request failed: {}".format(broker_role, e)}
        # R6 reclaim: this tool consumes only the payload — without this, any
        # carriage records the broker's serve boundary drained onto the reply
        # headers would die here (assessment #6).
        recs = reply.headers.pop("trace", None)
        if recs and hasattr(tracer, "ingest"):
            await tracer.ingest([r for r in recs if isinstance(r, dict)])
        if reply.kind == Kind.ERROR:
            return {"error": "broker returned ERROR",
                    "detail": reply.payload.get("failure") or reply.payload}
        # The broker is expected to return a result with a `slice` (or `raw`)
        # payload field. We accept either so the broker's prompt can be terse.
        slice_text = reply.payload.get("slice")
        if slice_text is None:
            slice_text = reply.payload.get("raw")
        if slice_text is None:
            return {"error": "broker reply missing 'slice'/'raw'",
                    "got_keys": sorted(reply.payload.keys())}
        text = slice_text if isinstance(slice_text, str) else _json(slice_text)
        truncated = len(text) > max_chars
        return {"value": text[:max_chars], "fast_path": False,
                "truncated": truncated, "chars": len(text)}

    return Tool(
        name=name,
        impl=_handler,
        description=(
            "Ask a cheap broker for the part of your task envelope that is "
            "RELEVANT to a query. Pass `field: \"name\"` for a verbatim fetch "
            "of an allow-listed field (no broker call); pass `query: \"...\"` "
            "for a natural-language ask the broker interprets. Readable "
            "payload fields (for the fast path): {}.".format(sorted(allow_payload))
        ),
        schema=_SCHEMA,
    )


def _json(value: Any) -> str:
    import json
    return json.dumps(value, indent=2, default=str)
