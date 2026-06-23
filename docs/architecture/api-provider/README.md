# ApiProvider — architecture

> **Status note (2026-06-23):** the migration is complete. The legacy
> `ModelBackend`/`ToolBackend` Protocols were removed in B6, and the
> `LegacyBackendAdapter` bridge was removed in MED-001 (every backend now
> implements `stream()` natively; a new backend author writes `stream()`,
> not the old `turn()`). The `LegacyBackendAdapter` sections and the
> `legacy-adapter-bridge.svg` / `migration-shape.svg` diagrams below are
> **historical** — kept for the migration story, not current code.

`ApiProvider` is the streaming model interface that replaces `ModelBackend`
/ `ToolBackend` over the course of Phase 1b. Where the legacy protocol is
two collected-result methods (`complete() -> str`, `turn() -> {text|calls}`),
the new protocol is **one event-stream method**:

```python
async for event in provider.stream(context, **opts):
    if event["type"] == "text_delta":
        ...
```

This page is the implementer / architect view of the migration. For the
node-level architecture see [`../agent-loop/`](../agent-loop/).

## In this folder

- [**Use cases**](use-cases.md) — six concrete scenarios the streaming
  protocol enables (token-level surfacing, partial tool-call assembly,
  trace events, capability simplification, provider parallelism, audit
  replay). Each has a "why a stream" justification + a pipeline / consumer
  shape.
- [**Flow**](flow.md) — call graphs and event timelines. Four diagrams:
  protocol comparison (old vs new), the LegacyBackendAdapter bridge, the
  event timeline of one turn, and the per-backend migration shape used
  through B2.

## Where it lives in the tree

- `src/yaah/agents/api_provider.py` — the protocol + adapter + helpers (NEW, B1)
- `src/yaah/agents/model_backend.py` — the legacy `ModelBackend` /
  `ToolBackend` Protocols (KEPT through Phase 1b; removed in B6 cleanup)
- Each backend in `src/yaah/agents/` and `src/yaah/adapters/backends/` —
  gains a native `stream()` method one at a time (B2)

## Migration order (B2)

Easiest first. The "easy" ones collapse to a single `text_delta` because
their source has no real wire stream; the work is mechanical. The hard
ones (LiteLLM, ClaudeCli) light up actual token streaming.

1. **`FakeBackend`** — done (B2.1). Canned responses → one `text_delta`.
2. **`ScriptedBackend`** — NEXT. Model-keyed canned responses; same shape as Fake.
3. **`FakeToolBackend`** — scripted `turn()` responses → text + tool events.
4. **`ScriptedToolBackend`** — model-keyed `turn()`; same shape as FakeTool.
5. **`LiteLLMBackend`** — translate litellm chunks → `StreamEvent` stream
   (the first native streaming source).
6. **`ClaudeCliBackend`** — parse `claude --output-format stream-json`
   (or fall back to delegating the loop entirely — see B3 in the
   resume context).
7. **`RoutingBackend`** — simplifies: `supports_turn` capability check
   goes away because every provider implements `stream()`. Stays as a
   prefix router; loses the dual-method surface.

Each step keeps `complete()` and `turn()` available at the backend level
so existing consumers don't break mid-migration. Once every backend is
native, B6 removes the legacy Protocols + `tool_loop.py` in one
intentional cleanup commit.

## The one-paragraph summary

`ApiProvider.stream(context, **opts)` returns an `AsyncIterator` of tagged
events: `start`, `text_delta`, `toolcall_end`, `done`, `error`. Module-
level helpers `complete(provider, prompt)` and `turn(provider, messages,
tools)` reproduce the legacy return shapes by draining a stream — so call
sites can migrate one at a time without big-bang renames. The
`LegacyBackendAdapter` wraps any current `ModelBackend`/`ToolBackend` as
an `ApiProvider` (non-streaming sources collapse to a single
`text_delta`), so the new protocol works against EVERY existing backend
from day one. Native `stream()` implementations replace the adapter
backend-by-backend as B2 progresses, gaining wire-level streaming as a
byproduct.
