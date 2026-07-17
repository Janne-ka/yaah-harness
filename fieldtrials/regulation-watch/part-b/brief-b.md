# Part B — Debugging brief: fix the broken regulation-watch pipeline

This directory is a complete, almost-working yaah project: a pipeline that
classifies a regulation notice, drafts a summary with a bounded verify→retry
loop, parks at a human gate, and renders a digest. It runs offline with a fake
provider. It is **broken on purpose** — several distinct problems are seeded in
the config, prompts, transform, and overlay.

Do NOT read `../answer-key.md` — that is for the trial runner.

## Your job

Make this project **strict-clean and offline-green**:

- `yaah validate --strict` exits 0 with no lint warnings.
- `yaah run root.local.json` runs to the human gate, parks, and — after you
  drive it (`yaah list` → `yaah baton-schema` → `decision.json` → `yaah resume`)
  — completes and renders the digest.

And **document every problem you find and how you found it.** For each one:

```
## P<n>: <one-line title>
- Symptom: <the lint text, error, or wrong behavior — paste the exact output>
- How I found it: <which surface told you — a lint / an error message / --json
  output / the scaffolded AGENTS.md / a doc / trial-and-error / reading src/>
- Root cause: <the actual defect in the config/prompt/transform/overlay>
- Fix: <the change you made>
```

## Ground rules

- Offline only. The fake overlay is deterministic; keep it that way.
- Fix the pipeline, not the engine. If something looks like an engine bug rather
  than a config defect, write it down instead of editing `src/`.
- `yaah run --json` gives you a machine-readable failure object
  (`stage` / `failures[].code` / `fix_hint`) — use it when a runtime failure is
  opaque.
- Some problems trip a lint. Some only show up at runtime. At least one runs
  without complaint but behaves wrong — those are the ones worth catching.

## Deliverables

1. The fixed, strict-clean, offline-green project.
2. The problem log (format above).
3. A short note on which problems the tooling caught for you vs. which you had
   to reason out yourself.
