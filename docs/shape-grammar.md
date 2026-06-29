# Shape grammar — the YAAH reference card

One page. Every root-config key, every node type, every graph
construct. Print it, tape it to a monitor, hold it in your head.

If the card and the code disagree, the code wins — re-read
[`docs/root-config-reference.md`](root-config-reference.md) /
[`docs/node-reference.md`](node-reference.md), then fix the card.
This file is the compressed essence, not the source of truth.

---

## Root config (the *.local.json / *.real.json files)

```jsonc
{
  // What runs
  "pipeline":    "<path-to-pipeline.json>",      // required (or inline {...})
  "input":       "<path-to-fixture.json>",       // task payload; absent → empty
  "run":         true,                           // run now (default: iff input present)
  "serve":       "all" | ["role:x"] | {placement: "..."},

  // How nodes talk
  "transport":   {type: "inproc" | "localbus" | "nats", ...},
  "providers":         {<name>: {type: "...", ...}},   "default_provider":      "<name>",
  "prompt_sources":    {<name>: {type: "...", ...}},   "default_prompt_source": "<name>",
  "data_sources":      {<name>: {type: "...", ...}},   "default_data_source":   "<name>",
  "data_sinks":        {<name>: {type: "...", ...}},   "default_data_sink":     "<name>",
  "mcp_sources":       {<name>: {type: "...", ...}},   "default_mcp_source":    "<name>",

  // Durability + control plane
  "state":       {type: "memory" | "file", ...},        // backs resume + idempotency
  "trace":       {mode, capture: [...], sink: {...}},   // observability
  "baton_ttl":   4320,                                   // minutes; default 72h
  "live_config": false,                                  // re-read mutable leaves per call
  "decisions":   {<gate-stage>: {auto: "approve"}},      // unattended-run answers
  "interactive": false,                                  // stdin prompts on suspend

  // Inheritance + comments
  "_extends":    "<path-to-parent>",                     // RFC 7396; null deletes
  "_<anything>": "...comment..."                         // ignored
}
```

## Pipeline (the *-pipeline.json files)

```jsonc
{
  "nodes": {
    "<node-id>": { "type": "<built-in>", ...config }
  },
  "graph": {
    "start": "<stage-name>",
    "sticky": ["<payload-key>", ...],   // optional: re-fold across stages
    "stages": {
      "<stage-name>": {
        "node":         "<node-id>"  | "",  // "" for pure control stages (fork/fanin)
        "then":         "<next-stage>" | null,
        "validators":   ["<node-id>", ...], "max_attempts": 1, "feedback": false,
        "branch":       {"on": "<payload-key>", "routes": {"<value>": "<stage>"}},
        "fork":         ["<branch-stage>", ...],
        "fanin":        {"expect": ["<branch-stage>", ...], "wait": "all" | "any",
                          "reduce": "fn:module:func"},
        "escalate":     "human" | "fail",
        "clearable":    false,
        "concerns_from":"<payload-key>",
        "on_error":     {target: "fn:...", retry: N}
      }
    }
  }
}
```

## Node types (the `type:` values for entries under `nodes:`)

| Type | Job | Required config |
|---|---|---|
| `agent`        | render prompt → call model → return raw text | `template:` OR `prompt:`; `model:` |
| `transform`    | run an `fn:` function on the envelope (the deterministic workhorse) | `target: "fn:module:func"`; `call: "envelope" \| "args"` |
| `human_gate`   | suspend; deliver to operator; resume with their decision | `form: "..."`; `ask:` (`decision_schema:` only with `form: "json_schema"`) |
| `json_object`  | validator — payload[key] parses as a JSON object with required keys | `required: [...]` (optional); `key: "raw"` |
| `json_schema`  | validator — payload[key] matches a JSON-Schema subset | `schema: {...}`; `key: "raw"` |
| `expect_field` | validator — payload[key] == value | `key:`, `equals:` |
| `render`       | fill a template against the payload → write to a file | `template_text:` OR `template_file:`; `out:` (path) |
| `get`          | read from a configured `data_source` → payload | `source: "<name>:<key>"` |
| `post`         | write payload field to a configured `data_sink` | `sink: "<name>:<key>"`; `field:` |
| `shell`        | run a command (full output → payload) | `command:` |
| `shell_check`  | run a command (pass iff exit 0; output ignored) | `command:` |
| `worktree`     | check out a fresh worktree of the source repo → payload `workdir` | `repo:` |
| `agent_loop`   | bounded tool-use loop: model emits tool calls, harness dispatches, repeat until done or `max_turns` | `tools: {name: {description, input_schema, dispatch}}`; `model:` |

Common keys on EVERY node spec: `model`, `effort`, `temperature`,
`timeout`, `retries`, `config:`, `idempotency_key:`, `idempotent:`,
`cwd_from:`, `note:`, `_<anything>:`. Unknown keys are rejected by
`validate_pipeline`.

## Agent node — the extras

| Key | Meaning |
|---|---|
| `stage` | trace + event label (defaults to stage name) |
| `carry: [<payload-key>, ...]` | keys forwarded into the reply (agents REPLACE payload otherwise) |
| `tools: [...]` | model-initiated tools (needs a turn-capable backend) |
| `allowed_tools:` + `permission_mode:` | claude-native MCP permissioning |
| `mcp:` | inline MCP server map, or `"source:key"` |
| `expose:` / `filters:` / `max_chars:` / `broker:` | R9–R12 envelope access controls |
| `attach: ["fn:module:Cls", ...]` | post-invoke wrappers ([ADR-0003](decisions/0003-attacher-port.md)) — each is a subclass of `Attacher`, returns dict merged onto reply |
| `strict_render: true` | fail the stage (`render_unfilled_placeholders`) on a `{{placeholder}}` with no value in payload ∪ extras, instead of leaving the literal `{{name}}` (default `false`); engine-injected keys + present-but-empty values never trip it |
| `output_schema: {…}` | the stage's OUTPUT CONTRACT (JSON-Schema subset). Self-validates the parsed reply (`schema_mismatch` on drift) AND its `required` keys guide weak-executor parse recovery; makes a separate `json_schema` validator node redundant on agent outputs. Opt-in (default none) |

