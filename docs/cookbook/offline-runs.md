# Offline runs — fake providers + the `--fake` overlay

Three idiomatic patterns for running a pipeline **without an API key**, in CI,
in tests, or to iterate on prompts before paying a model. All three are
already in the engine — pick the one that matches your project shape.

The unit cost of each is dollars-per-run × the times you run it. Once the
pipeline works against fake providers, the only thing left to validate against
a real model is the *prompt quality*, not the *plumbing*. Most authoring loops
live in fake mode; real-mode runs are the punctuation.

## Pattern 1 — single-file `*.local.json` with a fake provider baked in

The simplest case. Used by `hello-yaah`, `fork-join`, `review-pipeline`,
`config-flow`. The pipeline references a provider name (e.g. `"fake"`); the
deployment root binds that name to a `fake_scripted` backend with canned
responses keyed by model:

```json
{
  "transport": {"type": "inproc"},
  "providers": {
    "fake": {
      "type": "fake_scripted",
      "by_model": {"summarize": ["{\"summary\":\"hello\"}"]}
    }
  },
  "default_provider": "fake",
  "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
  "default_prompt_source": "file",
  "state": {"type": "memory"},
  "pipeline": "starter.json",
  "input": "fixtures/input.json",
  "run": true
}
```

Run it: `yaah run starter.local.json`. No flags. No API key. Exit 0 or
non-zero like any other run.

**Use this when** you ship one example/project and the offline run is the
default. Real-model is an extension someone else adds.

## Pattern 2 — paired files: `*.local.json` (offline base) + `*.real.json` (real overlay)

The example that has both: `examples/arch-drift/`. The `.local.json` carries
the full offline config (fake providers, fake state, prompt sources, pipeline
ref, input ref). The `.real.json` is a thin overlay that **extends** the
local file and swaps only the provider definition to the real backend
(claude_cli / litellm / etc.):

```json
{
  "_extends": "arch-drift.local.json",
  "providers": {
    "claude": {"type": "claude_cli", "binary": "claude", "by_model": null}
  },
  "state": {"type": "file", "dir": ".arch-drift-state"}
}
```

Run offline: `yaah run arch-drift.local.json`.
Run for real: `yaah run arch-drift.real.json`.

**Use this when** the same pipeline runs in two modes (offline for CI, real
for production), and you want each mode to be one command — no flag, no
mental model of "which side is overlaid where." The base is the canonical
offline shape; the overlay names exactly what changes for real.

`_extends` is deep-merge with JSON Merge Patch delete semantics (`null` at a
key deletes it). Lists are replaced, not appended. See
[durable-state.md](../durable-state.md) and `runtime_factories.py:_deep_merge`
for the rule.

## Pattern 3 — `_fake` block inline + the `--fake` CLI flag

A power-user shortcut. One root config carries the real settings at the top
level and an underscored `_fake` block (a comment-keyed sibling) at the
bottom. The `--fake` flag merges the `_fake` block over the top level at
load time:

```json
{
  "providers": {"claude": {"type": "claude_cli", "binary": "claude"}},
  "default_provider": "claude",
  "state": {"type": "file", "dir": ".state"},
  "pipeline": "starter.json",
  "input": "fixtures/input.json",
  "run": true,

  "_fake": {
    "providers": {
      "claude": {
        "type": "fake_scripted",
        "by_model": {"summarize": ["{\"summary\":\"hello\"}"]}
      }
    },
    "state": {"type": "memory"}
  }
}
```

Run real: `yaah run root.json`.
Run offline: `yaah run root.json --fake`.

The merge is **shallow** (top-level keys are replaced, not deep-merged) — the
whole `providers` dict gets overwritten by the `_fake.providers` dict if
present. If you want deep-merge, use Pattern 2.

**Use this when** you can't / don't want a second file and the swap is clean
(no need to preserve any real-config nesting). The `_fake` key starts with
`_` so the validator's unknown-key check ignores it (`_*` is the comment
convention).

## Picking between them

| | Pattern 1 | Pattern 2 | Pattern 3 |
|---|---|---|---|
| files | 1 | 2 | 1 |
| run command | `yaah run x.local.json` | `yaah run x.{local,real}.json` | `yaah run x.json [--fake]` |
| merge semantics | n/a | deep + JSON Merge Patch | shallow |
| best for | demos, single-mode projects | two-mode pipelines with one offline canonical | quick-iteration single-file |

## Verifying offline first (the engine rule)

[AGENTS.md](../../AGENTS.md) makes this a load-time contract:

> Always ship a `.fake.json` overlay (or equivalent) so the pipeline runs
> offline/CI for free. Verify on it before going real.

The rule survives because fake-mode catches three failure modes that real
mode also catches but more expensively: missing prompt files,
unfilled-placeholder bugs, validator misconfig. A failing fake run never
needed to be a real run.

## The `fake_scripted` provider

The canned-reply backend the patterns above bind to:

```json
{
  "type": "fake_scripted",
  "by_model": {
    "summarize": ["{\"summary\":\"hello\"}"],
    "verify":    ["{\"verdict\":\"ok\"}", "{\"verdict\":\"ok\"}"]
  }
}
```

- `by_model` is keyed by model name (matched against the pipeline's
  `model: "provider:name"` value's `name` half).
- Each key's value is a list of replies; calls consume them in order. Lists
  cycle, so one entry suffices for stages called once.
- The reply is just a string. For agent stages with parse-by-default, that
  string should be valid JSON — the agent self-parses it onto the payload
  (see [decisions/0004-parse-by-default.md](../decisions/0004-parse-by-default.md)).

For non-deterministic scenarios (test that retry works, simulate a failure
chain, etc.), the reply list can mix valid + invalid JSON.
