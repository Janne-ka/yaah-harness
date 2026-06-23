# starter (fork-fanin)

Scaffolded from the `fork-fanin` archetype. Three independent agents (lenses)
review the same input in parallel; a reducer merges their findings into one
report.

## Run

```bash
python3 -m yaah.runtime starter.local.json
# → produces report.html with all three lenses' findings
```

## Adapt

- Add or remove lenses: every lens has THREE matching places — a `nodes:`
  entry, a stage in `graph.stages`, and an entry in `fork:` AND in
  `fanin.expect`. The `fanin.expect` list takes FORK BRANCH names (not the
  names of the last stage in each branch).
- Change the reduce logic in `transforms.py:merge`. It receives
  `{branch_id: payload}`; return a dict that spreads onto the next stage's
  input.
- For the real provider, copy `starter.local.json` to `starter.real.json`,
  set `_extends: "starter.local.json"`, and swap `providers.fake` for
  `providers.claude` (with `by_model: null` to delete the inherited stub).

## Reference

- `examples/fork-join/` in the yaah repo — fuller version of this shape.
- `docs/archetypes.md` — what makes this archetype distinct.
