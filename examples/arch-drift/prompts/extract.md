You are extracting the architecture of a software project from a snapshot of
its code. Your output is a Mermaid diagram a human will review.

# Repo snapshot

{{snapshot}}

# Prior human feedback (may be empty on the first pass)

{{feedback}}

# Task

Produce a Mermaid diagram (flowchart syntax) showing the project's architecture
at the package / subsystem level — NOT every module. Include the most important
dependency edges between packages, and group nodes by layer where the snapshot
makes the layering obvious.

Guidelines:

- 6–12 boxes is the target. Fewer is fine if the project is small; more than
  12 means you are documenting modules, not architecture.
- Use `subgraph` to group nodes that form a layer (e.g. "core", "harness",
  "adapters") when the snapshot supports it.
- Show direction of dependency with arrows: `A --> B` means A depends on B.
- If the prior human feedback is non-empty, address it specifically in this
  attempt — the feedback comes from someone who reviewed your previous output.

Reply with exactly ONE JSON object, no prose around it:

```json
{
  "mermaid": "flowchart TD\n  ...",
  "notes": "one paragraph explaining the layering choices you made and any uncertainties"
}
```

The `mermaid` value must be a valid flowchart that `mmdc` will render. The
`notes` value will be shown to the human reviewer next to the rendered diagram.
