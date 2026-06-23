# coding-agent — a worked example (Phase 1b/B4)

The first realistic worked example: an agent fixes a one-character bug
in a Python file. Demonstrates BOTH of YAAH's tool-use patterns end-to-end.

## The fixture

`fixtures/buggy_code/is_fizzbuzz.py` has a bug:
```python
def is_fizzbuzz(n: int) -> bool:
    return n % 3 == 0 or n % 5 == 0    # bug: 'or' should be 'and'
```

`fixtures/buggy_code/test_is_fizzbuzz.py` is a runnable test that fails
against the buggy version and passes after the fix.

## YAAH's two tool-use patterns (and which example variant uses which)

YAAH supports two ways to give a model tools, with different node types
and different backend requirements:

| pattern | node type | backend needs | who owns the inside loop |
|---|---|---|---|
| YAAH-driven | `agent_loop` + `tools` dict | `turn(messages, tools)` (model-side function calling) | **YAAH** dispatches each tool call via `call_target`; observes every turn |
| Model-driven | `agent` (no YAAH tools) | `complete(prompt)` | **the model** runs its own loop natively; YAAH just calls and waits |

The honest tradeoff:

- **YAAH-driven** gives the harness full visibility — per-turn trace
  spans, mid-loop intervention, custom `fn:` / `node:` / `http:` tool
  dispatch. Required when you want to compose YAAH-native tools
  (`envelope_get`, `context_broker`, MCP via `node:`) or run advisory
  watchers between turns. Works with backends that support real
  function-calling: LiteLLM (real model APIs) and the offline fake/
  scripted-tool backends.

