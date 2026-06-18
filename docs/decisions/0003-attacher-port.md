# 0003 — The `attacher` port for in-flight payload data

**Status:** Accepted
**Date:** 2026-06-18

## Context

YAAH's tracer captures observability data (token usage, model name,
latency, tool calls) as spans and routes it to sinks. The CostContributor
([`src/yaah/trace/contributors/cost.py`](../../src/yaah/trace/contributors/cost.py))
projects `{tokens_in, tokens_out, model}` onto each model_call span;
dollar pricing is deliberately external (a config price-map applied by
the aggregator, so history can be re-priced without touching the engine).

That data is **observability** — visible after a run via `yaah trace
<jsonl>`. It is **not** visible to *in-flight* decision-making — a
downstream stage that wants to branch on cost, route to a cheaper model,
or surface the per-stage cost in a report cannot read it from the
payload, because the agent's output is just the model's reply.

Concrete use case driving this ADR: **A/B model comparison.** Two
agents run in parallel via `fork`+`fanin`; the report shows tokens
spent by each candidate; the human picks. To do this without
hand-rolled "read the trace JSONL from a sidecar transform" code, the
in-flight payload needs access to what the tracer captured.

The same need will appear for budget enforcement (kill at $X), dynamic
model routing (start cheap, escalate on validator fail), and ops cost
reporting per stage. The pattern is general: *observability data the
tracer already captures, surfaced onto the in-flight payload for
operational decisions*.

## Decision

Add an opt-in `attach: [...]` config key to the `agent` node and a small
`Attacher` port. **The engine ships zero built-in attachers.** Users
implement attachers in their own code and reference them as
`fn:module:func` — the same idiom yaah already uses for transforms.

### Why zero built-ins (not one)

An earlier draft of this ADR shipped `UsageAttacher` as the one built-in
in `src/yaah/agents/builtin_attachers.py`. The reasoning was "every
advanced user wants tokens/model on the payload; shipping it once gives
everyone the convenience of `attach: ['usage']`."

That was wrong. The pattern yaah already follows for transforms is
"engine ships the *mechanism* (the `transform` node + `fn:module:func`
resolver); consumers implement every transform in their own
`transforms.py`." There is no built-in `parse` transform; every consumer
writes their own. Attachers fit the same pattern.

Shipping the one built-in:

- Creates a fake decoupling between AttachingAgent and cost-knowledge
  (the wrapper claims to be cost-agnostic, but the one shipped built-in
  IS cost — pure theater).
- Forces an arbitrary "but only ONE" slippery-slope guard, where the
  next person who wants `latency` has to write an ADR amendment.
- Adds a `BUILTIN_ATTACHERS` registry to the engine that must coexist
  with `fn:` resolution.
- Reserves a `_yaah_*` payload prefix that has nothing to reserve once
  there are no engine-attached keys.

Shipping zero built-ins:

- Matches the transforms pattern exactly.
- Eliminates the slippery slope (no slope to slip down).
- Removes the registry; only one resolution path (`fn:`).
- Eliminates the prefix-reservation question.
- The reference `usage` implementation lives in
  `examples/arch-drift/transforms.py` as the canonical demo.

### Surface

```json
"role:extract": {
  "type": "agent",
  "model": "claude:claude-sonnet-4-6",
  "prompt": "file:extract",
  "attach": ["fn:transforms:usage"]
}
```

Each list item is a `fn:module:func` string. Same syntax as
`target: "fn:transforms:parse"` on a transform node. Same `import_callable`
resolver. The resolved callable must be a subclass of `Attacher`; the
builder instantiates it.

### Port

`src/yaah/agents/attacher.py`:

```python
class Attacher:
    """Read an agent's recent execution and return payload keys to merge.
    Pure: no I/O, no side effects.

    Subclass and override `attach()`. Configuration declares attachers
    by `fn:module:func` reference — the engine ships no built-ins.
    The canonical reference implementation is the `usage` attacher in
    `examples/arch-drift/transforms.py`.

    Implementations may declare `requires_capture` — a tuple of tracer
    capture names. The builder enforces that the configured tracer has
    all required captures, so the attacher never silently returns {}.
    """
    name: str = ""
    requires_capture: tuple = ()

    def attach(self, envelope: Envelope,
               span: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError
```

Same shape as the existing tiny ports in `src/yaah/trace/contributors/`
(`CostContributor`, `PhaseContributor`, `ToolsContributor`). Not a new
pattern; parallels precedent.

### Wrapper

`src/yaah/agents/attaching_agent.py` wraps an `Agent`. After
`inner.invoke()`, reads the tracer's most recent `model_call` span **for
this correlation** (not the global last span — concurrent runs share the
buffer), runs each configured attacher, merges the results onto the
output envelope's payload. Per-correlation lookup is mandatory to avoid
nested-agent / broker races.

Base `Agent` is unchanged — knows nothing about cost, tokens, or
attachers. Wrapping happens in the builder when `attach: [...]` is
present and non-empty.

