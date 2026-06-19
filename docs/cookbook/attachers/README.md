# Cookbook: Attachers

Reference implementations of the [Attacher port](../../decisions/0003-attacher-port.md).

An *attacher* is a tiny post-invoke wrapper that reads tracer data the
agent just produced (token counts, latency, cache hits, retries, etc.)
and surfaces it into the in-flight payload so downstream stages can
branch on it. The engine ships zero attachers; consumers write or
copy their own.

## Entries

- [`usage.py`](usage.py) — `UsageAttacher`: tokens + model from the
  most recent `model_call` span. The minimum useful attacher; ~15
  lines including docstring.

## What makes a good attacher

Read the [ADR](../../decisions/0003-attacher-port.md) first; the
short version:

- **Read-only on the tracer.** An attacher consumes the span the
  tracer recorded. It does not call models, does not write trace
  records of its own.
- **Tiny return shape.** Return a *small* dict the consumer's
  downstream code can spread into the payload. The good ones return
  one key with a sub-dict.
- **Declare your captures.** `requires_capture` lists the contributor
  names you need (`"cost"`, `"latency"`, etc.). The builder
  capture-checks at LOAD time — a missing capture should fail with a
  clear "add this to your trace.capture" message before the pipeline
  ever runs.
- **No domain language.** `UsageAttacher` returns `tokens_in /
  tokens_out / model` — facts the tracer literally captured. Domain
  facts (cost-per-million, $-amount, "is this expensive?") belong in
  consumer code, not the attacher.

## What an attacher is NOT

- **Not a place for dollar pricing.** Cost-per-million-tokens varies
  by contract and changes; tokens-out is a property of the call.
  Multiply later, in app code, against a price-map the *consumer*
  controls. See the A/B variant of `examples/arch-drift` for the
  shape.
- **Not a way to mutate the envelope.** The Attacher protocol returns
  a dict; the wrapper merges it. Reach for `fn:` transforms if you
  need to *modify* the envelope structure.
- **Not for capturing data you didn't already record.** If you find
  yourself wanting "the prompt length the agent sent," teach the
  tracer to capture it via a contributor, then read it from the
  attacher.

## Copy-paste workflow

1. Open `usage.py` in this folder.
2. Copy the class into your project's `transforms.py` (or any
   importable Python module).
3. Keep the source-attribution comment at the top of the copy:
   `# Source: docs/cookbook/attachers/usage.py`. A future contributor
   reading your code can find the canonical version.
4. Reference it from your agent node:
   ```jsonc
   "role:my-agent": {
     "type": "agent",
     "model": "...",
     "attach": ["fn:transforms:UsageAttacher"]
   }
   ```
5. Add the matching capture to your tracer config:
   ```jsonc
   "trace": { "capture": ["cost"] }
   ```

## Proposing a new entry

A new cookbook entry is appropriate when:

1. You've actually written the attacher for *your* project (it solves
   a real problem in your code, not a hypothetical one).
2. The pattern is *general enough* that another project would copy it
   roughly verbatim — not specific to your stack.
3. The implementation is small (target: under 30 lines including
   docstring).

If those hold, open a PR adding `docs/cookbook/attachers/<name>.py`
plus an entry in the *Entries* list above. Keep the docstring sharp;
the value is in showing the shape, not in being exhaustive.
