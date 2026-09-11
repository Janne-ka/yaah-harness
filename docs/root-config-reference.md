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
| `baton_ttl` | **seconds** | how long a **suspended gate** survives before the sweep (time since `parked_at`); default `259200` = 72h, so a Friday gate is resumable Monday. This is a *human patience* window. (Earlier revisions of this table said "minutes" — wrong: the value is passed straight to `Baton.ttl`, which is seconds.) |
| `checkpoint_ttl` | **seconds** | how long a **Level 2 running checkpoint** stays recoverable (time since the last `checkpointed_at`). The clock restarts at every completed stage, so this really bounds *how long one stage may be in flight* before its recovery record is swept — a crash after that point is unrecoverable. **Absent → inherits `baton_ttl`**, which is the pre-split behaviour; there is deliberately **no engine default**, because a number here would silently *shorten* an existing deployment's recovery window on upgrade. Recommended explicit value: `21600` (6h) — comfortably above a slow agent stage, far below the 72h human window. |
| `lease_horizon` | **seconds** | how long a liveness lease from **another host** may go unrefreshed before its process is presumed dead and its run declared recoverable (default `3600` = 1h). Only the foreign-host tier uses it — a lease on *this* host is probed with `kill(pid, 0)`, not guessed. Must be **≤ the effective checkpoint window** (`checkpoint_ttl`, or `baton_ttl` when absent): a record swept before it can be declared stale could never be recovered at all, and `validate` rejects that combination. |
| `lease_host` | string | what this deployment calls **this host** in a lease owner id (`<host>/<pid>/<nonce>`). **Default: `socket.gethostname()`** — leave it unset on a normal machine. Set it in a **container**, where `gethostname()` is the pod/container id and is different on every restart: the same machine then looks like a new host each time, so every one of its own runs is labelled `foreign` and falls back to the coarse `lease_horizon` age guess instead of the real `kill(pid, 0)` probe. Use a stable identity that is **unique per kernel** (the k8s node name, the VM hostname); two containers on *different* kernels sharing one `lease_host` would probe each other's pid namespace and read a coincidental pid as "the owner is alive". |
| `run_dir` | path | this run's artifact root, base-relative or absolute — what the `{run_dir}` node-spec macro expands to (see `docs/node-reference.md`). Created at load if missing. **No default:** using `{run_dir}` without this key is a build error naming it, rather than a quiet fall back to the launcher's cwd. |
| `strict_resume` | bool | enforce a parked gate's declared `form` at `resume` (default **true**). A decision that violates the form is rejected (`decision_rejected`, exit 1) instead of silently taking the branch default; the gate stays parked and re-submittable. `false` restores the old blind merge — set it only for a gate whose form is genuinely mis-declared. No effect on a gate with no `form`. See ADR-0002. |
| `live_config` | bool | re-read mutable node leaves from the pipeline file per call (no restart). |
| `decisions` / `interactive` | map / bool | gate-driver answers (auto-drive) / stdin prompting. |

Unknown top-level keys, bad shapes, and bad enums are caught by `validate_root`
with a suggestion. Any `_`-prefixed key (`_about`, `_fake`) is a comment.

