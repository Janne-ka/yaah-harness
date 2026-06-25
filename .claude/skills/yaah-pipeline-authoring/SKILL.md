---
name: yaah-pipeline-authoring
description: Use when the user wants a new or modified YAAH pipeline config — a `*-pipeline.json` (nodes + graph) and/or a `*.local.json` root. Not for editing engine code (use yaah-extending).
---

# Authoring YAAH pipelines

**Standing rule:** never commit unless explicitly asked.

## Step 0 — pick an archetype (read this BEFORE asking the user anything)

Open [`docs/archetypes.md`](../../../docs/archetypes.md). Match the user's request against the **"Reach for this when…"** lines of the five archetypes (`linear`, `branch-with-gate`, `fork-fanin`, `instrumented`, `meta-tool`). The match dictates the reference example to copy from. **Don't design from first principles** — the archetype map exists precisely so you don't have to, and the named examples are battle-tested.

If no archetype obviously fits, re-read once. Most "doesn't fit" cases are the user describing a pipeline that's doing too much; split into two simpler pipelines that each match an archetype, then ask the user which one to build first.

## Overview

A YAAH pipeline is two JSONs: a **pipeline** (`nodes` + `graph` of stages) and a **root** (`providers`, `transport`, `state`, `pipeline:`, `input:`, `run:`). Most authoring failures come from inventing concepts that existing primitives compose. **Pick the archetype (Step 0), drive a short Q&A** on the variation points the archetype calls out, propose the smallest config that fits, then ship a canonical `.json` + a thin `_extends`-based `.fake.json` overlay so it's testable without LLM cost.

## When to Use

- User asks: "build / write / scaffold a YAAH pipeline", "I want a pipeline that does X"
- They have a workflow shape in mind but not the JSON
- Not for: editing engine code (`yaah-extending`); an app-specific pipeline variant when your project ships its own authoring skill

## Knowledge base — read this when answering "which X?"

`docs/module-catalog.md` is the **single source of truth** for node types, ports, adapters, validators, trace contributors/sinks, and model-initiated tools — auto-generated from `src/yaah/` by `scripts/build_catalog.py`. When the user asks "which node type for X?", "what args does FileDataSource take?", "which trace sink?" — look it up in the catalog, don't guess. If the catalog is stale, regenerate: `python3 scripts/build_catalog.py` (drift-free by construction — the code is the truth).

**Root-config schema (R15):** `src/yaah/validate.py` is the single source of truth for what a root config may contain — `_ROOT_KEYS` (allowed top-level keys), `_TYPED_BLOCK_KEYS` / `_NAMED_MAP_KEYS` / `_STRING_KEYS` / `_BOOL_KEYS` (shape per key), `_TRACE_MODES` / `_TRANSPORT_TYPES` (enum values). Before handing the dev a root config, run it through `yaah.validate.validate_root(root)` mentally: any unknown key, bad shape, or bad enum surfaces with did-you-mean. This is the contract the runtime gates every load on, and it is what an LLM-generated root must pass cleanly.

**Seed base configs (R14-seed):** the seeds ship INSIDE the package (`src/yaah/configs/bases/`, carried in the wheel) so they're available whether yaah is a source sibling or a `pip install`:
- `local.base.json` — inproc transport, memory state, console trace. Pick for **dev iteration / fixture-driven / CI smoke / QUICK START**.
- `nats.base.json` — NATS transport (local), file state, file-sink trace. Pick for **local-over-NATS multi-worker / resume-after-crash**.
- `trace-audit.base.json` — heavy-capture trace OVERLAY (`phase + cost + tools`, file sink). Layer onto a deployment base via a 2-level `_extends` chain for **regulatory audit / post-incident replay**.

**Use a seed via `_extends`:** prefer the install-safe `yaah:` scheme — it resolves the packaged seed via importlib.resources, so the SAME ref works from a source checkout AND an installed/extracted yaah (no `../yaah/...` relative path that dies on install):
```json
// PREFERRED — packaged reference (works installed or from source, no local copy needed):
{"_extends": "yaah:bases/local.base.json", ...}

// Alternative — vendored seed (copied into your app, version-pinned):
{"_extends": "bases/local.base.json", ...}      // ./bases/local.base.json next to the root
```
A full minimal root looks like:
```json
{"_extends": "yaah:bases/local.base.json",
 "providers": {"claude": {"type": "claude_cli"}},
 "default_provider": "claude",
 "pipeline": "my-pipeline.json", "input": "fixtures/input.json"}
```
The skill's procedure is **pick a seed → diff to intent → emit overlay → validate → repair → explain**, not author every key by hand.

