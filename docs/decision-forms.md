# Decision forms — the shared vocabulary for human gates

A `human_gate` node parks a run waiting for a decision. Until now, the shape of
that decision (`{"decision": "approve"}`? `{"answer": "..."}`?) was conventional
knowledge between the pipeline author and whoever resumes the gate — nowhere in
the engine. A driver skill that wanted to compose `decision.json` mechanically
had to interpret prose.

**Decision forms** are a small, curated catalog of generic decision shapes the
engine ships. A gate declares the form it expects; the CLI surfaces the form's
JSON Schema via `yaah baton-schema`; a skill composes the decision from the
schema (or from the included `example`) without guessing.

The engine owns **only the generic vocabulary**. Domain-specific decision
payloads stay in the consumer via the `json_schema` escape hatch.

## The catalog

| Form | Decision shape | Use when |
|---|---|---|
| `approve` | `{"decision": "approve"}` | A one-button "yes, continue" gate. Reject is implicit-via-timeout or by skipping the resume. |
| `approve_or_revise` | `{"decision": "approve" \| "revise", "feedback": "string?"}` | A review gate where `revise` sends `feedback` back to the previous stage. |
| `free_text` | `{"answer": "string"}` | A free-form question gate (e.g., "what should this variable be called?"). |
| `json_schema` | inline schema | Escape hatch: a one-off gate whose decision shape doesn't fit any catalog entry. The pipeline author provides `decision_schema` inline. |

The catalog lives in [`src/yaah/harness/decision_forms.py`](../src/yaah/harness/decision_forms.py).
Tests verify each form's `example` validates against its own `schema`.

## Declaring a form on a gate

In the pipeline JSON, on a `human_gate` node:

```json
"role:review": {
  "type": "human_gate",
  "ask": "Approve the spec, or send feedback to revise?",
  "awaiting": "human:spec-review",
  "form": "approve_or_revise"
}
```

The escape hatch for a one-off:

```json
"role:grill": {
  "type": "human_gate",
  "ask": "answer the grill",
  "awaiting": "human:grill",
  "form": "json_schema",
  "decision_schema": {
    "type": "object",
    "properties": {
      "verdict": {"enum": ["dismissed", "addressed", "accepted"]},
      "rebuttal": {"type": "string"}
    },
    "required": ["verdict"]
  }
}
```

The builder (`build/builders.py::_build_human_gate`) rejects at load time:
- a `form` value that isn't a catalog entry,
- `form: "json_schema"` without `decision_schema`,
- `decision_schema` on any form other than `json_schema`.

`form` is optional — a gate that omits it works exactly as before (no `form`
field on the AWAIT envelope, no schema surfaced by `baton-schema`). Adding the
field is a purely additive UX upgrade; existing pipelines do not need to change.

## Driver-skill flow

```bash
yaah list <root> --json
# {"batons": [{"id": "b-42", "stage": "review", "awaiting": "human:spec-review", ...}]}

yaah baton-schema <root> b-42
# {
#   "form": "approve_or_revise",
#   "schema": { "type": "object", "properties": {...}, "required": ["decision"] },
#   "example": {"decision": "approve"},
#   "baton_id": "b-42",
#   "awaiting": "human:spec-review"
# }

# the skill composes decision.json (either from example, or by filling the schema)
echo '{"decision": "revise", "feedback": "tighten the third paragraph"}' > /tmp/d.json
yaah resume <root> b-42 /tmp/d.json
```

A skill that knows the four catalog forms can render an appropriate UI for any
gate that uses one — across pipelines, across consumers. That common UX is the
whole reason the catalog lives in the engine instead of in each consumer.

## Engine boundary — what's in scope and what isn't

**In scope for the engine:**
- The catalog of *generic* decision shapes.
- The mechanism for surfacing them (`baton-schema` CLI; `form` field on the
  AWAIT envelope).
- The escape hatch for one-off shapes (`json_schema` form + inline
  `decision_schema`).

**Not in scope, and won't be:**
- App-specific decision payloads (the `bug-708-review-decision` shape with
  fields `foo` and `bar`). Those belong in the consumer's pipeline, not the
  engine catalog. Use the `json_schema` escape hatch.