### Tracer extension

The `Tracer` protocol gets one new method:
`last_model_call_span(correlation_id: str) -> Optional[Dict[str, Any]]`.

- `NullTracer` returns `None`.
- `RecordingTracer` filters its buffer by correlation, returns the most
  recent `model_call` span's projected record.
- `EnvelopeTracer` filters `_by_corr[corr]`, same.

### Hard rule

> The engine ships **zero** built-in attachers. The contribution is the
> *port* (one class, one method) + the wrapper + the tracer extension.
> All attacher implementations live in consumer code, referenced as
> `fn:module:func`.
>
> Adding a built-in attacher to the engine requires a new ADR that
> amends this one and justifies why the implementation cannot live in
> the consumer's own code.

### Coexistence with the existing cost stack

The cost contributor + aggregator + `yaah trace` aggregator are
**unchanged**. The attacher is a *read-path* onto the same data, exposed
in-flight to the payload.

| Source | Shape | When | Authority |
|---|---|---|---|
| Trace JSONL via aggregator | Run total + per-stage breakdown | After the run | **Canonical** |
| Attacher-attached payload key | This-stage snapshot | In-flight | Decision-input only |

If the two diverge (cache hits counted differently, retries, rounding),
the trace wins. The payload-attached value is *the snapshot a downstream
branch saw at decision time*.

## Consequences

### What this enables

- A/B model comparison: two agents run in parallel, each attaches its
  own usage, the report shows tokens-per-candidate, the human picks.
- Budget enforcement: a downstream transform sums attached usage across
  fork branches and short-circuits if over a threshold.
- Dynamic model routing: a small classifier reads the previous stage's
  attached data and chooses the next model.
- Ops cost reporting in-flight without scraping the trace.
- User-written attachers for application-specific data (latency
  budgets, cache hit ratios, tool-call counts) without engine changes.

### What this forbids

- Engine-side attachers of any kind (the hard rule).
- Stage-level `after: [...]` hooks. If a use case needs more than what
  an attacher can do, that's a different ADR.
- Flag-shaped expansion (`attach_usage: true` + `attach_latency: true`
  + ...). One mechanism, list of items.
- Engine-computed dollar cost in the attacher (pricing stays in the
  aggregator's price-map per yaah's existing rule).

### What we expect to regret

- Each consumer that wants `usage` rewrites the same 10-line attacher.
  Acceptable cost — every yaah consumer also rewrites their own `parse`
  transform; same pattern. Mitigated by the canonical reference
  implementation in `examples/arch-drift/transforms.py` (copy-paste).
- Tracer requirement is now load-bearing for any pipeline using
  `attach: [...]`. The builder rejects at load with a concrete
  `trace: {...}` snippet to add. Visible at config level, but the
  hello-yaah pipeline cannot demonstrate the headline feature
  (acceptable — hello-yaah is the minimal demo, the A/B example is
  the cost-aware demo).
- Per-correlation span lookup assumes a tracer that buffers spans by
  correlation. `NullTracer` cannot serve attachers (correct, it
  rejects); a custom tracer that flushes spans synchronously to a sink
  without buffering would also fail. Documented; tracer authors who
  want to serve attachers buffer per-corr.

## What this commits us to

- `Attacher` class shape (`name`, `requires_capture`, `attach(envelope,
  span)`) is a stable public contract.
- `Tracer.last_model_call_span(correlation_id)` is a stable accessor
  on the Tracer protocol.
- `attach` config key on the `agent` node accepts `fn:module:func`
  string items forever (changing the item shape would migrate every
  pipeline that uses it).
- Zero built-in attachers — adding one requires a new ADR that amends
  this one.

## Related

- [`docs/decisions/0001-three-concepts.md`](0001-three-concepts.md) §5
  "Budgets, not infinity" — the rule this ADR works within (no new
  node type added; one new opt-in config key; one new tiny port; zero
  built-ins shipped).
- [`docs/decisions/0002-decision-forms.md`](0002-decision-forms.md) —
  the precedent for "engine ships a curated catalog; user-defined
  extensions via escape hatch" applied to gate decision shapes. This
  ADR goes further: zero catalog, port only — because attachers are
  not a shared vocabulary the way decision forms are.
- [`src/yaah/trace/contributors/cost.py`](../../src/yaah/trace/contributors/cost.py) —
  the data source the reference `usage` attacher surfaces.
- [`src/yaah/trace/aggregate.py`](../../src/yaah/trace/aggregate.py) —
  where dollar pricing lives (price-map), which the attacher
  deliberately does NOT duplicate.
- [`AGENTS.md`](../../AGENTS.md) "Engine boundary" — the principle
  this ADR works under (the engine owns generic mechanism; consumers
  own domain-specific decisions about what to monitor and how to
  price).
- [`examples/arch-drift/transforms.py`](../../examples/arch-drift/transforms.py) —
  the reference `usage` attacher implementation, copy-paste-able into
  any consumer's own transforms file.
