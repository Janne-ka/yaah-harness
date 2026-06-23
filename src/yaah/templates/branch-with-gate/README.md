# starter (branch-with-gate)

Scaffolded from the `branch-with-gate` archetype. The pipeline drafts a
summary, parks for human review, then either publishes (approve) or
re-drafts (revise).

## Run

```bash
# Start the run; the human gate suspends.
python3 -m yaah.runtime starter.local.json

# See what is parked.
yaah list starter.local.json

# Deliver the decision (the file decision.json has {"decision": "approve"}).
yaah resume starter.local.json <baton-id> decision.json
```

## Adapt

- Change the prompt in `prompts/draft.md`.
- Change the decision shape: the gate uses `form: "approve_or_revise"`
  (see `docs/decision-forms.md`); swap to `free_text` or `json_schema` if
  the operator needs to provide structured revision content.
- Add more branch routes by adding entries to the `routes:` map.
- For the real provider, copy `starter.local.json` to `starter.real.json`,
  set `_extends: "starter.local.json"`, and swap `providers.fake` for
  `providers.claude` (with `by_model: null` to delete the inherited stub).

## Reference

- `examples/review-pipeline/` in the yaah repo — fuller version of this shape.
- `docs/archetypes.md` — what makes this archetype distinct.
- `docs/decision-forms.md` — gate decision shapes.
