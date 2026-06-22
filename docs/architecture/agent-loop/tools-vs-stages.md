# Tools vs. stages — the decomposition heuristic

The architectural question: when should work be a TOOL inside
`agent_loop` vs. a separate STAGE in the DAG?

This page exists because `agent_loop` can dispatch tools via
`node:role` (which invokes any other YAAH node), which means it
*could* subsume the harness — author puts everything as tools, DAG
becomes decorative. The DAG layer's value depends on the author's
decomposition choice. This page makes the heuristic explicit.

## Both options work — the question is when

Tool dispatch supports three schemes via `call_target`:
- `fn:module:func` — direct in-process call
- **`node:role` — invoke any other YAAH node via Comms**
- `http(s)://...` — external HTTP

So you CAN make scout a tool: `tools: {scout: {dispatch:
"node:my_scout"}}`. Or you CAN make scout a stage: `[scout] →
[agent_loop] → [render]`. Both are buildable today.

The question: which is the right pattern when?

## The heuristic

| Use a **tool** when | Use a **stage** when |
|---|---|
| The agent should DECIDE whether to call | The work should ALWAYS happen at this point |
| The call is small / fast (microseconds–low-ms) | The call is expensive or has clear dependencies |
| The agent's reasoning benefits from interactivity | You want operator visibility / A/B-ability |
| The work is internal to "doing the task" | The work is decomposition / orchestration |
| The call's cost varies with agent reasoning | You want predictable per-stage cost |
| Failure is recoverable mid-loop | Failure should park the pipeline (gate / retry) |

## What you GAIN with stages

The DAG layer earns its value through four properties:

1. **Per-stage model selection** — `scout` can use haiku ($0.003/call),
   `actor` can use sonnet ($0.03/call). Inside agent_loop everything
   uses the same backend.
2. **Operator visibility** — each stage appears in `yaah list`, in
   the trace, in `yaah ab` comparisons. Tools inside a stage are
   buried in that stage's trace span.
3. **A/B-ability** — swap one stage's variant; rest unchanged. Tools
   inside a loop can't be swapped without changing the loop's config.
4. **Forced structure** — the author commits to "validate runs
   between actor and gate." A tool the agent might-or-might-not
   call doesn't enforce structure.

## What you GAIN with tools

The loop's tool catalog earns its value through three properties:

1. **Agent agency** — the agent decides what to call based on what
   it's discovered so far. A stage that always runs can't react to
   what came before.
2. **Cheap composition** — a `fn:` tool is microseconds; a `node:`
   stage transition has envelope construction + comms routing.
3. **Tight coupling to reasoning** — a tool result feeds back into
   the next turn of the same agent's reasoning. A stage break
   loses that continuity.

## Worked example: scout

**Pipeline scout (stage):**
```
[input] → [scout: agent_loop, haiku, READ-ONLY tools]
        → [prefetch: transform, deterministic, no LLM]
        → [actor: agent_loop, sonnet, EDIT tools, pre-loaded context]
        → [output]
```
- Predictable cost (every run does scout)
- A/B-able (swap scout's prompt, compare)
- Operator-visible (scout's findings show in trace)
- Forces decomposition (clear scout-vs-actor boundary)

**Tool scout (tool inside the actor):**
```
[input] → [actor: agent_loop, sonnet, EDIT tools + scout tool]
        → [output]
```
Where `tools.scout: {dispatch: "node:scout_agent"}`.
- Flexible (agent decides when to call scout)
- Loop-internal (no operator visibility per scout call)
- Variable cost (cheap if scout not needed; expensive when called multiple times mid-loop)
- Less forced structure (agent may not scout at all)

**Use the pipeline-scout pattern when** scouting is universally
beneficial (always-prefetch-first tasks) and you want A/B-ability
on the scout strategy itself.

**Use the tool-scout pattern when** scouting is occasionally
beneficial (agent should decide if/when) and you accept the
operator visibility tradeoff.

## Worked example: validation

**Pipeline validator (stage):**
```
[actor: agent_loop] → [json_object: validator] → [render: output]
```
- Validation always runs
- Failed validation triggers retry-with-feedback (built-in mechanic)
- Operator sees pass/fail per stage

**Tool validator (tool inside the actor):**
```
tools: {check_my_work: {dispatch: "node:json_validator"}}
```
- Agent decides when to validate
- Validation cost is per-call inside the loop
- Loop continues regardless of validation result
- Operator doesn't see validation as a distinct event

**Almost always: validation should be a stage.** Validation's value
is in the FORCED check + retry-with-feedback mechanic. As a tool
the agent might skip it.

## Worked example: tool dispatch (recursion)

Can a tool inside an `agent_loop` call ANOTHER `agent_loop`?

Yes — via `tools: {sub_task: {dispatch: "node:other_loop_stage"}}`.
The sub-loop runs as a normal stage, returns its result envelope,
and the outer loop sees the result as an observation.

**When this is useful:**
- Long tasks decomposed into sub-tasks the agent identifies
- Sub-loops with different tool catalogs (parent has edit perms;
  sub-loop has read-only)
- Recursive coding patterns (parent plans; sub-loop executes one
  step at a time)

**When this is dangerous:**
- Cost amplification (each tool call may spin its own loop)
- Depth without bound (the agent may keep recursing)
- Lost operator visibility (nested loops bury work)

**Recommendation:** for recursion-shaped work, prefer the DAG
expression (`fork` to multiple sub-loops + `fanin` to aggregate)
over the tool-inside-loop expression. The DAG gives bounded
parallelism + visibility.

## The honest warning

Without intentional decomposition, `agent_loop` becomes the harness
— author puts everything as tools, the DAG is decorative. The
unique value YAAH offers (DAG over agent loops) is lost in that
configuration.

**The default for authoring should be: stages first, tools second.**

When in doubt:
- Is this a primitive operation (read a file)? → tool
- Is this a phase of work (review, validate, approve, render)? → stage
- Is this something a human operator should see as a checkpoint? → stage
- Is this something the agent should choose to invoke or skip? → tool
- Is this something whose cost / model / config you want explicit
  control over? → stage

The pipeline DAG is where YAAH earns its keep vs. Cline / Kon / Pi.
Use it deliberately.

## Where to read more

- [`flow.md`](flow.md) — the per-stage invoke flow + tool dispatch
- [`use-cases.md`](use-cases.md) — three common pipeline shapes
  showing the decomposition choice in practice
- [`.notes/harness-offload-patterns.md`](../../../.notes/harness-offload-patterns.md)
  — the nine offload patterns; most use stage-level decomposition