## Turn 1 — offer to scaffold first

Most devs want to copy a working thing first, customize second. After Step 0 named the archetype, **offer to run `yaah scaffold <archetype> <dir>`** — the runtime CLI now writes a working starter from any of three archetypes (`linear`, `branch-with-gate`, `fork-fanin`; `instrumented` and `meta-tool` templates are queued but not yet shipped — for those, copy from `examples/arch-drift/` and `examples/config-flow/` respectively). Only fall into Q&A if the scaffold doesn't cover the user's intent.

> "I'll scaffold the `<archetype>` starter: `yaah scaffold <archetype> <dir>` writes a runnable shape you can adapt. Want that, or shall I drive a Q&A for a custom shape first?"

If the user accepts, run the command (or have them run it), then **drive the Q&A only on the variation points** the archetype calls out in `docs/archetypes.md` (e.g. for `branch-with-gate`: gate stage name, decision form, branch routes; for `fork-fanin`: number and names of the lenses, reduce strategy). Don't re-ask for things the scaffold already wrote.

If the archetype is `instrumented` or `meta-tool` and no template ships yet, fall through to the Q&A and copy from the reference example listed in `docs/archetypes.md`.

Agents are parse-by-default ([ADR-0004](../../../docs/decisions/0004-parse-by-default.md)). The model's JSON output is auto-merged onto the payload — `render` and `branch` find the keys they need without an intermediate `transform`. The data-flow footgun is gone for the common case; it fires only when the user opts out via `"parse": false`, and `validate.py` catches THAT at load time.

## The Q&A (in order — **skip what the user already specified**)

The user has often given the shape ("a spec→code pipeline that…"). Don't re-ask what they answered; only ask the open holes. Propose defaults out loud ("I'll use `inproc` unless you need NATS") rather than asking permission for every choice.

1. **Archetype?** (decided in Step 0, but re-confirm if drift): `linear` / `branch-with-gate` / `fork-fanin` / `instrumented` / `meta-tool`. Picking the archetype dictates the reference example. See [`docs/archetypes.md`](../../../docs/archetypes.md) for the "Reach for this when" lines.
2. **Stages?** Name each. *Suggest:* lower-kebab, ≤3 words. One role per stage.
3. **Per stage: which node type?** Pick from `module-catalog.md`'s **Node types** table (12 today: `agent`, `transform`, `human_gate`, `shell`, `shell_check`, `expect_field`, `json_object`, `json_schema`, `worktree`, `get`, `post`, `render`). *Suggest:* `agent` for thinking; `transform` (with `call: "envelope"`) for deterministic Python; `human_gate` for decisions; `json_object` validator after every agent.
4. **Providers?** *Suggest:* `claude_cli` for real, `fake_scripted` for the fake overlay. Always build both. Other backends in `adapters/backends/` (catalog).
5. **Transport?** *Suggest:* `inproc` (default → seed **`local.base.json`**). `nats` if distributed across machines or workers (→ seed **`nats.base.json`**). `localbus` rarely. Picking a transport = picking a seed; the seed prefills compatible state/trace too.
6. **State?** *Suggest:* `memory` if you picked the `local` seed (default). `file` if you picked the `nats` seed (prefilled). Override only if you want cross-host KV or NATS JetStream — out of seed scope.
7. **Validators per stage?** Severity `hard` (blocks) or `soft` (concerns + continues)? *Suggest:* `hard` json_object on every agent output; `max_attempts:3, feedback:true` for refix loops. If the agent needs to forward payload keys across attempts (dialogue, refix), add `carry: ["key1", "key2"]`.
8. **Human gates?** Where; `ask:` text; **which decision form?** ([ADR-0002](../../../docs/decisions/0002-decision-forms.md) catalogs four generic shapes: `approve`, `approve_or_revise`, `free_text`, `json_schema` escape hatch). *Suggest:* `approve_or_revise` for review/edit loops, `approve` for go/no-go gates, `free_text` for open-ended editing, `json_schema` only when the four don't fit. Set `form: "..."` so `yaah baton-schema <id>` can surface the decision shape to a driver skill (otherwise `baton-schema` exits with "no form declared"). If the gate is hard, `branch` on `decision` (don't leave only `then`). For unattended/CI runs, set `decisions: {<gate>: {auto: "approve"}}` in the root.
9. **Asymmetric arm with a slim view?** *Suggest:* configure the agent's `expose:`/`max_chars:` (the model fetches allow-listed envelope fields on demand — R9 `envelope_get`), `broker:` (the model asks a cheap broker node for relevant slices — R12 `context_broker`), and `filters:` (named `Filter` adapters that the model invokes by name with allowed params — R10; see the Filter adapters table in the catalog). **Security knob — read the guardrails table below before answering.**
10. **Tracing?** *Suggest:* console is **default-on** (the harness adds a `ConsoleTraceSink` when no `trace` block is given). Add `{"type": "file", "path": "..."}` for persistent JSONL, `progress_file`/`stats_file` for live waterfall, `{"type": "langfuse"}` for managed dashboards. `trace: {sink: []}` to opt out entirely. See the Trace-sink adapters table in the catalog.
11. **Attachers?** ([ADR-0003](../../../docs/decisions/0003-attacher-port.md)). For `instrumented` archetype pipelines, an agent's `attach: ["fn:transforms:UsageAttacher"]` surfaces per-call cost/tokens into the payload — the load-bearing piece for "this run cost $X" answers. The engine ships ZERO built-in attachers; copy [`docs/cookbook/attachers/usage.py`](../../../docs/cookbook/attachers/usage.py) into the consumer's transforms file (NOT importable). Attachers also require `trace.capture: ["cost"]` at the root — the builder rejects at LOAD if the capture is missing.

