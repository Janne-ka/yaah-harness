# Node-type reference

Every built-in pipeline `type:` — what it does, the config keys it reads, the
output shape it produces, and a minimal example. **Ground truth is the code**:
`src/yaah/build/builders.py` (which keys each builder reads, and the error you
get when a required one is missing) and the node sources in `src/yaah/nodes/`.
The auto-generated [module-catalog.md](module-catalog.md) lists the same
surface as one-liners; this file is the sit-down version for pipeline authors.
For how a stage *uses* a node (validators / retry / branch / fork), see
[architecture.md](architecture.md) §4–5.

## Keys every node spec understands

| Key | Goes to | Meaning |
|---|---|---|
| `model`, `effort`, `temperature`, `timeout`, `retries` | `NodeConfig` | per-call scalars; the node reads them at invoke time. With root `live_config: true` these (plus numeric `config` values) refresh from the file per call — the mutable-leaf surface (`validate.MUTABLE_LEAF_KEYS`). |
| `config` | `NodeConfig.extras` | node-specific settings; agents also resolve prompt `{{placeholders}}` from here (payload wins). |
| `idempotency_key`, `idempotent: true` | `OnceNode` wrapper | run the node's side effect ONCE per correlation even across retries/replays (needs root `state:`). |
| `cwd_from` | repo-bound nodes | payload key holding the per-run worktree path (usually `"workdir"`); shell/agent/get run there. |
| `note`, any `_*` key | nobody | config comments. |

Unknown keys are caught by `validate_pipeline` (the silent-no-op class).
`{base_dir}` inside agent tool `usage`/`allowed_tools` strings expands to the
config file's directory (absolute), so tool scripts ship beside the config.

---

## `agent` — the LLM worker

Renders a prompt (inline `template` or `prompt: "source:key"` fetched from the
prompt source), calls the model backend, returns the raw text. Placeholders:
`{{key}}` resolves payload-first then `config`; `{{!key}}` marks the value
UNTRUSTED and fences it (unguessable per-render token); bare payload values get
fence-mimicking sequences neutralized (the instruction-channel defense).

Config: `template` *or* `prompt` (required), `model`, `stage` (trace/event
label), `cwd_from`, `carry` (payload keys forwarded into the reply — agents
REPLACE the payload otherwise; prefer graph `sticky` for run-wide keys),
`parse` (bool, default `true` — [ADR-0004](decisions/0004-parse-by-default.md):
agent runs `extract_json` on its output and merges the parsed keys onto the
reply; opt out with `parse: false` for streaming/raw-only cases),
`tools` (model-initiated, needs a turn-capable backend), `allowed_tools` +
`permission_mode` (claude-native), `mcp` (inline servers or `"source:key"`),
`expose`/`filters`/`max_chars`/`broker` (R9–R12 envelope access), `attach`
(opt-in list of `fn:module:func` references to `Attacher` subclasses; the
agent gets wrapped in `AttachingAgent` and each attacher merges post-invoke
data — e.g. tokens/usage — onto the output payload from the tracer's last
span; see [ADR-0003](decisions/0003-attacher-port.md)).

Output (`parse=true`, default): `{raw: <model text>, ...parsed JSON keys,
...carry keys, ...cwd carry, ...attacher keys}` — payload REPLACED. On
parse failure / non-object JSON the agent emits a failed Verdict envelope
the retry+feedback loop catches.

Output (`parse=false`, opt-out): `{raw: <model text>, ...carry keys, ...cwd
carry, ...attacher keys}` — `raw` only; downstream stages need an explicit
`transform` with `call: "envelope"` to merge the model's structured output
(load-time graph linter enforces this).

```json
"role:review": {"type": "agent", "prompt": "file:review",
                "model": "claude:claude-sonnet-4-6", "stage": "review",
                "carry": ["diff", "task"], "config": {"lens": "correctness"}}
```

## `transform` — call a function/node/URL

`target` (required): `fn:module:func` (local Python — **code-equivalent, never
payload-derived**), `node:role` (another node over Comms), or `http(s)://url`
(POST JSON). Two call shapes:
- `call: "args"` (default): `fn(args)` where args = payload (or `args_from`
  key); result lands under `into` (default `"result"`) — enrich, don't replace.
- `call: "envelope"` (fn: only): `fn(envelope, config)`; the returned dict
  SPREADS over the payload top-level — the config-aware deterministic step.
  **Gotcha:** "spread" here means "the returned dict IS the new payload" —
  multi-stage pipelines must explicitly carry prior keys forward, e.g.
  `return {**envelope.payload, "new_key": value}`. The hello-yaah and
  review-pipeline examples don't trip on this because each is a one-key
  pipeline; arch-drift (multi-stage) does it explicitly at every transform.

```json
"role:flatten": {"type": "transform", "call": "envelope",
                 "target": "fn:app.transforms:merge_findings"}
```

**Where `fn:` resolves from.** `fn:module:func` targets are imported relative
to your config file's directory — keep your `transforms.py` / tool module next
to your config and it just resolves. For shared or production code, install it
as a package (`pip install -e .`) and use a dotted path, e.g.
`fn:mypkg.transforms:func`. The convenience is the on-ramp; packaging is the
durable path.

## `get` — read through the data port

`source` (required): a `"source:key"` ref into the data-source layer
(`git:` diff, `file:path`, http...). Optional `into` (default `"data"`),
`cwd_from`, `context` (diff context lines), `paths`. Output: payload +
`{into: fetched}` — enriching.

