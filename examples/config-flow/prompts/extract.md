You are visualizing how a yaah configuration FLOWS through the system. Your
output is a Mermaid diagram a human will look at.

# Config-flow snapshot

{{snapshot}}

# Prior human feedback (may be empty on the first pass)

{{feedback}}

# Task

Produce a Mermaid `flowchart` that shows the **configuration flow**, not
the code architecture. The diagram should make these relationships visible
at a glance:

1. **The `_extends` chain** — each config file in the chain as a box;
   arrows from child to parent showing inheritance direction; brief notes
   on what each layer adds or overrides.
2. **The pipeline graph** — as a subgraph, showing the stages (snapshot,
   extract, parse, …) and how they connect (then / fork / fanin / branch).
3. **Reference resolution** — dashed arrows from pipeline nodes that use
   `model:` / `prompt:` / `target:` strings BACK to the effective root's
   providers / prompt_sources / etc.
4. **The fixture** — as a box feeding into the start stage.

Guidelines:

- Group related items with `subgraph` (e.g., "extends chain", "pipeline",
  "providers", "fixture") so the structure is readable.
- Use solid arrows for control flow / direct reference; dashed arrows
  (`-.->`) for reference resolution (string → registry).
- Aim for 10–20 boxes total. Fewer if the config is simple; more is fine
  if there's a lot to show.
- If the prior human feedback is non-empty, address it specifically.

Reply with exactly ONE JSON object, no prose around it:

```json
{
  "mermaid": "flowchart TD\n  ...",
  "notes": "one paragraph explaining the diagram and any uncertainties"
}
```

The `mermaid` value must be a valid flowchart that `mmdc` will render. The
`notes` value will appear next to the rendered diagram.