**`decisions` matching (auto-drive).** Each key answers a gate by its `awaiting`
tag. An **authored** convenience gate matches loosely — the whole tag, then the
parts either side of `:` — so `{"data-audit": ...}` answers a gate whose
`awaiting` is `data-audit`, `review:data-audit`, or `data-audit:v2`. A **fault**
park (the escalate lane, when a stage exhausts its attempts) is tagged
`human:<stage>` and matches an **exact key only**: `{"human:merge": ...}` answers
it, but `{"merge": ...}` does **not** — auto-approving a parked *failure* has to
be opt-in and explicit, never a suffix-match accident. A gate with no matching
decision (and no `interactive` fallback) leaves the run **parked** as a resumable
baton — the driver prints the `yaah resume` command and exits with the normal
suspended-run code, it does not crash.

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
  "router": {"type": "litellm", "stream": true},        // any litellm-routed model
  "fake":   {"type": "fake", "default": "ok"}           // offline/test: canned responses
},
"default_provider": "claude"
```
A node's `model: "claude:claude-sonnet-4-6"` is `provider:model`. `fake` and
`fake_scripted` (fixtures `by_model`) make a root runnable offline — the `--fake`
flag merges an inline `_fake` block over the top so one file covers both.
litellm's `"stream": true` (optional, default off) switches it to real SSE
chunking — incremental deltas feed the `live` monitoring heartbeat; the default
stays a single collected call (usage always attached) until chunking has live
mileage.

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
duration + the retry cause, default-on; see below), `cost` (tokens/model),
`tools`, `live` (mid-call
monitoring pulses: turn started / a throttled chars-so-far heartbeat / each tool
call / done — answers "alive or hung?" while a model call runs; sizes and names
only, never model text). `stats_file` takes a `price_map` (tokens→$).

**What `cost` projects.** `model`, `model_ref`, `tokens_out`, and the input
tokens split into the three classes that bill at different rates: `tokens_in`
(fresh, uncached input), `tokens_cache_read` and `tokens_cache_write`. Both cache
keys are ALWAYS written, zeros included — their absence is what identifies a
record written before the split (2026-08), whose lumped `tokens_in` prices as an
upper bound; `python -m yaah.trace.aggregate` counts those as
`totals.unpriced_upper_bound_calls`. Price-map rows are `{"input": usd_per_1k,
"output": usd_per_1k}`; the two cache rates derive from `input` via the
multipliers in `yaah.trace.aggregate` (`CACHE_READ_MULT` / `CACHE_WRITE_MULT`)
unless a row states an explicit `"cache_read"` / `"cache_write"` per-1k rate
(needed for e.g. 1h-TTL cache writes, which bill at 2x — the record carries no
TTL).
Cross-field checks reject silently-dropped config (e.g. `sinks` under
`mode: none`). `--explain` shows the effective trace block.

**What `phase` projects.** Always `status` + `duration_ms` (beside the
structural `id`/`corr`/`parent`/`name`/`t_start`/`t_end` every record carries),
plus these attrs when the emitting span sets them: `stage`, `awaiting`,
`artifact` (park context), `ladder_from`/`ladder_trigger` (model-ladder rung),
`resumed`/`decision_keys`/`approver`/`decision_diff` (human-override audit —
payload KEYS and an identity only, never decision VALUES),
`effects`/`effects_truncated`/`effects_head` (rollback handle),
the saga buckets `rolled_back`/`skipped_costly`/`impossible`/`failed`/
`not_attempted`/`skipped`, and the **retry cause** on a stage-error span:
`retry` (kind — `transient` | `retry` | `feedback`), `attempt` (which attempt,
against `max_attempts`), `error_retry_n` (which transient retry, against the
separate `error_retries` budget), and `error` (the failing verdict's detail).
`yaah trace --pretty` / `--errors-only` render these beside each error line, and
`yaah trace` (JSON) carries them on each `errors[]` entry. Traces written before
2026-08 spell the counter `n`; the readers accept that legacy key and report it
as `error_retry_n`, but nothing emits it any more.
`error` and `note` (advisory free text, e.g. the `json_salvaged` marker) are
the two free-text values here; `error` is **truncated at 2600 chars** with a
trailing `...[truncated]` marker (sized so a CLI provider's
bounded stderr tail arrives with its diagnosis intact): trace files are line-oriented
JSONL, and one unbounded validator message (a schema dump, a diffed payload)
would blow a single line to megabytes and make the file hostile to
`tail`/`jq`/grep. Read the full message from the stage's own artifact or the
run's failure output; the trace carries the diagnostic, not the corpus.

> **A trace file inherits the sensitivity class of its run's payloads.** Every
> other projected attr is a key, an identity, or a number — `error` is the
> documented exception: a rejecting validator routinely QUOTES the payload it
> rejected, so payload VALUES (bounded at 2600 chars, but values) land in
> `trace.jsonl`, in whatever `sinks` you configured (a third-party observability
> backend included), and — under `mode: "envelope"` — on the wire records that
> ride replies between nodes. That is deliberate: the detail is the whole
> postmortem value of the retry cause. Treat trace artifacts of a run that
> handled regulated or personal data with the same care as the data itself, and
> pick sinks accordingly.

Pipelines in which any node declares `rollback:` — or arm the auto-saga
(`graph.on_failure: "rollback"`, ADR-0009: automatic unwind of completed stages
on terminal failure; cheap-only unless `include_costly`; always re-raises; on a
non-inproc transport the trace may lag by the final records, a documented bounded
limit) — require a `{"type": "file", ...}`
entry in `sinks` (FileTraceSink) UNDER the default `mode: "tracer"` — a file sink
declared beside `mode: "none"`/`"envelope"` never persists (the tracer builder
short-circuits before sinks), so those modes are rejected too. The trace record
IS the rollback input; checked at `yaah validate` time AND at `yaah run` time;
both refuse without it.

## Plugins (extension types)

```jsonc
"plugins": ["my_ext"]          // modules imported BEFORE validation
```

Each named module is imported (config-dir on `sys.path`, like `fn:` targets) so
its `yaah.plugins.register_type(kind, name, factory, spec_keys=...)` calls run —
after which the new `type` value validates, enum-checks, and builds like a
built-in. Kinds: `provider`, `prompt_source`, `data_source`, `data_sink`,
`mcp_source`, `state`, `transport`, `trace_sink`.

**Trust note:** plugins run at import time — even `yaah validate` executes them
(the validator can't know a plugin's types without importing it). Only validate
configs whose plugins you trust; the CLI prints which plugins it imported. For
shared/long-lived use, package the extension (`plugins: ["mypkg.yaah_ext"]`)
instead of a flat file next to the config.

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
