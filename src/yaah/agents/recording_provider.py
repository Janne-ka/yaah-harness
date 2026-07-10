"""RecordingProvider — an ApiProvider decorator that RECORDS every completion.

Used by: runtime_factories when a provider spec carries `record_to: <path>` — it
wraps the built leaf so a live run appends each model call's completion text to a
JSONL file, one object per call:

    {"order": <int>, "model": <bare-name>, "completion": <text>}

Where: the model seam, wrapping any leaf ApiProvider INSIDE the RoutingProvider,
so it sees the post-prefix bare model name — exactly the key ScriptedProvider's
`by_model` is indexed by, so a recording IS a replay script.
Why: v1 counterfactual replay (yaah.replay) can script only ONE model call — the
experiment row store persists only the terminal completion. Because EVERY model
call is served through `ApiProvider.stream()` (the plain path's `complete()`, the
`turn()` helper and `run_tool_loop` all route through `stream_of` -> `stream`),
a decorator over `stream()` catches them all at ONE point — a linear multi-stage
run, an escalate_model rung, each recorded in call order — so replay can script
the whole topology instead of refusing it.

`order` is a process-global monotonic counter shared across every RecordingProvider
instance, so the file is a faithful GLOBAL call-order log (it documents the
interleaving under concurrency — which is exactly why replay of concurrent
topologies must stay refused: the interleaving is scheduler-dependent, not
reproducible from a per-model FIFO list).

PRIVACY — recording is OFF by default and OPT-IN per provider. A completion is
MODEL OUTPUT: it may contain sensitive DERIVED content — a summary of a private
input, extracted PII, a secret echoed back from the prompt. The engine
deliberately never puts completion TEXT in traces; `record_to` is the one place
it lands on disk, so it is explicit, per-provider, and writes only where the
author points it. Do NOT enable it against untrusted data without a plan for the
file (it is plaintext JSONL, unencrypted). Prompts are NOT recorded (only the
answer), matching the trace layer's existing no-prompt-text stance.

Targets Python 3.9+.
"""
from __future__ import annotations

import itertools
import json
import threading
from typing import Any, AsyncIterator, Dict, List, Optional

from .api_provider import ApiProvider, Context, StreamEvent, stream_of


class RecordingProvider(ApiProvider):
    """Wrap `inner` (any ApiProvider / collected-only leaf) and append every
    completion it produces to the JSONL file at `path`. Forwards `turn` and
    `supports_turn` to `inner` so a tool-capable leaf keeps its capability."""

    # Process-global so a GLOBAL call order is captured even when several named
    # providers each wrap a leaf writing to the same file.
    _order = itertools.count()
    _lock = threading.Lock()

    def __init__(self, inner: Any, path: str) -> None:
        self._inner = inner
        self._path = path

    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        return self._record_stream(context, **opts)

    async def _record_stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        # stream_of adapts a collected-only leaf (turn()/complete()-only, e.g. an
        # external legacy backend) into a one-shot stream, so ALL three provider
        # shapes flow through this single tee point.
        model = context.get("model") or ""
        parts: List[str] = []
        async for ev in stream_of(self._inner, context, **opts):
            if ev.get("type") == "text_delta":
                parts.append(ev.get("delta", ""))
            yield ev
        # Written only on CLEAN completion of the stream: an error event makes the
        # consumer raise (closing this generator before we reach here), so a failed
        # call — which has no completion to replay — records nothing.
        self._write(model, "".join(parts))

    async def turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], *,
                   model: Optional[str] = None, **opts: Any) -> Dict[str, Any]:
        """Forward a direct tool-loop `turn` to the leaf and record its text. In
        practice the loop prefers `stream()` (recorded above); this covers a caller
        that invokes `turn` directly on a routed provider. A leaf without `turn`
        raises the same TypeError it would unwrapped."""
        inner_turn = getattr(self._inner, "turn", None)
        if not callable(inner_turn):
            raise TypeError(
                "provider {} has no turn()".format(type(self._inner).__name__))
        out = await inner_turn(messages, tools, model=model, **opts) or {}
        self._write(model or "", out.get("text") or "")
        return out

    def supports_turn(self, model: Optional[str] = None) -> bool:
        """Reflect the LEAF's tool capability so the Agent's turn-vs-manifest
        choice (and RoutingProvider.supports_turn) stay honest through the wrap."""
        inner = self._inner
        if hasattr(inner, "supports_turn"):
            return bool(inner.supports_turn(model))
        return callable(getattr(inner, "turn", None))

    def _write(self, model: str, completion: str) -> None:
        # `order` drawn INSIDE the lock so it can never disagree with file-append
        # order (replay groups by FILE order; `order` documents it for humans).
        with self._lock:
            rec = {"order": next(self._order), "model": model, "completion": completion}
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