- **Model-driven** is the right fit for `claude_cli`. Claude has its own
  tool execution (Read/Edit/Bash/Write) — trying to drive that from
  outside fights the grain of the CLI (opus eval, 2026-06-23 — see git
  log around B4 for the architectural analysis). The harness still
  earns its keep at the pipeline LAYER: gates, retries, fan-in,
  observability across multiple stages, prompt rendering. Claude just
  runs its inside loop.

  **Honest scope of THIS isolated example:** `pipeline_native.json` is
  a single agent node with no gates, no fan-in, no retries, no
  composition. In this configuration YAAH is doing very little the
  bare CLI couldn't (`claude -p "$(cat prompts/coding_agent_native.md)"
  --allowedTools Read,Edit,Bash` reaches the same end). The YAAH value
  surfaces when this stage is composed with others — a scout stage
  before, a verify gate after, observability across the whole flow.
  This example shows the harness CAN host claude_cli as an agent stage,
  not that the harness adds value here.

## How to run

### Offline (fake_tool, YAAH-driven, no model, no network, no cost)

Uses `pipeline.json` (agent_loop) + `local.json` (fake_tool with
scripted-fix turns). This is what `tests/test_coding_agent_example.py`
exercises in CI.

```bash
cd examples/coding-agent
# the local.json uses {{WORK}} placeholder — point it at the fixture
sed "s|{{WORK}}|$PWD/fixtures/buggy_code|g" local.json > /tmp/local.resolved.json
PYTHONPATH=$PWD:../../src python3 -m yaah.runtime /tmp/local.resolved.json
# verify the fix
cd fixtures/buggy_code && python3 test_is_fizzbuzz.py
# reset for the next run
cd ../.. && git checkout fixtures/buggy_code/
```

The fake_tool provider in `local.json` is scripted to emit the tool
calls a real agent would emit for this bug. The TOOLS are real (they
read, edit, and run shell commands against the fixture), only the model
decisions are canned — so the run is deterministic and free, and the
end-state IS the fixed file.

### Real claude (claude_cli, model-driven)

Uses `pipeline_native.json` (single `agent` node, no YAAH tools) +
`claude.json` (claude_cli with `allowed_tools: ["Read", "Edit", "Bash"]`).
Claude uses its native tools to fix the file.

**Requires:** the `claude` CLI installed and authenticated.
**Costs:** roughly a few cents per opus run (haiku is cheaper).

```bash
cd examples/coding-agent
PYTHONPATH=../../src python3 -m yaah.runtime claude.json
# verify the fix
cd fixtures/buggy_code && python3 test_is_fizzbuzz.py
# reset
cd ../.. && git checkout fixtures/buggy_code/
```

What you should see:
- Claude reads the file, identifies the bug
- Claude edits the file using its native Edit tool
- Claude runs the test using its native Bash tool
- Claude reports "DONE: ..."

Claude's tool calls are CLAUDE's, not YAAH's. The YAAH side is observing
the pipeline + reporting cost; the tool dispatch happens inside claude.

### Real LLM via LiteLLM (YAAH-driven, switchable)

LiteLLM supports OpenAI, Anthropic, Gemini, Bedrock, etc. through one
API. To switch the YAAH-driven variant to a real LLM, copy `local.json`
to `litellm.json` and replace the provider section:

```json
{
  "providers": {
    "openai": {
      "type": "litellm",
      "default_opts": {"temperature": 0}
    }
  },
  "default_provider": "openai",
  ...
}
```

Then set the relevant API key in the environment (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, etc.) and run `python3 -m yaah.runtime litellm.json`.

The current `LiteLLMBackend` collapses to a single non-streaming
`acompletion()` call internally — sufficient for this example's "model
decides which tool to call" pattern, but real chunk-by-chunk streaming
is a future upgrade (see `.notes/breaking-changes.md`, B2.5 entry).

## What this example proves about the harness

- **The agent_loop primitive works end-to-end against scripted backends.**
  pipeline.json + local.json: harness owns the loop, dispatches each
  tool call via `call_target`, observes every turn.
- **`Tool` errors flow back as observations.** Try editing `local.json`
  to break the edit_file args (e.g. pass an `old_string` that doesn't
  exist) — the next turn sees the error, can correct.
- **claude_cli works as a real-model PoC of the harness control flow.**
  The B3 stream-json parser carries the model's text + usage; the
  pipeline orchestrates around it.
- **Both patterns share the same harness substrate.** Same prompt
  source, same state, same trace, same gate mechanics. Only the inside
  of the agent-stage changes.

## When to reach for which pattern

- Use **YAAH-driven (`agent_loop`)** when: you want fine-grained tool
  control, mid-loop intervention, YAAH-native tool composition,
  per-turn tracing, A/B comparison of tool catalogs.
- Use **model-driven (`agent` + native tools)** when: the model is
  claude_cli (its native tools are excellent + the CLI fights external
  dispatch), or when "let the model figure it out" is the right level
  of abstraction.

For most YAAH applications the answer is "both, at different stages."
A scout stage might use claude_cli (model-driven, exploratory); the
fix stage might use LiteLLM with agent_loop (YAAH-driven, controlled).

## Honest limits

- The fixture is small. Real bug-fixing across multiple files needs a
  bigger fixture, higher `max_turns`, better prompts. Out of scope for
  "PoC of mechanics."
- The `run_bash` tool (YAAH-side) is unsandboxed. In production,
  scope it down via `--allowedTools` (claude_cli) or wrap in a
  worktree-isolated shell. The example's `run_bash` does enforce a
  60-second timeout but offers no other sandbox. Claude's native
  Bash already gates via `--permission-mode`.
- The fake-tool variant scripts the exact fix flow. If you change the
  bug, the script doesn't adapt — only a real model would.
- Claude as a YAAH-driven `agent_loop` backend would require an MCP
  shim (YAAH-as-MCP-server pattern). Not in this example; see the
  opus eval verdict in `.notes/eval-agent-priming.md` or git log around
  2026-06-23 for the architecture analysis.

## Where to look in the engine

- [`src/yaah/nodes/agent_loop_node.py`](../../src/yaah/nodes/agent_loop_node.py)
  — the YAAH-driven loop node
- [`src/yaah/agents/agent.py`](../../src/yaah/agents/agent.py)
  — the model-driven single-shot `agent` node
- [`src/yaah/agents/tool_loop.py`](../../src/yaah/agents/tool_loop.py)
  — the canonical tool-use loop (B8 unification)
- [`src/yaah/adapters/backends/claude_cli_backend.py`](../../src/yaah/adapters/backends/claude_cli_backend.py)
  — claude provider with B3 stream-json parsing
- [`docs/architecture/agent-loop/`](../../docs/architecture/agent-loop/)
  — the design + use cases