## Security guardrails — the IaC `0.0.0.0/0` analogues

The `expose:` / `filters:` / `reasoning` knobs are security-sensitive; don't default-on without intent.

| Knob | Safe default | Failure mode |
|---|---|---|
| `expose.payload` | only fields the model needs THIS stage | over-broad allow-list leaks data into the prompt |
| `expose.header` | empty | leaking `baton`, auth tokens, or `correlation_id` lets the model spoof system state |
| `max_chars` | ≤20000 (the hard cap) | unbounded pull blows the context window |
| `filters` | only the named filters the author has vetted | model-supplied params are bounded by the adapter, but a sloppy `CallTargetFilter` target is RCE-equivalent |
| `trace.capture: reasoning` | OFF; turn on ONLY for compliance/regulation runs, with restricted sink | reasoning may contain sensitive deliberation; treat like PII |

If the user asks for any of these without context, ASK what they're trying to expose and why; don't just write `expose: {payload: ["*"]}`.

**Canonical constraint text** lives in `docs/module-catalog.md` § *Security-relevant constraints (from `Args:` docstrings)* — auto-extracted from the source. The table above is a quick reference; the catalog is the truth.

Then **draft, show, ask "what to adjust"**, write.

## Quick reference

Concrete shapes to adapt — smallest-viable linear, fork (asymmetric A/B), fanout barrier, the root-config template, and the three fake-mode shapes — live in [`pipeline-reference.md`](pipeline-reference.md). Read it when you need a template to copy; the judgment for *which* shape is Step 0 (archetype) + the Q&A above.

**Two extra reference assets the user / you can also load:**

- [`docs/shape-grammar.md`](../../../docs/shape-grammar.md) — one-page card listing every root key, every node type, every graph construct, every CLI verb. The card that fits in working memory.
- [`docs/cookbook/`](../../../docs/cookbook/) — non-importable reference recipes for consumer code (currently: `attachers/usage.py`, the canonical `UsageAttacher`). Copy these into the consumer's `transforms.py` rather than importing — per ADR-0003.

Two rules that travel with every template: the parse stage between an agent and any render/branch is **not optional**, and `fn:` targets in config are **trusted code** — never point one at anything payload-derived.

`fn:module:func` resolves relative to the config's directory — keep `transforms.py` next to the config and it just resolves; for shared/production code, package it (`pip install -e .`) and use a dotted path. Full note: [`docs/node-reference.md`](../../../docs/node-reference.md#transform--call-a-functionnodeurl).