- A registry mechanism for consumers to inject custom forms at runtime via root
  config. That would break the cross-cutting value of a shared vocabulary —
  the moment forms are consumer-defined, a skill that knew `approve_or_revise`
  can't recognize `consumer-X-approval` and is back to per-pipeline prose
  interpretation. If a shape recurs across three unrelated pipelines, upstream
  it as a catalog entry (see below); if it's one-off, use the escape hatch.
- Decision *validation* by the engine. `yaah baton-schema` SURFACES the schema
  for a skill to use; today the engine does not check the resumed decision
  against it. (A future addition would be reasonable; it stays optional.)

## Extending the catalog (open a PR)

**When to upstream a new form:**
- The same decision shape recurs across **at least three unrelated pipelines**.
- The shape is genuinely generic (not domain-flavored — `code_review_decision`
  is too specific; `rate_1_to_5` is fine).
- The name reads naturally in pipeline JSON (`form: "..."`).

**When NOT to upstream:**
- The shape is one-off — use `json_schema` inline.
- The shape is a near-variant of an existing form — extend the existing form
  if possible (e.g., adding an optional `priority` field to `approve_or_revise`)
  rather than minting a new one.
- The shape carries domain vocabulary (`form: "grill_answer"` does not belong
  in the engine catalog — what's a grill? That's s_factory's vocabulary).

**How to extend:**
1. In [`src/yaah/harness/decision_forms.py`](../src/yaah/harness/decision_forms.py),
   add a new entry to the `FORMS` dict: `{"my_form": {"schema": {...}, "example": {...}}}`.
   The schema uses the JSON-Schema subset yaah's own validator understands
   (`type`, `enum`, `required`, `properties`, `items`). `additionalProperties: false`
   is encouraged for the benefit of consumer skills using a full validator.
2. In [`tests/test_decision_forms.py`](../tests/test_decision_forms.py), add no
   new test code — the existing loop iterates `FORMS` and asserts every form's
   `example` validates against its own `schema`. If your new entry passes, the
   suite passes. If it fails, fix the entry, not the test.
3. Update this file (add a row to the catalog table; expand the "Use when"
   column with the case that justified the addition).
4. PR description must answer: *which three unrelated pipelines need this
   shape, and why does the `json_schema` escape hatch not suffice for them?*
   A vague answer rejects.

**What you commit yaah to:** a new form is a stable public vocabulary tag.
Removing or renaming a form is a breaking change for every skill that knows it.
Add forms sparingly; remove them never.

## Using the inline escape hatch (no PR needed)

If your gate's decision shape is one-off, use the `json_schema` form with an
inline `decision_schema`. The CLI surfaces it the same way as a built-in:

```json
"role:audit": {
  "type": "human_gate",
  "ask": "approve the audit?",
  "form": "json_schema",
  "decision_schema": {
    "type": "object",
    "properties": {
      "finding": {"enum": ["pass", "fail"]},
      "evidence_url": {"type": "string"},
      "notes": {"type": "string"}
    },
    "required": ["finding", "evidence_url"]
  }
}
```

The inline schema must use the same JSON-Schema subset as the catalog (`type`,
`enum`, `required`, `properties`, `items`). It cannot reference `$defs` from
external files; it cannot contain `fn:` / `mcp:` style references (those would
be code-execution surface in a security-sensitive seam — see
[AGENTS.md "Security"](../AGENTS.md)).

The escape hatch carries no upstream-PR cost and no commitment. A pipeline can
change its inline `decision_schema` freely between releases.

## Related

- [`decisions/0002-decision-forms.md`](decisions/0002-decision-forms.md) — the
  ADR that records the decision behind this catalog (why it exists, what was
  rejected, what we expect to regret).
- [`yaah list --json`](root-config-reference.md) — the parseable mailbox view a
  driver skill reads before calling `baton-schema`.
- [`yaah baton-schema <root> <baton_id>`](root-config-reference.md) — emits the
  decision-form schema for one parked baton.
- [AGENTS.md "Engine boundary"](../AGENTS.md) — why the engine does not own
  app-specific schemas (or `run_dir`, or `task_dir`).
- [`src/yaah/harness/decision_forms.py`](../src/yaah/harness/decision_forms.py) —
  the catalog (source of truth).
