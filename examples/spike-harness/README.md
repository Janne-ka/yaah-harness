# spike-harness — YAAH as a real harness (Phase 1a smoke example)

A minimal `agent_loop` pipeline running end-to-end against a scripted
`fake_tool` backend. Proves the harness primitive works: agent emits
tool call → harness dispatches via `call_target` → result flows back
→ next turn → eventually a final answer.

## What this demonstrates

YAAH had an `ApiProvider` seam whose `stream()` a loop drives, but no
node that drove that loop. Phase 1a closes that gap:

- `src/yaah/nodes/agent_loop_node.py` — the loop. Bounded by
  `max_turns`. Tool catalog is **author-declared** (preserves
  workers-not-citizens; the agent has agency only within the fence
  the pipeline author built).
- `src/yaah/build/builders.py` — `_build_agent_loop` is wired into
  the default registry, so `"type": "agent_loop"` loads.
- `src/yaah/adapters/providers/fake_tool_provider.py` — scripted
  backend that drives the loop with canned turn responses. Proves
  the protocol seam is replaceable.
- `src/yaah/runtime_factories.py` — `fake_tool` registered as a
  provider type alongside `fake`, `fake_scripted`, `claude_cli`,
  `litellm`.

## Run it

```bash
yaah run examples/spike-harness/local.json
```

(Not installed? `python3 -m yaah.runtime examples/spike-harness/local.json`
is the equivalent; from a source checkout prefix `PYTHONPATH=src`.)

Expected: three scripted turns (read_file → done → final), result
envelope with `answer: "Task complete."`, `turns: 3`,
`outcome: "completed"`.

## What stayed inside the architecture

- **Workers-not-citizens** — the agent can only call tools the
  author put in the catalog. No MCP discovery, no improvisation.
- **AI is layered, optional, replaceable** — `agent_loop` lives
  in `nodes/` (protocol-bound, not impl-bound, same layer as
  `Agent`); FakeToolProvider lives in `adapters/providers/`.
  Engine works without any specific backend.
- **Compose, don't invent** — tool dispatch reuses `call_target`
  (the same machinery transforms use). No new dispatch concept.
- **Prompts in files** — `system_prompt: "file:agent"` resolved
  via the prompt source (lazy, on first invoke).

## Honest scope

Phase 1a deliberately keeps things small:

- Backend is **scripted-only** (`fake_tool`). A real
  `claude_cli_provider.stream()` implementation **shipped in 1b**.
- Tools are `fn:` dispatch only here. `node:`, `http:`, and a
  future `mcp_tool` adapter node would compose without engine
  change.
- No streaming, no parallel-dispatch, no compaction, no cost
  capture. Phase 1b + later phases add these where measurement
  shows they matter.

## How to review

Read in this order (~150 lines of new code total):

1. `src/yaah/nodes/agent_loop_node.py` — the loop body (~100 lines)
2. `src/yaah/build/builders.py` — `_build_agent_loop` + registration
   (~45 lines added)
3. `src/yaah/adapters/providers/fake_tool_provider.py` — scripted
   backend (~50 lines, unchanged from spike)
4. `examples/spike-harness/pipeline.json` — author's declaration
5. `examples/spike-harness/local.json` — runtime wiring
6. `tests/test_agent_loop.py` — end-to-end coverage

## Shipped in 1b

The decision gate fired here — the shape held, and Phase 1b landed
all three follow-ons:
- Provider unification — one streaming `ApiProvider` seam with
  `stream()` as the single required method (the old `ModelBackend`/
  `ToolBackend` Protocols are gone).
- `claude_cli_provider.stream()` — a real backend driving the loop.
- A realistic coding-agent example (read/edit/test on a fixture) —
  see `examples/coding-agent/`.
