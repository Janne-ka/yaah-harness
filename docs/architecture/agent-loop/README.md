# agent_loop — architecture

`agent_loop` is YAAH's **harness primitive**: one node type that hosts a
bounded tool-use loop. An agent emits tool calls; the harness dispatches
them through the same `call_target` resolver transforms use; results
flow back as observations; the loop terminates when the agent emits a
final text or hits `max_turns`.

It is **one node type among many** in the DAG, not the whole story. The
DAG layer (pipelines, branches, gates, fanin, validators) is what makes
YAAH different from Cline / Kon / Pi — those are agent loops; YAAH has
an agent loop *as a stage*.

## In this folder

- [**Use cases**](use-cases.md) — six concrete scenarios showing what
  `agent_loop` enables. Each has a tool catalog, a pipeline shape, and
  honest limits.
- [**Flow**](flow.md) — call graphs and data flow. Three diagrams:
  pipeline context (where it fits), invoke flow (one stage's lifecycle),
  tool dispatch (the per-call resolver).
- [**Tools vs. stages**](tools-vs-stages.md) — the decomposition
  heuristic. When should work be a tool inside the loop vs. a stage
  in the DAG? The answer determines whether YAAH's unique-value
  proposition (DAG over loops) is realized or wasted.

## Related docs

- [`docs/harness-tool-use.md`](../../harness-tool-use.md) — the
  user-facing how-to. This folder is the implementer / architect view.
- [`docs/architecture/`](..) — broader architecture (sibling folders
  for other components as they get this treatment).
- [`docs/decisions/0004-parse-by-default.md`](../../decisions/0004-parse-by-default.md)
  — interaction with the data-flow contract.

## The one-paragraph summary

Author declares: backend + tool catalog (`name → {description,
input_schema, dispatch}`) + max_turns. The node accepts an input
envelope with `goal`, runs `backend.turn(messages, tools)` in a loop,
dispatches each emitted tool call via `call_target` (so `fn:`, `node:`,
`http:` all work uniformly), feeds results back to the agent, and
returns a result envelope `{answer, turns, outcome}`. Tool errors flow
back as observations; loop-internal errors crash the stage. Bounded by
`max_turns`. Workers-not-citizens: agent has agency only within the
catalog the author declared.