## Common mistakes

| Mistake | Reality |
|---|---|
| Adding `json_object` + explicit `transform` parse between an agent and render/branch | Agents are parse-by-default (ADR-0004) — they `extract_json` their own output and emit a failed verdict on bad JSON (which the retry+feedback loop catches). Drop the extra stages; the pipeline is shorter and the data-flow contract is automatic. The validator+parse pattern still works (the inner parse is a no-op merge) but it's noise. |
| Setting `"parse": false` on an agent that flows into render/branch | The data-flow graph linter rejects this at load time with the exact fix. If you actually want raw text downstream, add an explicit `transform` with `call: "envelope"` to merge. If you want render to render `{{raw}}` literally, set `allow_unfilled: true` on the render node. |
| Expecting a `by_model` string to script multiple attempts | A bare string = ONE reply (then exhaustion → default `""`). To script a refix loop, use a list — one entry per attempt. |
| `call: "envelope"` fn with the wrong signature | It's `fn(envelope, config) -> dict` (dict spreads over payload). `fn(payload)` dies with a raw `TypeError` traceback, not a friendly message. |
| Parse transform using `json.loads` on agent output | Real `sonnet`/`haiku` wrap JSON in markdown fences; strict `json.loads` breaks on the first real-model run. Use `from yaah.jsonio import extract_json` and `extract_json(envelope.payload.get("raw", "{}"))` — the engine's `JsonObjectValidator` already does this; examples should match. |
| Transform with `call: "envelope"` returning `{key: val}` without spreading | Transforms with `call: "envelope"` REPLACE the payload by default. Use `return {**envelope.payload, "key": val}` to enrich. The dropped keys footgun is the silent kind — downstream stages see `{{placeholders}}` they can't fill. |
| Inventing a new node type to "encapsulate" a sub-graph | `fork` + `fanin` + `transform` likely compose it. `subpipeline` was added and retired in 24h for this reason. |
| `human_gate` with only `then`, no `branch` on `decision` | It's a pause, not a gate. Human's reject is ignored. |
| Validators with `max_attempts: 0` or no `feedback` | No retry / no refix loop. The agent gets one shot. |
| Hardcoding work-tmp dirs or repo specifics in the pipeline | Adaptation layer leaks. Keep host-specifics in the root config. |
| Skipping the `.fake.json` overlay | No CI scaffold. The next time you change the graph, drift starts. |
| Copying the canonical graph into the `.fake` instead of `_extends` overlay | 3× the bytes, 3× the drift surface. Use `_extends`. |
| Asking 9 questions when the user already gave the shape | Skip what they already specified. The Q&A header says this — apply it. |
| Writing the JSON without showing the draft first | Hand-off without consent. Show it, ask "adjust?". |
| Writing root config with bare strings (`"transport": "inproc"`) | `yaah.validate.validate_root` catches this with a JSON-shaped rewrite suggestion (`rewrite as {"type": "inproc"}`), but emit the typed-block shape first so you don't see the error. Real roots use typed blocks (`{"type": "inproc"}`) and `providers`/`prompt_sources`/`state` are dicts of typed-blocks. |
| Misspelling `trace.mode`, `transport.type`, `state.type`, `trace.capture`, or `trace.sinks[].type` | `validate_root` surfaces these at LOAD with `did you mean 'X'?` (R15). Use only values listed in `validate.py` / the module catalog. |
| Setting `trace.capture` but `trace.mode: "none"` | Captures get silently dropped. `validate_root` surfaces this cross-field mistake — pick `mode: "tracer"` or remove `capture`. |
| Maintaining two near-duplicate roots when only providers differ | Use the `_fake` block + `--fake` flag (see fake-mode shapes in [`pipeline-reference.md`](pipeline-reference.md)). One root file. |

## Before handoff — generate → validate → repair (R16)

The recipe is **generate → validate → repair**, never "trust the draft." Walk the emitted config through `validate_root` / `validate_pipeline` MENTALLY before showing it to the dev:

