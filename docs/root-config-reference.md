# Root / deployment config reference

The **root config** is *what we spin up here* — transport, model providers,
prompt/data/mcp sources, the state store, tracing, which pipeline to load, which
roles this host serves, and the input. It is separate from the **pipeline
config** (*what the stages are* — see [node-reference.md](node-reference.md) and
[architecture.md](architecture.md) §3). The same pipeline runs in-process,
local-over-NATS, or as a cloud node by changing only this file.

**Ground truth** is `src/yaah/validate.py` (`validate_root` + `_ROOT_KEYS` and
the shape tables) and the factory maps in `src/yaah/runtime_factories.py` (each
`{type: (factory, allowed-keys)}` entry IS the per-type schema — the validator
reads enums and keys from there, never a hand-copied list). Run `yaah <root>
--explain` to print the effective config with `(user)`/`(extends)`/`(default)`
provenance; a malformed root fails fast with a did-you-mean.

**Don't author from scratch** — `_extends` a packaged seed and override:
```json
{"_extends": "yaah:bases/local.base.json",
 "providers": {"claude": {"type": "claude_cli"}},
 "default_provider": "claude",
 "pipeline": "my-pipeline.json", "input": "fixtures/input.json"}
```
`yaah:bases/{local,nats,trace-audit}.base.json` resolve from the package
(install-safe). See [node-reference.md](node-reference.md) for `_extends`
merge semantics (RFC 7396: objects merge, lists replace, `null` deletes).

**The `null`-deletes pattern bites** the first time you extend a fake-overlay
config with a real-provider override. Example: `arch-drift.local.json` has
`providers.claude = {type: "fake_scripted", by_model: {…}}`; the real config
says `providers.claude = {type: "claude_cli", binary: "claude"}` thinking it
overrides. It doesn't — deep merge keeps `by_model` from the base, and
`ClaudeCliProvider` rejects the extra key. **Explicitly null it:**
```json
"providers": {"claude": {"type": "claude_cli", "binary": "claude",
                          "by_model": null}}
```
RFC 7396 says child `null` deletes the key from the merged result. Apply
whenever a child config swaps a typed-block's `type` and the base had
type-specific fields the new type doesn't accept.

## Top-level keys

| Key | Shape | Meaning |
|---|---|---|
| `transport` | typed block | how nodes talk — `inproc` (default) / `localbus` / `nats`. |
| `providers` + `default_provider` | named map + name | model backends; `provider:model` resolves a node's `model`. |
| `prompt_sources` + `default_prompt_source` | named map + name | where `prompt: "source:key"` fetches from. |
| `data_sources` + `default_data_source` | named map + name | `get` node sources. |
| `data_sinks` + `default_data_sink` | named map + name | `post` node sinks. |
| `mcp_sources` + `default_mcp_source` | named map + name | agent MCP config resolution. |
| `state` | typed block | durable store — `memory` (default) / `file`. Backs baton resume + idempotency. |
| `trace` | block (keyed on `mode`) | observability — see Tracing below. |
| `pipeline` | path | the pipeline JSON to load (relative to the root file). |
| `input` | path or inline object | the task payload; absent → empty payload. |
| `serve` | `"all"` / list / `{placement}` | which roles THIS host runs (distributed). |
| `run` | bool | run the pipeline now, or stay a serve-only worker (default: run iff `input` present). |
| `baton_ttl` | minutes | how long a parked gate survives before the sweep (default 4320 = 72h, so a Friday gate is resumable Monday). |
| `live_config` | bool | re-read mutable node leaves from the pipeline file per call (no restart). |
| `decisions` / `interactive` | map / bool | gate-driver answers (auto-drive) / stdin prompting. |

Unknown top-level keys, bad shapes, and bad enums are caught by `validate_root`
with a suggestion. Any `_`-prefixed key (`_about`, `_fake`) is a comment.

## Transport

```json
"transport": {"type": "inproc"}                         // one process (default)
"transport": {"type": "localbus"}                       // in-proc bus (offline multi-node proof)
"transport": {"type": "nats", "url": "nats://127.0.0.1:4222",
              "request_timeout": 300.0,                 // LLM nodes blow past the 30s NATS default
              "user": "...", "password": "...",         // OR "token", OR "creds": "path.creds"
              "tls": {"ca": "ca.pem", "cert": "c.pem", "key": "k.pem", "hostname": "..."}}
```
`request_timeout` is the reply window; `validate_budgets` rejects any node
`timeout` that exceeds it (the work would outlive the wait — BUG-635 class).
TLS cert paths resolve relative to the root file.