## Trace block

```jsonc
"trace": {
  "mode":     "off" | "envelope" | "bus" | "recording",  // default: bus
  "capture":  ["cost", "tools", ...],                     // contributor names
  "sink":     {type: "console" | "progress_file" | "stats_file" | "file_jsonl", ...}
}
```

## CLI verbs

```bash
yaah init <dir>                            # scaffold a hello-yaah starter
yaah scaffold <archetype> <dir>            # scaffold a specific archetype
yaah run <root>                            # the default; `yaah <root>` also works
yaah validate <root>                       # validate_root + validate_pipeline; no run
yaah list <root> [--json]                  # mailbox view: every suspended baton
yaah resume <root> <baton-id> [<file>]     # deliver a human decision; run to next park
yaah baton-schema <root> <baton-id>        # surface the parked gate's decision form
yaah clear <root>                          # drop all suspended batons (engine reset)
yaah explain <root>                        # render the effective config + blast radius
yaah trace <jsonl> [<price-map>]           # post-hoc aggregate over a JSONL trace
  --pretty          per-run tree of stages, model calls, tool calls, errors
  --errors-only     CI-shaped check; exits non-zero if any error spans present
  --cost            compact per-model cost rollup (with PRICES for $)
  --last N          filter to the most recent N runs
yaah doctor                                # diagnose install: Python, optional deps, packaged bases
yaah completion <bash|zsh>                 # emit a shell tab-completion script
yaah --version                             # print the installed yaah version
```

All verbs also accept the legacy flag form: `yaah <root> --list`,
`yaah <root> --resume <id> <file>`, etc.

## Five pipeline shapes (everything's one of these)

See [`docs/archetypes.md`](archetypes.md). Quick:

- **`linear`** — sequence of stages, no branches, no gates. (hello-yaah)
- **`branch-with-gate`** — produce → human review → decision routes to one of N. (review-pipeline)
- **`fork-fanin`** — N parallel branches → reduce. (fork-join)
- **`instrumented`** — production-shape with attacher + optional A/B + optional gate. (arch-drift)
- **`meta-tool`** — pipeline whose input is another YAAH config. (config-flow)

## The three concepts (the spine)

- **Envelope** — one message shape; a run is one envelope flowing stage → stage.
- **Node** — `invoke(input, config) → output`. Pick a built-in; rarely write one.
- **Comms** — the harness routes; nodes never address each other.

If a new feature would add a fourth top-level concept, that's an
[ADR](decisions/) discussion, not a code change.

## Three rules that bite

1. **Agent output is a STRING in `payload["raw"]`.** Every
   `agent → render` / `agent → branch` edge needs a `transform`
   (with `call: "envelope"`) that parses + merges. Otherwise
   `render` fails with `render_unfilled_placeholders`.
2. **Transforms REPLACE the payload by default.** Use
   `return {**envelope.payload, ...new_keys}` to enrich.
3. **`_extends` is RFC 7396 merge-patch.** `null` DELETES an
   inherited key. Use to override a typed-block whose parent had
   a different shape.

## IDE autocomplete + error highlighting

YAAH derives JSON Schemas for both root and pipeline files from the
engine's own validation tables. Wire them in your editor and you get
autocomplete on every key, enum, and node type — and red squiggles on
typos before the runtime ever loads.

**Scaffolded configs get this for free.** `yaah init <dir>` (and
`yaah scaffold <archetype> <dir>`) writes a `$schema` pointer into each
config and **generates the matching schema** into `<dir>/schemas/` —
generated from the installed engine, so the autocomplete always matches
the engine that will run your config (no shipped-snapshot version skew).
Nothing to wire; just open the dir in your editor.

**Hand-written configs** add `$schema` at the top themselves:

```jsonc
// in a root config:
{
  "$schema": "schemas/root.schema.json",
  ...
}

// in a pipeline config:
{
  "$schema": "schemas/pipeline.schema.json",
  ...
}
```

…and need a generated schema for the path to resolve against. The
schemas are NOT shipped as package data in the wheel (they're derived
artifacts, not source); get one of these ways:

- **Easiest:** `yaah init` a throwaway dir and copy its `schemas/`
  next to your config.
- **Source checkout:** point the path at `schemas/root.schema.json` in
  the yaah repo, or run `python3 scripts/build_schemas.py` to (re)write
  them there.

The schema is **autocomplete, not the correctness gate** — `yaah
validate` + `lint_pipeline` remain that (a clean schema does not mean a
sound pipeline; it does not see the data-flow contract). The generator
lives in `src/yaah/schema_gen.py` (used by both `scripts/build_schemas.py`
and `yaah init`); a suite test asserts the committed `schemas/` match it,
so drift is caught at suite time, not when a user complains the
autocomplete lies.

## Where to look next

- Authoring → [`docs/archetypes.md`](archetypes.md)
- Why the shape is what it is → [`docs/decisions/`](decisions/)
- Every key in detail → [`docs/root-config-reference.md`](root-config-reference.md)
- Every node in detail → [`docs/node-reference.md`](node-reference.md)
- Copy-paste recipes → [`docs/cookbook/`](cookbook/)
- Generated catalog (always current) → [`docs/module-catalog.md`](module-catalog.md)
- IDE-ready JSON Schemas → [`schemas/`](../schemas/)
