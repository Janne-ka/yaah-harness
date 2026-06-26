# Troubleshooting

Catalog of common error messages YAAH emits, what they mean, and the next move.

The engine's error-voice rule is "every message names the bad value AND the
fix" — most of the time the message IS the answer. This page is for when it
isn't, or when the user wants to scan the catalog before hitting a problem.

For diagnosing a specific run, see also [cookbook/debugging.md](cookbook/debugging.md).

## Load-time errors (`yaah validate` / `yaah run` at startup)

### `error: <path>: invalid JSON — Expecting value: ...`

The named file isn't parseable JSON. The decoder line + column points at where
parsing failed. Common causes: a trailing comma, an unclosed bracket, a stray
character before the opening `{`.

**Fix**: open the file at the named line and check the syntax. JSON validators
(`jq -e . file.json`) give a second opinion.

### `error: invalid root config: unknown top-level key 'X' (did you mean 'Y'?); known: ...`

A typo or an outdated key. The full list of known keys follows the suggestion.

**Fix**: rename to the suggested key, or remove if the field is no longer used.

### `error: invalid root config: 'transport': typed-block is missing required key 'type' (got keys: ['kind'])`

A `transport` / `state` / `providers[*]` block uses `kind:` instead of `type:`.
This is the most common cause of "I copied an example and it doesn't work."
Engine-wide convention: every typed block discriminates on `type`.

**Fix**: rename `kind` to `type`.

### `error: invalid pipeline: stage 's': then 'X' is not a stage`

A graph `then` / `branch` / `fork` / `fanin` target names a stage that doesn't
exist. Caught by `validate_pipeline` at load. The most common case is a stage
rename where one downstream reference wasn't updated.

**Fix**: change the target to a real stage name, or add the missing stage.

### `error: node 'X' has no 'type' — if this comes from an _extends overlay, the base pipeline has no such node`

An overlay set fields on a node role the base doesn't declare — usually after
a rename in the base.

**Fix**: align the overlay's role name with the base, or remove the orphan.

### `error: a 'transform' node needs 'target' (e.g. 'fn:mod:func' or 'node:role')`

A `transform` node is missing its dispatch target.

**Fix**: add `target: "fn:my_module.transforms:func_name"` (call a Python
function) or `target: "node:role-name"` (route to another node).

### `error: an 'agent' node needs 'template' or 'prompt' in its config`

An `agent` node has neither inline `template` nor a `prompt:` ref to a
prompt-source.

**Fix**: add `"template": "Your inline prompt..."` for one-line cases, or
`"prompt": "file:my-prompt"` and put the prompt in `prompts/my-prompt.md`.

### `error: unknown node type 'X'; have ['agent', 'agent_loop', ...]`

A `type:` value not in the registry. The known set follows.

**Fix**: pick from the known list. If the type SHOULD exist (you wrote a
custom node), confirm the registry includes it.

### `error: _extends '<path>': no such packaged seed under yaah.configs`

A `_extends: "yaah:bases/X"` reference where X isn't shipped in the wheel.
Either the path is wrong or the wheel was built without package-data
(rare; `yaah doctor` would have caught this).

**Fix**: check `yaah:bases/local.base.json` / `nats.base.json` / `trace-audit.base.json`
are the only valid options today. For a packaging bug, rebuild the wheel.

### `error: _extends cycle: A -> B -> A`

Two configs reference each other.

**Fix**: break the cycle. Usually one of the two should be a leaf.

## Run-time errors

### `error: pipeline failed: stage 'X' failed: validator 'Y' rejected (code: ..., message: ...)`

A validator rejected the stage output. The included `code` + `message` come
from the validator itself.

**Fix**: read the validator output, fix the prompt or the upstream
transform that produces the input. The validator's `fix_hint` field, if
present, names the change to make.

### `error: pipeline failed: stage 'X' failed: render_unfilled_placeholders`

A `{{key}}` placeholder had no value in the payload (∪ the node's `config`
extras). Two sources:

