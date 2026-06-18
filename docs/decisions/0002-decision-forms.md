# 0002 — Decision forms for human gates

**Status:** Accepted
**Date:** 2026-06-18

## Context

`human_gate` parks a run waiting for a decision. The shape of the decision
payload (`{"decision": "approve"}`? `{"answer": "..."}`? something else?) was
conventional knowledge between the pipeline author and whoever resumed the
gate — nowhere in the engine.

A driver-skill author trying to compose `decision.json` mechanically had to
interpret prose from the pipeline definition or the operator runbook. The
result, observed in a real upstream skill ([s_factory critique, item 5b](../../AGENTS.md)):
the skill defaulted to "guess by example" because there was no machine-readable
contract for the parked gate's expected decision shape.

The ask: give skills a parseable contract — but without the engine acquiring
opinions about app-specific decision payloads. A pipeline that needs the human
to confirm "should we deploy BUG-708 with hotfix bar?" has a domain decision
shape; the engine cannot and should not know about it.

What the engine **can** own is a small vocabulary of *generic* shapes —
"approve", "approve or revise", "free-text answer" — that recur across
unrelated pipelines. Cross-cutting value (every skill in every consumer
recognises the same shape) depends on that vocabulary being shared, which
means it must live in the engine.

## Decision

The engine ships a curated catalog of **decision forms** plus a CLI surface
to look one up by baton id:

- `src/yaah/harness/decision_forms.py` — the catalog (`FORMS`: name →
  `{schema, example}`). Initial set: `approve`, `approve_or_revise`,
  `free_text`, `json_schema`.
- `human_gate` accepts `form: "<name>"` (optional) and — for the `json_schema`
  escape hatch — `decision_schema: {...}` (inline JSON Schema). Validated at
  build time.
- `yaah baton-schema <root> <baton_id>` returns `{form, schema, example,
  baton_id, awaiting}` — the contract a skill composes against.

Extension is bounded:

- **Adding to the catalog** is a PR-level change. The bar: a shape that
  recurs across ≥3 unrelated pipelines and reads naturally as a generic verb
  (not `code_review_decision`; yes `rate_1_to_5`). Adding one commits the
  project to maintaining the name as a stable public API forever.
- **One-off shapes** use the `json_schema` form with an inline `decision_schema`.
  No catalog change, no PR, no commitment.
- **Consumer-defined forms registered at runtime via root config** are
  explicitly out of scope. The whole value of the catalog is that it's a
  shared vocabulary; the moment forms are consumer-defined, a skill that
  knew `approve_or_revise` can't recognise `consumer-X-approval` and is
  back to per-pipeline prose interpretation.

The catalog rules, the extension story, and the driver-skill flow are
documented for users in [`docs/decision-forms.md`](../decision-forms.md);
this ADR captures the *decision* (why the catalog exists at all, why these
rules, what the alternatives were).

## Consequences

### What this enables

- A driver skill that knows the four catalog forms can render an appropriate
  UI for any gate that uses one — across pipelines, across consumers,
  without per-pipeline prose interpretation.
- Pipeline authors can pick a form and inherit free skill compatibility, OR
  use the escape hatch for one-off shapes without engine changes.
- The boundary "engine owns generic vocabulary, consumer owns domain
  payloads" is concrete and inspectable in the catalog file.

### What this forbids

- The engine acquiring opinions about app-specific decision payloads
  (`bug708_review_decision` does not belong in `FORMS`).
- A registry mechanism that lets consumers inject custom forms at runtime
  via root config. Cross-cutting value depends on the catalog being
  curated, not extensible per-deployment.
- Engine-side validation of resumed decisions against the surfaced schema.
  Today `baton-schema` surfaces the schema for a skill to use; the engine
  does not check the decision against it. A future addition would be
  reasonable but stays optional — declared forms do not change the semantics
  of `resume`.

### What we expect to regret

- The catalog will grow more slowly than some users want. The bar of "≥3
  unrelated pipelines" is intentionally high; rejected proposals will land
  in consumer code via the escape hatch, which is the designed path but
  feels like a no to the proposer.
- The `form` value rides on `baton.pending.payload` (a payload key, not a
  dedicated `Baton` dataclass field). A future node that writes its own
  `form` payload key would collide. If that ever bites, the fix is to move
  `form` onto `Baton` itself — a small backward-compatible serialization
  change.
- The four initial forms reflect today's observed patterns. The first time
  a new generic shape clears the ≥3-pipelines bar will be a useful test of
  whether the rule is calibrated correctly.

## Related

- [`docs/decision-forms.md`](../decision-forms.md) — user-facing reference
  (catalog table, declaring a form on a gate, the driver-skill flow, both
  extension stories).
- [`src/yaah/harness/decision_forms.py`](../../src/yaah/harness/decision_forms.py)
  — the catalog (source of truth).
- [`docs/decisions/0001-three-concepts.md`](0001-three-concepts.md) §5
  "Budgets, not infinity" — the constraint this ADR justifies an exception
  against (a new harness-level concept; the budget rule mandates an ADR).
- [`AGENTS.md`](../../AGENTS.md) "Engine boundary" — the principle this
  catalog applies (the engine owns generic vocabulary; consumers own domain
  payloads).
