"""LangfuseTraceSink — forward trace records to Langfuse (R5a).

Used by: the runtime when `trace.sink: {type: langfuse, ...}`; subscribed to the
`trace` topic. The write-side twin of LangfusePromptSource — we already READ
prompts from Langfuse; this WRITES traces back.
Where: a swap-in TraceSink adapter (binds to Langfuse).
Why: Langfuse's UI gives the dashboard + cost aggregation + evals for free, so
sending spans there REDUCES how much custom observability we build. Langfuse
also does the token->$ math, so the Langfuse path needs no local price-map.

Span -> Langfuse mapping: `corr`->trace, `model_call`->generation (model +
tokens, so Langfuse computes cost), every other span->observation span; nested
by `parent` where it resolves. The client is injected for testability (a stub in
tests); the real Langfuse client is built lazily, only when used.

v1 approximations (refine when run against a live instance): spans carry monotonic
durations, not wall-clock timestamps, so duration rides in metadata and Langfuse
timestamps on ingestion; `parent` is an envelope-causation id, so deep nesting is
best-effort. Both are cosmetic — trace/generation/cost land correctly.

Targets Python 3.9+.
"""
from __future__ import annotations

from typing import Any, Dict, Set

from ...core import Envelope


class LangfuseTraceSink:
    def __init__(self, *, client: Any = None, **client_opts: Any) -> None:
        # `client` is the external dependency, injected for testability: any object
        # exposing trace()/span()/generation(). Defaults to a lazily-built real
        # Langfuse client. Tests pass a stub so this runs without the SDK / creds.
        self._client_opts = client_opts
        self._client: Any = client
        self._seen_traces: Set[str] = set()  # corr we've already opened a trace for

    def _client_(self) -> Any:
        if self._client is None:  # pragma: no cover - real SDK shim (lazy, integration-only)
            from langfuse import Langfuse

            self._client = Langfuse(**self._client_opts)
        return self._client

    async def handle(self, env: Envelope) -> None:
        r = env.payload
        corr = r.get("corr")
        if not corr:
            return
        client = self._client_()
        if corr not in self._seen_traces:
            client.trace(id=corr, name="run")
            self._seen_traces.add(corr)
        name = r.get("name")
        obs_name = r.get("stage") or r.get("tool") or name
        common: Dict[str, Any] = dict(
            trace_id=corr, id=r.get("id"), parent_observation_id=r.get("parent"),
            name=obs_name,
            metadata={k: r[k] for k in ("stage", "status", "duration_ms", "tool") if k in r})
        if name == "model_call":
            client.generation(
                model=r.get("model"),
                usage={"input": r.get("tokens_in", 0), "output": r.get("tokens_out", 0)},
                **common)
        else:
            client.span(**common)