1. **Top-level keys** — every non-`_`-prefixed key in the root must be in `_ROOT_KEYS` (see `validate.py`). A typo like `tracee:` or `default_provder` is the most common drift.
2. **Shapes** — `transport` / `state` are `{"type": "..."}` typed-blocks; `providers` / `prompt_sources` / `data_sources` / `data_sinks` / `mcp_sources` are named-maps of typed-blocks; `default_*` / `pipeline` / `input` are strings; `run` / `interactive` are bools.
3. **Enum values** — `trace.mode` ∈ `("none","tracer")`, `transport.type` ∈ `("inproc","localbus","nats")`, `state.type` / `trace.sinks[].type` / `trace.capture` from the module catalog.
4. **Cross-field** — never combine `trace.mode: "none"` with a non-empty `trace.capture` (captures get silently dropped).
5. **Pipeline graph** — every `then` / `branch.routes` / `branch.default` / `fanin.expect` resolves to a declared stage; every `node` / `validators[*]` / fanout role resolves to a declared node.
6. **Data flow — does each key exist by the time it's read?** The check the structural ones miss, and the one that bites hardest. Two halves:
   - **Trace it.** Walk the stages in graph order, tracking the keys AVAILABLE on the payload (from `input`, each upstream node's output, and any `carry:` lists). For every stage, the keys it READS must already be available: a `render`'s and an `agent` prompt's `{{placeholders}}` (inline or `file:`), and a `branch`'s `on:` key. A read with no upstream producer is the silent-wrong class — the model gets an empty `{{slot}}` and improvises or refuses (this was BUG-697: a prompt's `{{bug}}`/`{{context}}` that no upstream stage produced), or `render` dies at runtime with `render_unfilled_placeholders`. **Caveat:** a `transform` or a `parse:true` `agent` produces keys *opaquely* (Python / the model decides what comes out), so treat those as "could produce anything" and only flag a read with NO possible upstream producer and not in `input`/`carry` — that keeps the check free of false positives.
   - **Run it.** Where the pipeline is fakeable, `yaah run <root>.fake.json` is the surest data-flow check — the engine's own `render_unfilled_placeholders` / `not_json` errors name the missing key exactly. The trace above is what covers the stages a fake can't exercise (a real model, an external API, a worktree).

**If any check fails, FIX the draft and re-walk it before handoff.** Don't show the dev the broken draft and ask them to spot the error — they hired you to catch it.

**Confirmation step the dev runs:** `yaah <root> --explain` (R13) prints the EFFECTIVE config (post-`_extends`, post-`_fake`, with defaults) and validates it. Include this command in the closing artifact so they can verify what would actually load.

## Output

Write four files plus the supporting tree, then **close with both** (1) a plain-language summary of what you set and why, and (2) the run command — never hand off without a command the dev can paste.

**Plain-language summary shape** (one paragraph, before the run command):

> "Set up a `<shape>` pipeline with stages `<a → b → c>`. Providers: `<fake_scripted>` for the overlay, `<claude_cli>` for real (swap via `default_provider`). Trace: `<console>` (default). Exposed to the agent: `<none / payload.[diff]>`. Validators: `<json_object on every agent + 3-attempt feedback loop>`. Why these choices: `<smallest viable / matches their stated goal of X>`."

**Files:**
- `<name>-pipeline.json` — canonical
- `<name>-pipeline.fake.json` — thin `_extends` overlay
- `<name>.claude.local.json` — real root
- `<name>.fake.local.json` — fake root (also `_extends` the claude root)

**Supporting tree** (scaffold whatever the JSON references, or the run fails on first execute):
```
<your-app>/
├── prompts/<role>.md           # one per agent role
├── fixtures/<name>-input.json  # the input envelope
└── templates/<name>.html       # if you use a `render` node
```

**Verify with the offline overlay first** (this command is the closing artifact — print it after the files):
```bash
yaah <name>.fake.local.json
```
(After `pip install -e <repo>`. If uninstalled, prepend `PYTHONPATH=<abs>/src` and replace `yaah` with `python3 -m yaah.runtime`.)

**Inspect first if curious about defaults:** `yaah <name>.fake.local.json --explain` (R13) prints the effective config with per-key provenance — `(user)` / `(extends:<base>)` / `(fake)` / `(default)` — and validates it. Useful when a dev asks "where did `trace.mode: tracer` come from?".
