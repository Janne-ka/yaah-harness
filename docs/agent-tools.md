# Agent tools & MCP

How an agent node is given capabilities to call **mid-reasoning** — and why those
are *agent config*, not pipeline nodes.

## The governing distinction: who initiates the call

| | Initiated by | Lives as |
|---|---|---|
| `transform` node (`fn:`/`node:`/`http:`) | the **harness** (a pipeline step it routes to) | a node in the graph |
| **tools** | the **model** (it decides to call, mid-reasoning) | **agent config** (`tools`) |
| **mcp** | the **model** (server-provided tools/resources) | **agent config** (`mcp`) |

The harness never sees a model-initiated call — it happens inside one agent's
`invoke()`. This keeps the orchestrator thin and preserves "workers, not
citizens": the agent is still one opaque worker that takes input and returns an
output; what it does internally (call three tools, read an MCP resource) is its
own business.

There is deliberately **no `mcp:` transform scheme.** MCP is a model-tool
protocol; calling an MCP server as a standalone pipeline step is just an API call
→ use `http:`/`fn:`. MCP-as-the-model's-toolset is agent config (below).

## An agent's three capability inputs

The harness provisions an agent with three things, all at the agent-config layer:

1. **prompt** — fetched from a `PromptSource` (`file:`/`http:`/`langfuse:`). *(done)*
2. **mcp** — the MCP servers offered to the model. Resolvable like the prompt:
   inline, or a `source:key` ref fetched from a pluggable **`McpSource`**
   (`yaah.mcp`: Static / File / Routing), so endpoints + auth stay governed and
   per-environment rather than hardcoded in the pipeline file. The resolved
   servers reach claude as `--mcp-config` (+ `--strict-mcp-config`). *(done)*
3. **tools** — a **static** list declared on the agent. No dynamic tool fetch.

```json
"role:researcher": {
  "type": "agent", "prompt": "file:research", "model": "claude:claude-sonnet-4-6",
  "mcp": "registry:acme-prod",
  "allowed_tools": ["mcp__fetch__fetch", "Read"]
}
```

## Declaring tools

A tool is `{name, description, schema, impl}` on the agent node:

```json
"role:coder": {
  "type": "agent",
  "prompt": "file:code",
  "model": "openai:gpt-4o",
  "tools": [
    {"name": "lookup_account",
     "description": "Fetch an account by id",
     "schema": {"type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"]},
     "impl": "node:role:account-lookup"},
    {"name": "search_docs",
     "description": "Search internal docs",
     "schema": {"type": "object", "properties": {"q": {"type": "string"}}},
     "impl": "http://internal/search"}
  ]
}
```

- **`name` / `description` / `schema`** are what the model sees (function-calling /
  tool schema). `schema` is JSON Schema for the arguments.
- **`impl`** is *what actually runs* when the model calls the tool — a
  **transform target**: `fn:module:func`, `node:role` (a tool that **is** another
  node over Comms), or `http(s)://URL`. So a tool's implementation reuses the same
  `call_target` resolver as the `transform` node — one execution path, two
  entry points (a pipeline step vs. a model tool call).

## How it runs: the `turn` protocol + the tool-loop

`Agent` stays thin — "render prompt → backend → output". The tool logic is a
generic loop that drives any **tool-capable backend** through one method:

```
backend.turn(messages, tools, *, model, **opts) -> {"text": str}
                                                 |  {"calls": [{"id","name","args"}]}
```

- `{"text": ...}`  → the model produced a final answer; the loop returns it.
- `{"calls": ...}` → the model wants tools run; the loop executes each via
  `call_target(tool.impl, args, comms=...)`, appends the results as tool
  messages, and calls `turn` again.

```
run_tool_loop(backend, prompt, tools, comms, model):
    messages = [user: prompt]
    loop (bounded by max_iters):
        r = await backend.turn(messages, tool_schemas, model=...)
        if r has text: return r.text
        for call in r.calls:
            result = await call_target(tools[call.name].impl, call.args, comms=comms)
            messages += [assistant tool_call, tool result]
```

`Agent.invoke`: *if* `tools` are configured *and* the backend implements `turn`,
run the loop; otherwise fall back to plain `backend.complete` (so fake/scripted
backends and tool-less agents are unchanged).

## Backends differ — and that's fine (they're pluggable)

| Backend | How `tools` wire |
|---|---|
| **litellm / openai** | implements `turn` via native function-calling; the loop runs *here in YAAH*, and `impl` may be any `fn:`/`node:`/`http:` — full custom-tool support. |
| **claude CLI** | claude runs its **own** tool-loop natively, so it does **not** implement `turn`. Native tool perms are **per-agent** config: `allowed_tools: [...]` (built-ins: Read/Edit/Write/Bash/Glob/Grep) + `permission_mode` on the agent node → `--allowedTools` / `--permission-mode` (overriding any provider default — a coder gets Edit/Write, a reviewer read-only). MCP servers → `--mcp-config` (planned). A custom `fn:`/`node:` tool isn't callable natively unless bridged as MCP — on claude you lean on native tools + MCP; on litellm you get arbitrary impls. |
| **fake / scripted** | no `turn` → tools ignored (deterministic tests stay simple). A `ScriptedToolBackend` *does* implement `turn` (canned calls then a final answer) to test the loop offline, no network. |

So **tools are uniform config; execution is per-backend.** The loop, the
`call_target` resolver, and the `Tool` spec are shared; only `turn` is
backend-specific.

## Why not make every tool a node?

You can — wrap a capability in a `transform` and route to it as a stage. But a
tool is *model-initiated*: the model decides when and whether to call it, with
arguments it chooses. Forcing that through the orchestrator would mean the
orchestrator mediating every function call — chatty, and it drags the model's
reasoning into the line. The line stays coarse; the model's inner loop stays
inside the worker.
