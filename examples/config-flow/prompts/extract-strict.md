You are visualizing how a yaah CONFIGURATION FLOWS through the system. Your
output is a Mermaid `flowchart` diagram. Not a class diagram, not a sequence
diagram — `flowchart` only.

# Config-flow snapshot

{{snapshot}}

# Prior human feedback (may be empty on the first pass)

{{feedback}}

# Task

Produce a Mermaid `flowchart TD` with **exactly four `subgraph` groups**,
named exactly as below, in this order. Each subgraph has a required
layout direction; respect it. Do not invent additional subgraphs. Do not
omit any of the four (use a placeholder note if a section is empty).

```
flowchart TD

  subgraph ExtendsChain ["Extends chain (base → leaf)"]
    direction LR
    %% horizontal: each config in the _extends chain, base first, leaf last
    %% one box per layer; box label = file basename + a 1-line summary of
    %% what that layer adds/overrides (NOT the full _doc).
    %% solid arrows base --> leaf.
  end

  subgraph Pipeline ["Pipeline graph"]
    direction TB
    %% the pipeline's nodes wired by the graph (then / fork / fanin / branch).
    %% one box per stage; show fork/fanin if present.
    %% solid arrows for control flow.
    %% if the pipeline has branches, label the edges with the branch values.
  end

  subgraph Registries ["Effective root: registries"]
    direction TB
    %% one box per named entry in providers / prompt_sources / data_sources /
    %% data_sinks / mcp_sources / state / transport / trace.
    %% group label = "<map>.<name>: <type>" (e.g. "providers.claude: claude_cli").
    %% no arrows inside this subgraph — these are config values, not flow.
  end

  subgraph Fixture ["Input fixture"]
    direction TB
    %% one box per top-level key in the fixture (target_config_path,
    %% repo_path, snapshot_strategy, etc.); brief value summaries OK.
  end

  %% cross-subgraph edges, AFTER all subgraphs are declared:
  %% - SOLID arrow from Fixture → the Pipeline's START stage (the entry point).
  %% - DASHED arrows (-.->) from each Pipeline node that uses a `model:` /
  %%   `prompt:` / `source:` / `sink:` / `mcp:` string TO the matching box
  %%   in Registries. These are reference-resolution edges; they must be
  %%   dashed and they must cross the subgraph boundary.
  %% - DASHED arrow from the ExtendsChain's LEAF box to the Pipeline subgraph
  %%   to indicate "this effective config is what runs the pipeline."
```

# Rules (do not violate)

- **Use `flowchart TD` syntax only.** No `classDiagram`, no `graph TB`,
  no `stateDiagram`. The renderer is `mmdc` and it must produce a flowchart.
- **Exactly four subgraphs, in the order and with the IDs/titles given
  above.** ExtendsChain is `direction LR`; the other three are `direction TB`.
- **Solid arrows (`-->`) for control flow and inheritance.** Dashed
  arrows (`-.->`) for reference resolution (a string in one config refers
  to a name registered in another).
- **No HTML in node labels.** Plain text only. Keep labels under ~40 chars
  each; one-line summaries beat verbose ones.
- **8–20 boxes total.** If the snapshot is small, fewer is fine; if it
  has fork/fanin/multiple branches, more is fine. Do not pad.
- **Address prior feedback if any.** Quote the specific instruction you
  applied in the `notes` field.

# Reply

ONE JSON object, no prose around it, no markdown fences:

```json
{
  "mermaid": "flowchart TD\n\n  subgraph ExtendsChain [\"Extends chain (base → leaf)\"]\n    direction LR\n    ...\n  end\n\n  subgraph Pipeline [\"Pipeline graph\"]\n    ...\n  end\n  ...",
  "notes": "one paragraph: what this config does, which choices in the diagram are interesting, anything you couldn't resolve cleanly"
}
```

The `mermaid` value must parse with `mmdc -i input.mmd -o out.svg` without
errors. The `notes` value is operator-facing.
