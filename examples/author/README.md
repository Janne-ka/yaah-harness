# author — the authoring meta-pipeline ("yaah authors yaah")

Give it a plain-language request — *"a two-stage summarize-then-review
pipeline with a human gate"* — and it produces a **valid** yaah config pair
(root + pipeline), written to disk after a human approves the draft.

The point of the example is the **self-repair mechanic**: the draft stage
carries a *validator* that runs the engine's own author-time checks
(`yaah.validate.validate_root` / `validate_pipeline`) over the draft. A draft
that fails is retried with the exact validation errors appended to the
agent's prompt (`max_attempts` + `feedback: true`) — **the engine's retry
loop IS the repair loop**. No bespoke repair code exists anywhere in this
example; it composes what the harness already does for every validator.

Archetype: [`meta-tool`](../../docs/archetypes.md#5-meta-tool) (the pipeline's
output is another yaah config) with a `branch-with-gate` tail.

## Shape

| stage     | node                | type                        | what it does |
|-----------|---------------------|-----------------------------|--------------|
| `draft`   | `role:draft`        | `agent`                     | writes `{name, root, pipeline}` JSON from the request + the grammar card in [`prompts/draft.md`](prompts/draft.md); `output_schema` gates the top-level keys |
|           | `role:check-config` | `transform` (validator)     | runs `validate_root` + `validate_pipeline` on the draft, returns a Verdict; failures feed the retry loop (`max_attempts: 3`, `feedback: true`) |
| `approve` | `role:approve`      | `human_gate`                | `approve_or_revise` on the validated draft; `revise` loops back to `draft` with the human's feedback |
| `write`   | `role:write`        | `transform`                 | writes `<name>.json` + `<name>-pipeline.json` under `out_dir` (config-relative; sanitized) |

```
draft ──(validator fails → feedback retry, up to 3)──▶ draft
draft ──▶ approve ──decision=revise──▶ draft
                 └─decision=approve──▶ write ──▶ done
```

## Run it offline (no API key, deterministic)

```bash
PYTHONPATH=src python3 -m yaah.runtime examples/author/author.fake.json
```

`author.fake.json` scripts the "model" with two canned drafts
([`fixtures/draft.fake.json`](fixtures/draft.fake.json)):

1. **attempt 1 is INVALID** — its `summarize` stage says `"then": "reviw"`,
   an unresolvable stage ref. `check-config` rejects it with
   `stage 'summarize': then 'reviw' is not a stage`.
2. the harness retries the draft stage, appending that error to the prompt as
   FEEDBACK; **attempt 2 is the corrected draft** and passes.

The overlay auto-approves the gate (`decisions:`), so the run finishes
unattended and lands `generated/summarize-review.json` +
`generated/summarize-review-pipeline.json`. The trace file (`trace.jsonl`)
records one `model_call` per attempt — two calls = the repair loop fired.

## Run it for real

```bash
PYTHONPATH=src python3 -m yaah.runtime examples/author/author.local.json
```

Real `claude_cli` drafts; the run parks at the `author:config-review` gate.
Drive it with the mailbox flow: `yaah list` → `yaah baton-schema <id>` →
write `decision.json` → `yaah resume`. Edit
[`fixtures/input.json`](fixtures/input.json) to change the request.

## Files

- `author-pipeline.json` — the meta-pipeline (nodes + graph).
- `author.local.json` — canonical root (real model, human gate parks).
- `author.fake.json` — offline overlay (`_extends` the canonical: scripted
  drafts, auto-approve, file trace).
- `prompts/draft.md` — the draft agent's template: the minimal config
  grammar, the hard rules the validator enforces, a worked example, strict
  JSON output instructions.
- `transforms.py` — `check_config` (the Verdict-returning validator) and
  `write_config` (sanitized write of the approved pair).
- `fixtures/input.json` — the request + `out_dir`.
- `fixtures/draft.fake.json` — the two scripted drafts (invalid → valid).

Test: [`tests/test_author_example.py`](../../tests/test_author_example.py)
runs the fake overlay end-to-end in a tmpdir and asserts the written config
is valid (`validate_pipeline` on the artifact) and that the repair actually
happened (two model calls; the typo'd target is gone).

## Known limitations (deliberate)

- The validator checks **config shape**, not referenced assets: the drafted
  config points at prompt files / templates that don't exist yet. Authoring
  those is the operator's follow-up (or a future stage).
- One repair dimension is scripted in the fake run (a `then` typo). A real
  model can fail in other ways; the loop feeds back whatever
  `validate_root`/`validate_pipeline` report, up to `max_attempts: 3`.
- On `revise` at the gate, the scripted provider has no third draft — the
  fake overlay is built for the approve path (CI). Revise-looping is a
  real-model flow.
