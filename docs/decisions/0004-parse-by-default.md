# ADR-0004 — Agent parse-by-default

Status: accepted (2026-06-20)
Source: [`why-not-yaah.md`](../../.notes/why-not-yaah.md) §1.5;
[`design-parse-by-default.md`](../../.notes/design-parse-by-default.md);
shipped via the audit-paydown branch.

## Context

Before this ADR, an `agent` node returned its model text as a STRING in
`payload["raw"]`. To get parsed keys onto the payload, the user had to add
a `json_object` validator (for retry-on-bad-JSON) PLUS a `transform` with
`call: "envelope"` (for the parse-and-spread). Three stages where users
expected one.

The pattern showed up everywhere: `hello-yaah`, `review-pipeline`, every
scaffold template, every example. It was documented as a "rule that
bites" in AGENTS.md and as CHECK 8 in the pre-submission rubric.
[B1.1](../../.notes/why-not-action-plan.md) just shipped a load-time
graph linter that converts the trap from runtime-failure to
load-rejection — good, but the deeper question stood: **why does the
user have to author the parse step at all, when 90% of pipelines do the
same thing (extract JSON from `raw`, spread the result onto the
payload)?**

## Decision

The `agent` node has a new config key: `parse` (default `true`).

When `parse: true` (the default), the agent runs `extract_json` on its
model output and merges the parsed dict onto the reply payload (alongside
`raw`, which is kept for back-compat and debugging). On parse failure or
non-object JSON, the agent emits a `Verdict.failed` envelope of the same
shape `json_object` validator would have — so the harness's
retry+feedback loop catches it the same way.

When `parse: false`, the agent returns `{raw: <model text>}` only. The
data-flow graph linter (B1.1) then requires an explicit `transform` step
between the agent and any `render`/`branch` successor.

## Consequences

### Positive

- **Removes a stage from the common case.** The `hello-yaah` pipeline
  drops from 4 stages (agent → check → parse → render) to 2 (agent →
  render). Every example with an agent + json_object + transform parse
  collapses similarly.
- **Removes the data-flow footgun for the common case.** The validator
  + transform split that CHECK 8 existed to defend against is invisible
  to the user when parse-by-default does its job.
- **The retry+feedback loop still works.** Parse failures become failed
  verdicts; `max_attempts: N, feedback: true` keeps the agent
  reprompting on bad JSON.
- **`json_object` validator stays in the engine.** Useful for
  transform-output validation and for the (now opt-in) explicit
  pattern. Just no longer NEEDED on every agent.
- **Backwards compatible.** Existing pipelines with explicit
  `json_object` + `transform` parse stages keep working — the
  redundant inner parse is a no-op merge (same key, same value).
  Migration is a separate beat, not a forced upgrade.

### Negative / accepted tradeoffs

- **One new config key on `agent`** (`parse`). Surface grows by one
  knob to remove three stages from the typical pipeline. Net win.
- **Custom parsers still require an explicit transform.** If your
  agent's output isn't JSON-shaped (e.g. markdown sections), you opt
  out with `parse: false` and write a custom transform. The 10% case;
  acceptable.
- **One more thing to know about agents.** "parse=True default" is
  documented in AGENTS.md, the archetypes, the cookbook, the
  shape-grammar card. Agents (the docs) had room.

### What was rejected

Two alternatives from [`design-parse-by-default.md`](../../.notes/design-parse-by-default.md):

- **Shape B (auto-insert a transform node at build time).** Graph
  rewriting at build time would hurt debuggability — the user's
  JSON and the run graph would differ; trace would show
  `summarize__parse` stages the user didn't write. Magic for the
  surface-reduction win wasn't worth it.
- **Shape C (`parse_to_payload: [key, ...]` opt-in)** — trades one
  config burden for another. Doesn't dissolve the footgun for
  agents without the new key.

## Implementation

The shipping commit:

1. `src/yaah/agents/agent.py` — `Agent.__init__` accepts `parse: bool = True`;
   `invoke` runs `extract_json` + merges parsed dict, OR returns failed
   `Verdict` on bad JSON.
2. `src/yaah/build/builders.py:_build_agent` — reads `spec.get("parse", True)`
   and passes through.
3. `src/yaah/validate.py:_check_data_flow_contract` — skips the
   agent→render/branch flag when `parse` is True (the contract is
   satisfied by the agent itself).
4. All three scaffold templates (`linear`, `branch-with-gate`,
   `fork-fanin`) drop their `json_object` + `transform` parse stages.
5. `examples/hello-yaah`, `examples/review-pipeline`,
   `examples/fork-join` migrated.
6. Test fixtures migrated: tests using fake non-JSON responses opt out
   via `parse=False`.

### Deferred

- `examples/arch-drift` and `examples/config-flow` migration: their A/B
  variants compose `parse` with `attach: [...]` attachers; that
  interaction wants its own verification beat. Tracked in
  [`todos.md`](../../.notes/todos.md).
- The `json_object` validator's continued necessity. Once the
  migration completes, a follow-up may flag `json_object` as
  rarely-needed and recommend `parse: true` in its docstring.

## Cross-references

- [`why-not-yaah.md`](../../.notes/why-not-yaah.md) §1.5 — the original
  critique.
- [`design-parse-by-default.md`](../../.notes/design-parse-by-default.md)
  — the design exploration that picked Shape A.
- [B1.1](../../.notes/why-not-action-plan.md) — the load-time
  graph-linter this ADR composes with.
- [ADR-0001](0001-three-concepts.md) — three concepts (Envelope, Node,
  Comms). Parse-by-default adds zero concepts; it adds one config knob
  to an existing node type.