- A `render` node. The common cause: an `agent` upstream uses `"parse":
  false` (ADR-0004 opt-out) without a `transform` between to merge the parsed
  JSON onto the payload.
- An `agent` node with `"strict_render": true` — its prompt referenced a
  `{{key}}` not reachable at that stage (the message names the key + the stage).
  This is the opt-in guard for the stage-local unfilled-placeholder case.

**Fix**: for the render case, remove the `"parse": false` to let parse-by-default
merge the JSON, OR insert a transform stage between the agent and the render. For
the agent `strict_render` case, `carry:` the key from an upstream stage, set it in
a prior transform, give it an `extras` default, or fix/remove the placeholder.
`yaah validate` catches the load-time form of the render case; the run-time form
fires if the agent's reply isn't the shape the renderer expected.

### `error: node 'X' uses 'prompt' but no prompt_source passed to build()`

An `agent` node references `prompt: "file:Y"` but the root config has no
`prompt_sources` block (or no matching key).

**Fix**: add `"prompt_sources": {"file": {"type": "file", "dir": "prompts"}}`
and put the prompt in `prompts/Y.md` (or `prompts/Y` for unsuffixed).

### `error: agent config uses {base_dir} but no base_dir was passed to build()`

A tool's `usage:` string templates `{base_dir}` but the build was driven
without a base directory. This is engine-side and shouldn't happen via the
normal CLI path.

**Fix**: file an issue — the CLI passes `base_dir` automatically; if you
see this, the wiring broke.

### `error: invalid_decision: ...`

The decision payload at a parked human gate doesn't match the gate's
declared form. `yaah baton-schema <root> <id>` shows the expected shape.

**Fix**: regenerate `decision.json` against the baton's schema, then
resume.

### NATS transport timeouts

If `transport.request_timeout` is less than a stage's runtime, the request
gets killed before the worker can reply. `validate_budgets` catches the
static case at load; dynamic latency spikes show as truncated traces or
`asyncio.TimeoutError`.

**Fix**: raise `transport.request_timeout` past your slowest stage's p95.
For LLM stages, p95 can be 10-30s+; default with margin.

## Environment errors

### `yaah doctor` reports `✗ <package> not importable (pip install 'yaah-harness[X]')`

An optional dep isn't installed. Doctor will exit 0 — optional means optional.
This isn't an error unless your pipeline actually uses that backend.

**Fix**: `pip install 'yaah-harness[X]'` where X is the extras name doctor
named (`litellm`, `nats`, `langfuse`, `http`).

### `error: [Errno 2] No such file or directory: 'claude'`

The `claude_cli` backend can't find the `claude` binary on PATH.

**Fix**: install Claude Code per its docs, confirm `which claude` resolves,
or switch the provider to `{"type": "litellm", ...}` to use the LiteLLM
backend instead.

### `yaah doctor` reports `✗ packaged base config bases/X missing`

The installed wheel doesn't ship the packaged seeds. The wheel was built
without `package-data: {"yaah.configs.bases": ["*.json"]}` in pyproject.

**Fix**: rebuild the wheel from source with the current pyproject.toml,
or `pip install --force-reinstall yaah-harness` from PyPI when ready.

## Exit codes

The CLI uses three exit codes consistently:

| Code | Meaning |
|---|---|
| 0 | Success. Pipeline completed, doctor clean, validate ok, etc. |
| 1 | The run itself failed — a stage hit `StageFailed`, doctor found hard install problems, `trace --errors-only` saw any error spans. |
| 2 | Config-class error — bad JSON, unknown key, malformed CLI args, validation rejected the config, scaffold refused to overwrite. |

Scripts can branch on these without parsing the message.

## When the message isn't enough

If you've read the error and the next move isn't clear, the playbook in
[cookbook/debugging.md](cookbook/debugging.md) walks the six commands
that surface more state (`doctor` → `validate` → `explain` → `list` →
`trace --pretty` → state-store inspection).

Failing that: the message itself is the bug. The project's error-voice
rule says every operator-facing message must answer "what's wrong" AND
"what to do next" — if it doesn't, that's worth filing.
