# Node-type reference

Every built-in pipeline `type:` — what it does, the config keys it reads, the
output shape it produces, and a minimal example. **Ground truth is the code**:
`src/yaah/build/builders.py` (which keys each builder reads, and the error you
get when a required one is missing) and the node sources in `src/yaah/nodes/`.
The auto-generated [module-catalog.md](module-catalog.md) is the full
machine-readable index — one-liner surface for every node type, port, adapter,
contributor, and key terminology; this file is the sit-down version for
pipeline authors.
For how a stage *uses* a node (validators / retry / branch / fork), see
[architecture.md](architecture.md) §4–5.

## Keys every node spec understands

| Key | Goes to | Meaning |
|---|---|---|
| `model`, `effort`, `temperature`, `timeout`, `retries` | `NodeConfig` | per-call scalars; the node reads them at invoke time. With root `live_config: true` these (plus numeric `config` values) refresh from the file per call — the mutable-leaf surface (`validate.MUTABLE_LEAF_KEYS`). |
| `config` | `NodeConfig.extras` | node-specific settings; agents also resolve prompt `{{placeholders}}` from here (payload wins). |
| `idempotency_key`, `idempotent: true` | `OnceNode` wrapper | run the node's side effect ONCE per correlation even across retries/replays (needs root `state:`). |
| `provides`, `consumes` | the data-flow lint | the payload keys this node writes / reads, when the engine cannot infer them (a `transform`'s return, a custom type). Author-time only — never read at runtime. |
| `placement` | the `serve` selector | which worker serves this node, e.g. `"gpu"`; `serve: {placement: [...]}` in the root picks the subset. |
| `rollback` | any node type | declares the node's undo capability: `{target, cost}`. See **Rollback** below. |
| `note`, any `_*` key | nobody | config comments. |

Unknown keys are caught by `validate_pipeline` (the silent-no-op class). The
authoritative list is `src/yaah/node_keys.py:COMMON_NODE_KEYS` plus that type's own
row in `BUILTIN_NODE_KEYS`.

**`cwd_from` is NOT in this table** — it is a PER-TYPE key, read by `agent`,
`shell`, `shell_check`, `get` and `post` only. On any other type (`render`,
`transform`, `worktree`, a validator) it is a hard `validate_pipeline` error, not a
silently-ignored hint. It names the payload key holding the per-run worktree path
(usually `"workdir"`), and those nodes then run there; see each type's section
below.

### Path macros (`{base_dir}`, `{run_dir}`)

Two **single-brace** macros are expanded at BUILD time in every string leaf of a
node spec — command elements, `cwd`, `out`, `template_file`, `source`, `sink`,
agent tool `usage`/`allowed_tools`, `config` extras, at any nesting depth:

| Macro | Expands to | Root key |
|---|---|---|
| `{base_dir}` | the directory the root config was loaded from (absolute) | — (implicit) |
| `{run_dir}` | this run's artifact root (absolute) | `run_dir` |

The point is that a config file stays **relocatable** — a repo-relative path a
colleague can check out anywhere — while the runtime gets an **absolute** path,
which it must, because a repo-bound agent runs with cwd in the task worktree and a
shell node's `cwd` is wherever the author set it.

```jsonc
"role:report": {
  "type": "render",
  "template_file": "{base_dir}/templates/report.html",
  "out": "{run_dir}/report.html"
}
```

Both work on **every node type**, and reach `NodeConfig.extras` too — a
`config: {"repo_root": "{run_dir}/repo"}` arrives at the node already resolved.
(`{base_dir}` used to be agent-only, because its expansion lived inside the agent
builder — not because the need was agent-specific. It is now one implementation in
`yaah.build.macros`, applied in `build._built_nodes` — upstream of the builder AND
of the `NodeConfig` extraction, so both see the same expanded spec. `Registry.build`
expands as well, for an app calling it directly; expansion consumes its tokens, so
stacking the two is a no-op.)

**Compat edge of that widening.** A pre-existing literal `{base_dir}` in a
NON-agent string used to survive to runtime untouched; it now expands — or, if the
config was loaded without a base dir, raises a build error naming the macro. The
same applies to `{run_dir}` with no root `run_dir` key. There is no such string
anywhere in this tree, and a path-shaped `{base_dir}` was almost certainly meant to
expand; but if you were relying on the literal, escape it out of a single-brace
token (or move the value into a `{{key}}` the envelope fills at run time).

**One exception, by design: root `live_config: true`.** The per-invocation re-read
folds in only the `NodeConfig` scalars and NUMERIC `config` values, so it can never
re-introduce an unexpanded macro; every string extra keeps its build-time expanded
value. Editing a string `config` value in the live file therefore does not take
effect — that is the frozen-at-build surface, not a macro limitation.

**`{run_dir}` has no default.** Using it without the root `run_dir` key is a build
ERROR naming the key, never a quiet fall back to the launcher's cwd — a run whose
artifacts land in whatever tree the launcher happened to be in is the silent
misroute this refuses to ship. The directory is created at load if missing.

**Not the same as `{{key}}` interpolation.** These macros are single-brace and
resolve at build; `{{db_url}}` is double-brace and is filled per invocation from the
envelope/`config`. One string may carry both — `"psql {{db_url}} -o {run_dir}/d.sql"`
expands the macro now and leaves the placeholder for the run.

**`timeout` semantics (per-node, unchanged) + the provider stall watchdog.** A
node's `timeout` still overrides the backend default exactly as before — set it
per stage for a slow node, leave it off to inherit the backend's default. What
changed is the *default* when neither the node nor the consumer sets one:

- **`claude` backend** — `timeout` is a per-`readline` **inactivity** watchdog,
  NOT a total-run deadline. It bounds the silence between output lines: the CLI
  emits nothing between a `tool_use` and its `tool_result`, so one long tool call
  is one long silence. The default is now **900s (15 min)** of silence — finite
  so a network cut / wedged CLI / MCP stall surfaces as a clean error event (and
  the engine's retry/park path) instead of freezing the run forever. A node whose
  single tool call can legitimately run longer than 15 min (an agent executing a
  large test suite in one shell call) MUST set an explicit larger node `timeout`
  — otherwise it is false-killed at 900s (recoverable: the error is classified
  transient and retried, but each retry restarts the agent).
- **`litellm` backend** — `timeout` is the SDK request timeout (bounds the whole
  request for single-shot; for streaming it is forwarded to the client and bounds
  inter-chunk reads on httpx-based providers). Default is now **900s**; an
  explicit `null` in the provider spec opts out to litellm's own default.

Overrides: an explicit **node** `timeout` (a number) wins over the backend
default. A node-level `timeout: null` is NOT an opt-out — null and absent are
indistinguishable in node config, both inherit the backend default. The
wait-forever opt-out for the `claude` backend exists ONLY at the provider spec
level (`providers: {... "timeout": null}` → `ClaudeCliProvider(timeout=None)`),
deliberate and greppable in the run-root.

---

## `agent` — the LLM worker

Renders a prompt (inline `template` or `prompt: "source:key"` fetched from the
prompt source), calls the model backend, returns the raw text. Placeholders:
`{{key}}` resolves payload-first then `config`; `{{!key}}` marks the value
UNTRUSTED and fences it (unguessable per-render token); bare payload values get
fence-mimicking sequences neutralized (the instruction-channel defense).
`{{?key}}` marks the placeholder OPTIONAL — a key legitimately absent on early
passes (e.g. a cross-loop `loop_feedback` note a `tally` transform writes only
from the second iteration): an absent `{{?key}}` renders EMPTY instead of leaving
a literal, and under `strict_render` it does NOT fault. This lets a feedback-loop
agent set `strict_render: true` and still fault on a genuinely-missing REQUIRED
key. The markers compose as `{{?!key}}` (optional AND untrusted).

**Placeholder key names are single flat payload keys — dot-paths are not
supported.** `{{item.text}}` does NOT match the regex (`\w+` only); it passes
through as the literal string `{{item.text}}` in the rendered prompt, silently.
The `?`/`!` sigils (above) are an agent-prompt-only dialect; `render` and
`human_gate` templates use the plain templater, which recognises `\w+` only and
ignores sigils entirely (`{{!key}}` is a literal there).
If an upstream `foreach` stage delivers per-item dicts under `results`, add a
`transform` step to flatten the fields you need into top-level scalar keys before
passing them to an agent or render node.

Config: `template` *or* `prompt` (required), `model`, `stage` (trace/event
label), `cwd_from`, `carry` (payload keys forwarded into the reply — agents
REPLACE the payload otherwise; prefer graph `sticky` for run-wide keys),
`parse` (bool, default `true` — [ADR-0004](decisions/0004-parse-by-default.md):
agent runs `extract_json` on its output and merges the parsed keys onto the
reply; opt out with `parse: false` for streaming/raw-only cases),
`strict_render` (bool, default `false` — when `true`, a `{{placeholder}}` with no
value in payload ∪ `config` extras FAILS the stage loud with
`render_unfilled_placeholders` naming the key + stage, instead of leaving the
literal `{{name}}` in the prompt; engine-injected keys like `tool_manifest` and
present-but-empty values never trip it, and a placeholder the author marks
optional with `{{?key}}` renders empty rather than faulting — so an agent reading
a loop-seeded key like `{{?loop_feedback}}` can run strict and still fault on a
genuinely-missing required key. Catches the stage-local unfilled-placeholder
class no static lint can),
`output_schema` (optional JSON-Schema subset — the stage's OUTPUT CONTRACT,
parse-path only: the agent self-validates its parsed reply against it
(type/enum/required/properties/items) and fails loud with `schema_mismatch`
when it drifts, so you DON'T need a separate `json_schema` validator node; its
`required` keys + the typed `properties` also drive recovery of a weak executor's
not-quite-JSON on a parse failure — unquoted/bare values, enum members,
`type:string` free-form, and one-`key: value`-per-line output — see
[json-recovery.md](json-recovery.md) for the exact contract and how to declare a
stage so haiku output Just Works. Top level must be an object — a parse-mode
agent rejects a non-object reply as `not_object` before the schema runs. Omit
it for no contract — byte-identical to before),
`escalate_model` (optional model string, parse-path only — the one-rung MODEL
LADDER: when the parsed reply carries a truthy top-level `help` key (the
blocked-agent convention: a model that CANNOT do its job replies
`{"help": "<blocker>", ...}` instead of hallucinating), the agent re-calls the
SAME rendered prompt once with this stronger model and returns THAT reply,
whatever it is — a repeated `help` flows out to the normal concern/gate path so
an infra blockage reaches the human instead of burning a third model. Trigger
is `help` only; parse failures keep the stage's retry+feedback path. The
escalation is visible as a second `model_call` span whose record carries
`ladder_from`/`ladder_trigger` (via the default `phase` capture), each span
reporting ITS OWN call's tokens. Three facts to know before wiring it on:
a help reply must still PASS `output_schema` to trigger — declare `help` as an
additive optional string, never required; COST multiplies with retries — each
stage attempt may ladder once, so `max_attempts: 3` is up to 6 model calls
worst-case; and with `tools` a MIXED-capability ladder degrades silently (the
tool manifest is rendered for the primary model's capability — keep both rungs
tool-capable or the agent tool-free). Requires the parse path —
`parse: false` + `escalate_model` is rejected at validation),
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
  This `args_from` is **ONE string** naming the payload key that holds the whole
  args object. It is *not* the shell family's `interpolate_from` (a **list** of
  key names substituted into argv) — different type, arity and meaning; that is
  exactly why the shell key is not called `args_from`.
- `call: "envelope"` (fn: only): `fn(envelope, config)`; the returned dict
  REPLACES the payload entirely — the config-aware deterministic step. The fn
  must copy any prior keys it wants to keep:
  `return {**envelope.payload, "new_key": value}`. A returned Envelope passes
  through unchanged. The hello-yaah and review-pipeline examples are single-key
  pipelines (prior keys don't matter); arch-drift (multi-stage) copies explicitly
  at every transform.

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

**`json.loads` vs `extract_json` in transform functions.** Use
`yaah.jsonio.extract_json` whenever the string to parse came from a real LLM —
most models (all except opus reliably) wrap JSON in markdown fences or prose
that `json.loads` rejects. Use plain `json.loads` only for trusted
machine-generated JSON (e.g. a file your pipeline wrote, a tool's structured
output). Rule of thumb: if it touched an LLM, use `extract_json`.

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
`tail_only` (drop full stdout, keep the tail), `tail` (tail size in chars,
default 2000), `carry`, `target_from` / `interpolate_from` (below).
**Never fails the stage** — output: `{exit_code, ok, stdout?, stdout_tail,
...carry}`; route on `ok`/`exit_code` with `branch`, or gate with `shell_check`.
The stage trace span records `exit_code` (the error-path contract).

```json
"role:green-run": {"type": "shell", "command": "bundle exec rspec spec/unit",
                   "cwd_from": "workdir", "timeout": 600, "tail_only": true}
```

### `target_from` — append ONE payload value as a command argument

The command is **trusted config and never comes from the payload** (anti-injection).
`target_from` is the one controlled exception: it names a single payload key whose
value(s) are **appended to the command as additional argument(s)** — never
replacing it, never interpreted as a command. The motivating case (M17): a test
gate must run against the path the coding agent *actually wrote its test at*, not
a path pre-guessed in config.

- Value may be a **string** (one appended arg) or a **list of strings** (each
  appended, in order). A shell-string command gets each value `shlex.quote`d; an
  argv list gets each value appended as its own element.
- **Opt-in per value presence**: `target_from` unset → old behaviour; the key
  absent or `null` in the payload → the command runs **unchanged**.
- **Strict path-shape validation** (fail-loud): each value must be a non-empty
  string of only `[A-Za-z0-9._/-]` (no whitespace or shell metacharacters), must
  not start with `-` (no option injection), must not contain `..` (no traversal),
  and is length-capped. **Any** invalid value **ERRORS the node**, naming the key
  and offending value — the node never silently runs the command without the
  target (running the wrong/no target is the exact bug this fixes). A non-string
  `target_from` is rejected at **build time**.

```json
"role:green-run": {"type": "shell", "command": ["bundle", "exec", "rspec"],
                   "cwd_from": "workdir", "target_from": "test_path"}
```

### `interpolate_from` — substitute payload values INTO the command's arguments

`target_from` only ever appends. When the per-run value belongs in the **middle**
of the argv — a connection string, a tenant id, a branch name — declare
`interpolate_from`: a **list of payload key names** that the command's `{{key}}`
placeholders may draw from.

> **Why this name, not `args_from`?** The `transform` node already has an
> `args_from`, and it is a *different thing wearing the same words*: ONE string
> naming the payload key that holds the whole args object, versus a LIST of key
> names substituted into argv — different type, different arity, different
> semantics. `interpolate_from` names the mechanism and sits cleanly beside
> `target_from` and `cwd_from`.

- **argv-LIST commands for anything to substitute.** A **string** command
  carrying a `{{`-token plus `interpolate_from` is a **build error**. This is the
  safety argument, and it holds *by construction*: a list command is either
  exec'd directly (no shell exists) or, under `shell: true`, has every element
  `shlex.quote`d before being joined. Either way a substituted value is exactly
  **one argv token** and can never become command structure — a value containing
  `; rm -rf /` or `$(id)` reaches the child as inert literal text. A string
  command goes to the shell as *source*, where substitution would be raw
  concatenation; that edge is closed by refusing it.
- **A token-free string command is legal.** Substitution needs a `{{`-token; with
  none present it cannot occur, so a declared `interpolate_from` over a plain
  shell-string command **builds and runs unchanged** — interpolation is simply
  inert. That is the normal shape when a host overlay supplies its runner as a
  shell string while the pipeline declares the key centrally.
- **Whole-element or embedded**: both `"{{db_url}}"` and `"--db-url={{db_url}}"`
  work — substitution is per element, anywhere inside it.
- **Declared keys only.** A `{{key}}` the node's `interpolate_from` does not list
  is a **build error**, never a silently-passed-through literal. So is a
  `{{`-token that is not a well-formed `{{key}}` (the `{{?key}}`/`{{!key}}`
  agent-prompt sigils are not this templater's dialect).
- **Unfilled = node error.** A declared key absent from the payload, `null`, a
  non-scalar, an empty string, or a value carrying a C0 control other than tab
  (NUL, newline, ESC, ...) or past the 4096-char cap → the node **ERRORS**. A gate
  never runs with a literal `{{key}}` in its argv. (Controls beyond NUL/newline
  are rejected because these values ride back out through `stdout_tail` into ANSI
  operator terminals and rendered reports.)
- **Opt-in, and inert when unset.** `interpolate_from` absent → behaviour is
  byte-identical to before: a `{{key}}` in a command stays the literal `{{key}}`.
- **Order with `target_from`**: interpolation happens first (placeholders keep
  their position), then targets are appended — targets always land last.
- **Checked at LOAD.** A shell node declares the payload keys it reads — the
  `{{key}}`s actually present in its argv (only when `interpolate_from` opts in),
  plus `target_from`'s key — so the data-flow lint catches "nothing upstream
  produces this key" at load instead of a mid-run `TargetError`. Declaring a key
  in `interpolate_from` that the command does not use is *not* a read: the list is
  the allow-list, the argv is the read-set (so a pipeline may declare the key
  centrally while a host overlay decides whether its command carries the token).
  **The check is SKIPPED when the payload reaching this stage is unknown** — the
  common case being a `transform` upstream with no `provides:`, whose return value
  the engine cannot see. The lint has no set to check against, so it stays silent
  rather than guessing; that transform is reported separately as
  `transform-provides-undeclared`, and declaring `provides:` on it is what turns
  this check back on downstream.

```json
"role:migrate": {"type": "shell", "cwd_from": "workdir",
                 "command": ["./bin/migrate", "--url={{db_url}}", "--tenant", "{{tenant}}"],
                 "interpolate_from": ["db_url", "tenant"]}
```

## `shell_check` — a command as a VALIDATOR

Same execution as `shell` (incl. `cwd_from`, `target_from` and
`interpolate_from`), but
returns a pass/fail **Verdict** for a stage's `validators` list. `expect_exit`
(default 0) or `expect_nonzero: true` (the RED gate: tests must FAIL before code
exists). Failure detail carries the output tail (`tail`, default 2000 chars) into
the retry feedback.

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
these validators when validating a `transform` output. For an `agent` that
needs the SCHEMA shape, prefer the agent's own `output_schema` (above) — it
runs the SAME checker (`yaah.jsonschema.check_schema`) on the agent's reply,
so a standalone `json_schema` node on an agent output is redundant; keep
`json_schema` for non-agent (`transform`) outputs that have no contract of
their own.

## `human_gate` — park for a decision

`ask` (template, `{{key}}` filled from the payload — what the mailbox shows;
rendered via the plain templater, so the value is inserted **unframed** — the
`{{!key}}` fencing of the `agent` node does NOT apply here, see the lint note
below), `awaiting` (tag, default `"human"`), `form` (optional — names a generic
decision shape; one of `approve` / `approve_or_revise` / `free_text` /
`json_schema`), `decision_schema` (required iff `form: "json_schema"`; inline
JSON Schema for the one-off escape hatch — forbidden with the built-in forms),
`allow_untrusted` (lint-only opt-out, see the `untrusted-unfenced` note below).
Returns an AWAIT envelope; the harness parks the baton (artifact + the gate's
rendered question; the gate's keys win a collision) until `resume()` merges the
decision payload back (decision keys win). Route the decision with
`branch: {on: "decision", ...}` — a gate with only `then` is a pause, not a
gate. When `form` is declared, `yaah baton-schema <root> <baton_id>` surfaces
the matching JSON Schema so a driver skill composes `decision.json`
mechanically; see [decision-forms.md](decision-forms.md) for the catalog and
the extension story.

When a gate declares a `form` with a `decision` enum (e.g. `approve`,
`approve_or_revise`) and routes on it, every `branch` route key must be a value
the enum admits: a route keyed on a decision the form can never produce (e.g.
routing `reject` off `form: "approve"`) is **dead under resume enforcement** —
the harness rejects a non-conforming decision (`decision_rejected`), so that
branch can never fire. `yaah validate` catches it: an **ERROR**
(`gate-route-not-in-form`) under the default `strict_resume: true`, downgraded to
a **warning** under `strict_resume: false` (where the lenient blind-merge makes
the forbidden decision reachable again). Fix by using a form whose decisions
include the route key — `approve_or_revise`, or a `json_schema` form — or drop
the dead route.

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
Like `human_gate`, `render` fills `{{key}}` with the plain templater — the value
is inserted **unframed**, and `{{!key}}` is a literal (no fencing). See the lint
note below.

Two opt-out flags (both default `false`):

- `allow_unfilled` — by default an unfilled `{{placeholder}}` FAILS the stage
  (`render_unfilled_placeholders`), the loud form of the worst fault class (a
  forgotten parse step shipping a literal `{{name}}` at exit 0). Set it `true`
  when a field is intentionally optional.
- `allow_untrusted` — a render cannot fence (`{{!key}}` is a literal here), so
  the `untrusted-unfenced` lint has no in-place remedy on a render. Set it `true`
  to assert this render's output feeds a **human/file**, not a model prompt, so
  unfenced agent-authored text is legitimate — it silences ONLY this render's own
  `untrusted-unfenced` warnings. No runtime effect; a lint-only opt-out parallel
  to `allow_unfilled`. The **same flag works on a `human_gate`** (its `ask`
  likewise can't fence): there it means "the human decision-maker is the firewall
  — agent text is meant to reach the reviewer as-is." Per-node in both cases —
  it silences only the node that sets it, never agent-prompt fencing.

## Lint: `untrusted-unfenced` — agent-authored text at an unframed consumer

An **advisory** lint (`yaah validate`; fails only under `--strict`), explicitly
**NOT an injection-safety proof**. Only the `agent` node fences (`{{!key}}`);
`human_gate` `ask` and `render` templates render via the plain templater, which
never frames a value and treats `{{!key}}` as a literal. So agent-authored text
(a model's `summary`, `question`, `decision`, or its raw output) interpolated
`{{key}}`-unfenced into a gate question or a rendered document reaches the
consumer — a human, an **AI operator** driving the gate, or a downstream
document — as-is.

The rule flags a `human_gate`/`render` site that reads `{{key}}` where an `agent`
stage on some path to it **authors** `key` (its `output_schema` keys, an inline
`provides`, or `raw`). Provenance is graph reachability, so it sees through an
opaque parse-`transform` (the common `agent → parse → gate` shape). It is
deliberately **quiet** where provenance is unknowable — a key only an undeclared
envelope-`transform` could have invented, an engine key (`exit_code`), a
`human_gate`'s own human-typed `decision`, or an entry/`carry` key — because the
honest move is to flag agent-authored text, not to guess. A `{{!key}}` marker is
quiet.

Remediation is **not** `{{!key}}` at the consumer (a no-op literal there):
sanitize the value in an upstream `transform`, or confirm the consumer cannot act
on injected instructions. Both unframed consumers also have an in-place
**acknowledgment** opt-out — `allow_untrusted: true` on the node — for when the
exposure is intended: a `render` whose output feeds a human/file, or a
`human_gate` whose human reviewer is the firewall. It silences only that node's
sites. Known blind spot: a `transform` that RENAMES agent text
(e.g. folds a judge's `reason` into `refix_reason`) breaks the provenance chain,
so a renamed key is not attributed — declare intent with an inline `provides` on
the producing agent, or fence at the agent-prompt boundary. Because renames can
dominate a real config's exposed surface, any run of the rule that produces hits
also emits one consolidated caveat: the findings are a **floor, not a clean
bill**.

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

## Rollback — declared undo capability (node key)

Any node type can declare a `rollback` block naming the function or URL to call
when an operator triggers `yaah rollback --execute` on a completed run:

```json
"push_amendment": {
  "type": "post", "sink": "api:amendments",
  "rollback": {"target": "fn:undo:delete_amendment", "cost": "cheap"}
}
```

- `target` (required): `fn:module:func` or an `http(s)://` URL. **`node:` targets
  are rejected at validate time.** The rollback tool runs outside a running harness
  — it reads files, not Comms — so a `node:` target would fail at `--execute` rather
  than at authoring time. Validate catches it so the author knows before it runs.
- `cost` (optional, default `"cheap"`): `"cheap"` | `"costly"`. A hint from the
  author, not a computed truth. `costly` candidates are skipped unless
  `--include-costly` is passed to `yaah rollback --execute`. The menu also shows
  which stages ran AFTER each candidate so the operator can judge ordering risk
  independently of the label.
- **Absent `rollback`** → the stage is listed under `impossible` in the rollback
  report: never guessed, never silently skipped, always reported honestly.

The `rollback` block is shape-checked at validate time: unknown sub-keys are
rejected, `target` must be a non-empty `fn:` or `http:` string, `cost` must be
`"cheap"` or `"costly"`. A top-level node-key typo (`rollbck:`) is a silent no-op
— a pre-existing engine gap, noted in ADR-0008 but not fixed there.

**Compensate-context divergence (ADR-0008 D3).** A `compensate` function (the
`on_error: {compensate: ...}` at-failure undo) receives the FAILING stage's full
`payload`. A rollback target receives the bounded effect descriptor recorded at
COMPLETION: `{correlation_id, stage, node, effects, cost}`. These are deliberately
different contracts. A compensate function is NOT drop-in reusable as a rollback
target — reusing one silently reads absent `payload` keys. Choose deliberately.

### `effects_from` — stage key (alongside `concerns_from`)

`effects_from: "<payload-key>"` declares which payload key holds the **effect
descriptor**: the small, bounded handle the undo target needs (an amendment id,
a file path, an API-returned record id, etc.). On stage COMPLETION the harness
copies `payload[<key>]` onto the stage's trace span as attr `effects`. That
value is what `--execute` hands to the rollback target as `ctx["effects"]`.

```json
"write_alpha": {"node": "write_alpha", "effects_from": "effect", "then": "write_beta"}
```

Restrictions and bounds:
- **Rejected on `fork` and `fanin` stages.** The fork parent's completion span is
  emitted on a no-output path; the copy would silently record nothing — validate
  rejects it loudly instead. Linear, branch-child, and `foreach` stages record fine.
- **2048-char bound.** If the JSON-serialized descriptor exceeds 2048 chars it is
  NOT stored as a mid-string clip (mid-string JSON is unparseable garbage). Instead:
  `effects: null, effects_truncated: true, effects_head: "<first 256 chars, plain
  string>"`. The rollback menu shows a loud descriptor-dropped warning; the undo
  target receives `effects: None` and must handle it (e.g. by keying off
  `correlation_id` alone).

Lint (WARNING): a node declaring `rollback` whose stage(s) have no `effects_from`
means the undo target will receive `effects: None`. This is legal (some undos key
off `correlation_id` alone) but the author should confirm this intentionally rather
than by omission.

### `yaah rollback` verb

```
yaah rollback <root> [<corr-id>] [--json]
yaah rollback <root> <corr-id> --execute [--include-costly] [--only <stage>]... [--accept-partial]
```

**No corr-id:** lists all runs found in the trace file that have rollback candidates.

**With corr-id, default (menu / dry run, calls nothing):** resolves candidates from the
trace in **reverse file-append order** — JSONL line index, never by span `t_start`.
Process-local monotonic times break across a resume (the primary rollback scenario):
a run resumed in a fresh process emits post-resume spans on a new zero-point, so
sorting by `t_start` would interleave or invert undo order. File-append position is
chronological by construction.

Candidate selection: a span must be status `ok` AND not a point-span (`t_start ==
t_end`) AND not carrying the `resumed` attr. Resume/retry bookkeeping spans carry
`status ok` too; filtering them prevents a gate's resume note from masquerading as
a rollback candidate.

Each candidate shows: **stage name, node id, declared cost, the recorded `effects`,
and the stages that ran after it** (the dependency-visibility the operator needs to
judge ordering risk without the engine guessing domain semantics). A stage whose
node declares no `rollback` is listed under `impossible`.

A stage that ran TWICE (a branch-backward loop) yields TWO candidates with their own
descriptors, keyed by file-append position and shown with occurrence numbers.
`--only <stage>` addresses ALL occurrences of that name.

**`--execute`:** calls each candidate's `target` in reverse order with
`ctx = {correlation_id, stage, node, effects, cost}`.
- `costly` candidates are SKIPPED unless `--include-costly`.
- Stops on the first FAILED undo unless `--accept-partial` — half-unwound state is
  never silent; continuing past a failure is an explicit, flag-consented choice.
- `--only <stage>` (repeatable): restrict to the named stage(s).

**Report shape** (`--json` for machine-readable):
```json
{"run": "<corr-id>",
 "rolled_back":    [...],
 "skipped_costly": [...],
 "impossible":     [...],
 "failed":         [...],
 "not_attempted":  [...]}
```
`impossible` is a first-class outcome, not an error — a rollback that honestly names
what can't be undone is a success of the function.

### Non-goals (v1, ADR-0008 D4)

- **No automatic saga on failure.** Auto-unwind on a terminal failure is dangerous
  (a transient blip triggering semi-irreversible undos). A human is already in the
  loop at failure.
- **No dependency-aware ordering.** Reverse completion order + the what-ran-after
  view; the engine does not model domain dependencies.
- **No rollback-of-rollback tracking.** Undo targets should be idempotent; running
  the verb twice re-calls them.
- **No transactional guarantee.** Stop-on-first-failure + explicit `--accept-partial`
  is the full consistency story.

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