## Providers (model backends)

```json
"providers": {
  "claude": {"type": "claude_cli"},                     // claude -p (+ extra_args, allow_dangerous_flags)
  "router": {"type": "litellm"},                        // any litellm-routed model
  "fake":   {"type": "fake", "default": "ok"}           // offline/test: canned responses
},
"default_provider": "claude"
```
A node's `model: "claude:claude-sonnet-4-6"` is `provider:model`. `fake` and
`fake_scripted` (fixtures `by_model`) make a root runnable offline — the `--fake`
flag merges an inline `_fake` block over the top so one file covers both.

## Prompt / data / mcp sources

```json
"prompt_sources": {"file": {"type": "file", "dir": "prompts", "ext": ".md"}},
"default_prompt_source": "file",                         // also: http (base_url), langfuse, static
"data_sources": {"git": {"type": "git_diff", "context": 3},
                 "fs":  {"type": "file", "dir": "."}},
"data_sinks":   {"out": {"type": "file", "dir": "work_tmp"}},
"mcp_sources":  {"reg": {"type": "file", "dir": ".mcp"}}  // also: static (inline configs)
```
Each is a named map; a node references one by `source:key`. Add a source type =
one factory-map entry, no dispatch code (the hug-the-world port pattern).

## State (durable resume)

```json
"state": {"type": "memory"}                              // default — parked gates die with the process
"state": {"type": "file", "dir": ".yaah-state"}          // durable — resume across processes/restart
```
One store backs both the baton (resume cursor) and idempotency (`once`). A file
store is what lets `--list`/`--resume` work cross-process and survive a crash.

## Tracing

```json
"trace": {"mode": "tracer",                              // none | tracer (default) | envelope
          "capture": ["phase", "cost", "tools"],         // composable contributors (default ["phase"])
          "sinks": [{"type": "console"},                 // file | console | langfuse | progress_file | stats_file
                    {"type": "file", "path": "trace.jsonl"}]}
```
`capture` is an orthogonal SET, not a verbosity level — `phase` (stage/status/
duration, default-on), `cost` (tokens/model), `tools`. `stats_file` takes a
`price_map` (tokens→$). Cross-field checks reject silently-dropped config (e.g.
`sinks` under `mode: none`). `--explain` shows the effective trace block.

## Distribution (`serve`) and running

```json
"serve": "all"                                           // this host runs every role (default)
"serve": ["role:code", "role:review"]                    // an explicit subset
"serve": {"placement": "cloud"}                          // roles tagged placement:cloud in the pipeline
```
A serve-only worker (`run` omitted/false, no `input`) stays alive handling its
subject. The orchestrator process runs with `input` set. Per-user NATS subject
permissions scope a worker to its own roles (blast-radius limit) — proven in
`test_nats_distributed_auth.py`.

## Worked examples

**Local, offline, one process** (the QUICK START):
```json
{"_extends": "yaah:bases/local.base.json",
 "providers": {"fake": {"type": "fake", "default": "ok"}},
 "default_provider": "fake",
 "pipeline": "my-pipeline.json", "input": {"task": "DEMO-1"}, "run": true}
```

**Local-over-NATS, a worker + an orchestrator** (two roots, shared state dir):
```json
// worker.json — serves the heavy roles, stays alive
{"_extends": "yaah:bases/nats.base.json",
 "providers": {"claude": {"type": "claude_cli"}}, "default_provider": "claude",
 "pipeline": "pipeline.json", "serve": ["role:code"], "run": false}

// orchestrator.json — drives the run over the same NATS + file store
{"_extends": "yaah:bases/nats.base.json",
 "providers": {"claude": {"type": "claude_cli"}}, "default_provider": "claude",
 "pipeline": "pipeline.json", "input": "input.json", "run": true}
```

**Secured remote** — add auth + TLS to the transport (the rest unchanged):
```json
"transport": {"type": "nats", "url": "tls://nats.example:4222",
              "creds": "worker.creds",
              "tls": {"ca": "ca.pem", "hostname": "nats.example"}}
```

## CLI

```
yaah <root.json>                 run (or serve-only if no input/run)
yaah <root.json> --explain       effective config + provenance, no run
yaah <root.json> --fake          merge the inline _fake block (offline)
yaah <root.json> --list          parked gates (needs durable state:)
yaah <root.json> --resume <id> [decision.json]
yaah <root.json> --clear         drop parked state
yaah <overlay.json> --lint-overlay   gate an AI-authored overlay (deny-by-default)
```