```json
"role:get-diff": {"type": "get", "source": "git:", "into": "diff",
                  "cwd_from": "workdir", "context": 3}
```

## `post` — write through the data sink

`sink` (required): `"sink:key"` ref (e.g. `file:out/report.json`). Optional
`field` (payload key to store, default `"data"`), `into` (result marker key,
default `"stored"`), `cwd_from`. Output: payload + `{into: <where it went>}`.

## `shell` — run a command, report what happened

`command` (required; string or argv list). Optional `cwd`, `cwd_from`,
`timeout`, `shell: true` (string runs under a shell; list elements are quoted),
`tail_only` (drop full stdout, keep the tail), `carry`. **Never fails the
stage** — output: `{exit_code, ok, stdout?, stdout_tail, ...carry}`; route on
`ok`/`exit_code` with `branch`, or gate with `shell_check`. The stage trace
span records `exit_code` (the error-path contract).

```json
"role:green-run": {"type": "shell", "command": "bundle exec rspec spec/unit",
                   "cwd_from": "workdir", "timeout": 600, "tail_only": true}
```

## `shell_check` — a command as a VALIDATOR

Same execution as `shell`, but returns a pass/fail **Verdict** for a stage's
`validators` list. `expect_exit` (default 0) or `expect_nonzero: true` (the RED
gate: tests must FAIL before code exists). Failure detail carries the output
tail into the retry feedback.

## `expect_field` — payload assertion validator

`key` + `equals` (both required). Verdict: pass iff `payload[key] == equals`.
The cheapest hard gate (`placement_ok`, `scope_ok`, ...).

## `json_object` / `json_schema` — model-output validators

Parse `payload[key]` (default `"raw"`, fence/prose-tolerant via
`yaah.jsonio.extract_json`). `json_object`: optional `required` key list.
`json_schema`: `schema` (required, JSON-Schema subset).

**Usually unnecessary on `agent` outputs**: per [ADR-0004](decisions/0004-parse-by-default.md)
agents are parse-by-default (they run `extract_json` themselves and emit a
failed verdict on bad JSON, triggering the retry+feedback loop). Reach for
these validators when validating a `transform` output, OR when you need
the SCHEMA shape (`json_schema.schema` does structural checking the
agent's plain `extract_json` doesn't).

## `human_gate` — park for a decision

`ask` (template, `{{key}}` filled from the payload — what the mailbox shows),
`awaiting` (tag, default `"human"`), `form` (optional — names a generic
decision shape; one of `approve` / `approve_or_revise` / `free_text` /
`json_schema`), `decision_schema` (required iff `form: "json_schema"`; inline
JSON Schema for the one-off escape hatch — forbidden with the built-in forms).
Returns an AWAIT envelope; the harness parks the baton (artifact + the gate's
rendered question; the gate's keys win a collision) until `resume()` merges the
decision payload back (decision keys win). Route the decision with
`branch: {on: "decision", ...}` — a gate with only `then` is a pause, not a
gate. When `form` is declared, `yaah baton-schema <root> <baton_id>` surfaces
the matching JSON Schema so a driver skill composes `decision.json`
mechanically; see [decision-forms.md](decision-forms.md) for the catalog and
the extension story.

## `worktree` — git worktree isolation

`repo` (required; base_dir-relative ok), `base` (default `"HEAD"`), `root`
(where worktrees live), `branch_prefix` (default `"yaah/"`), `op`:
`"add"` (default) or `"remove"`, `task_key` (payload key naming the run,
default `"task"`; sanitized), `force`, `carry`, `timeout`.
Output (`add`): `{workdir, branch, repo, base, ...carry}` — downstream
repo-bound nodes point `cwd_from` at `workdir`. Output (`remove`):
`{removed, ok}`.

## `render` — fill a template to a file

`template_text` or `template_file` (base_dir-relative ok), `out` (output path).
For the heavier factory documents the app uses `transform` +
`call: "envelope"` into a Python renderer instead (`render_report.py` etc.).

## `agent_loop` — bounded, model-driven tool-use loop

A loop where the **model** drives: it emits tool calls, the harness dispatches
them, feeds the results back, and repeats until the model stops or `max_turns`
is hit. The model-driven counterpart to the author-static `fork`/`fanin`/
`transform` — reach for it when the number and order of tool calls is the
model's decision, not the pipeline's.

`tools` (required) — a non-empty dict `{name: {description, input_schema,
dispatch}}`; each tool's `dispatch` is an `fn:` / `node:` / `http:` target (a
`node:` tool needs `comms`). `max_turns` (default `10`), `system_prompt` (a
literal string or a `file:` reference resolved via the prompt source), `model`
(optional override). Needs a backend with `.stream()` (preferred) or `.turn()`
— a `complete()`-only backend can't drive it; use a plain `agent` for one-shot
stages. Tool specs are validated at BUILD time (a missing `dispatch` fails the
load, not turn N). Reads the task from payload `goal` (or `input`).
Output: `answer` (the final assistant text), `turns` (the count), and
`outcome` ∈ {`completed`, `empty_response`, `max_turns_exhausted`}.

---

## Choosing between them

- Deterministic logic → `transform` (never an agent).
- Read/write the world → `get`/`post` through the port (swappable adapter),
  not a hand-rolled shell command.
- A command whose *exit code is the verdict* → `shell_check` in `validators`;
  a command whose *output is data* → `shell` + `branch`.
- Model output you must trust structurally → `json_object`/`json_schema`
  validator on the agent's stage, `max_attempts` ≥ 2, `feedback: true`.
- Anything a human must own → `human_gate` + a `branch` on `decision`.
