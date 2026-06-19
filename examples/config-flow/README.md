# config-flow — visualize any yaah config's flow as an SVG

A reusable tool: point it at any yaah root config and get back an SVG of how
that config actually flows — `_extends` chain, pipeline graph, fixture, and
how the pipeline's `model:` / `prompt:` / `target:` strings resolve back into
the root's named maps. Works on any yaah example or a user-authored config.

This is the third yaah example to ship as a working pipeline (after
hello-yaah, review-pipeline, fork-join, arch-drift), and the first that
exists as a *tool* — its job is to operate on other yaah configs.

## Quick start (the easy way)

```bash
examples/config-flow/visualize examples/hello-yaah/starter.local.json
#  → examples/hello-yaah/diagrams/starter.local_<utc>.svg
```

That's it. The wrapper:
- Computes the output path: `<target-dir>/diagrams/<config-stem>_<utc>.svg`
  (and a stable `<config-stem>.svg` alias next to it).
- Runs the pipeline real-mode, auto-approving at the gate.
- Prints the SVG paths on stderr at exit so you don't have to read the
  RESULT envelope to find them.

Override the output dir:

```bash
OUTPUT_DIR=. examples/config-flow/visualize examples/hello-yaah/starter.local.json
# → ./starter.local_<utc>.svg + ./starter.local.svg
```

## What "config-flow" means

A yaah root config doesn't run a pipeline — it *describes* one. The pipeline
is referenced by the root's `pipeline:` key; the fixture by `input:`; the
nodes inside the pipeline reference back into the root via strings like
`model: "claude:sonnet"` (looked up in `providers.claude`) or
`prompt: "file:extract"` (looked up in `prompt_sources.file`). On top of
all that, `_extends` lets configs chain.

This tool walks ALL of that and asks claude to draw it.

## Files at a glance (inheritance map)

```
config-flow.local.json                ← offline / fake provider / canned renderer
  └─ config-flow.real.json            ← real claude + real mmdc + by_model:null

config-flow-ab.dogfood.json           ← A/B comparison (sonnet vs haiku), auto-approve at gate
```

Plus:
- `config-flow-pipeline.json` — 5 stages: snapshot → extract → check → parse → render-svg → land. **No human gate** — config-flow is regenerable; the user re-runs whenever. Default model: `claude-haiku-4-5`. (The 2026-06-19 A/B run initially showed haiku producing denser/less-clear output; a tightened prompt — explicit 4-subgraph structure with named IDs and required layout directions — closed the gap. With the current prompt, haiku produces output structurally equivalent to sonnet at ~3.4× lower cost and ~2× faster.)
- `config-flow-pipeline-ab.json` — A/B variant. **No gate** — writes BOTH SVGs to disk with `-a` and `-b` appended, in the same dir. The comparison IS the artifact; you open both and pick visually. Sonnet uses `prompts/extract.md` (loose); haiku uses `prompts/extract-strict.md` (tight) — see "Two prompts, one per model" below.
- `transforms.py` — `snapshot_config_flow` (the new one) + `parse`/`render`/`write` (copied from arch-drift) + A/B helpers (`UsageAttacher`, `merge_candidates`, `prepare_ab_template`, `write_both_candidates`).
- `prompts/extract.md` — loose prompt: gives the model freedom on layout. Used by sonnet in A/B. Sonnet's judgment beats the over-constrained prompt for it.
- `prompts/extract-strict.md` — tight prompt: explicit 4-subgraph spec with named IDs + skeleton example. Used by haiku in A/B AND in the single-model pipeline (default haiku). Closes haiku's natural-layout gap.
- `fixtures/input.json` — default target (arch-drift.dogfood-self.json — full chain, most to show).
- `visualize` — the wrapper script described above.

## The three ways to run it

| Run | Use when | Cost |
|---|---|---|
| `./visualize <target>` | One-shot SVG production for any config. The convenient default. | ~$0.07 + ~40s (haiku) |
| `python3 -m yaah.runtime config-flow.real.json` | Default target (arch-drift), runs end-to-end with no human input. | same |
| `python3 -m yaah.runtime config-flow-ab.real.json` | A/B comparison sonnet vs haiku — produces BOTH SVGs (`<stem>-a.svg` + `<stem>-b.svg`) side-by-side in the target's `diagrams/` dir. Fully automated, no gate. Use to verify haiku is still good enough on a new config shape. | ~$0.23 + ~85s |
| `MERMAID_RENDERER=:canned python3 -m yaah.runtime config-flow.local.json` | Verify the pipeline shape without an LLM key. Output is a canned SVG (visually meaningless). | zero |

## Dependencies

Same as arch-drift:

| Need | Why | Install |
|---|---|---|
| `claude` on PATH | yaah's `claude_cli` backend shells out to it. | comes with Claude Code; `which claude` |
| `mmdc` (mermaid-cli) | Renders the agent's mermaid output to SVG. | `npm install -g @mermaid-js/mermaid-cli` |
| python 3.9+ | yaah runtime. | system |

## How the wrapper works under the hood

`./visualize ../hello-yaah/starter.local.json` does this:

1. Resolves the target to an absolute path; computes the output dir
   (`<target-dir>/diagrams/`, or `OUTPUT_DIR` if set).
2. Writes `fixtures/_runtime.json` (gitignored) with `target_config_path`,
   `arch_svg_path`, `arch_svg_dir`.
3. Writes `_visualize-run.json` (gitignored shim root) that `_extends
   config-flow.dogfood.json` and overrides `input:` to point at the
   runtime fixture.
4. Runs `python3 -m yaah.runtime _visualize-run.json`.
5. Cleans up the shim root on exit (trap).

The gitignored intermediates (`fixtures/_runtime.json`, `_visualize-run.json`)
exist because yaah's root configs reference inputs by *path* — no `--input`
CLI flag. The two extra files are the price of keeping the engine surface
small.

## Two prompts, one per model

The first A/B revealed something useful: a prompt good for one model can
be wrong for another. We now ship two:

- **`prompts/extract.md`** (loose) — gives the model freedom on layout
  choices. Sonnet's judgment produces better layouts when it has room.
  Used by sonnet in the A/B variant.
- **`prompts/extract-strict.md`** (tight) — explicit 4-subgraph IDs,
  required per-subgraph directions, skeleton example. Closes haiku's
  natural-layout gap. Used by haiku everywhere (single-model + A/B).

Why not one prompt? With the tight prompt, sonnet drops from ~17K → ~10K
output tokens — the constraint cuts its "freedom budget" and the result
gets blander. With the loose prompt, haiku picks structurally worse
layouts (3 subgraphs instead of 4, vertical extends chain, mixed flow/
class-diagram cues). The right tool per model: sonnet wants room; haiku
wants rails. A/B without that asymmetry would be unfair to either side.

## A/B comparison mode (sonnet vs haiku)

The single-model pipeline defaults to **`claude-haiku-4-5`** — see below
for how that decision got made. To re-compare on a config you find tougher:

```bash
cd examples/config-flow
python3 -m yaah.runtime config-flow-ab.real.json
# → produces TWO SVGs side-by-side in the target's diagrams/:
#     <stem>-a-claude-sonnet-4-6-<utc>.svg + <stem>-a.svg  (latest pointer)
#     <stem>-b-claude-haiku-4-5-<utc>.svg  + <stem>-b.svg
# → prints paths on stderr; open both SVGs in browser to compare
```

Stderr at exit shows tokens-per-candidate so you can see the cost delta.

### How the haiku default got chosen (a case study for using A/B)

Two A/B runs on 2026-06-19, both against
`examples/arch-drift/arch-drift.dogfood-self.json` (a 4-layer `_extends`
chain — non-trivial target):

**Run 1 — original prompt (gave model lots of layout freedom):**

| | Sonnet | Haiku |
|---|---|---|
| Cost | $0.18–$0.34 (high variance) | ~$0.045 |
| Wall (extract) | 84–230s | ~37s |
| Output | 4 clean subgraphs, clear separation | 3 subgraphs, denser, harder to parse |

Haiku was cheaper but the layout it picked was meaningfully worse.
Initial reaction: keep sonnet as default. Then: "haiku would need more
instructions" — try tightening the prompt.

**Run 2 — tightened prompt (explicit 4-subgraph IDs + layout
directions + skeleton example):**

| | Sonnet | Haiku |
|---|---|---|
| Cost | ~$0.24 | ~$0.07 |
| Wall (extract) | ~144s | ~37s |
| Output | 4 subgraphs, exactly the spec | 4 subgraphs, exactly the spec |
| Quality | structurally equivalent | structurally equivalent |

With the constrained prompt, **haiku closed the gap**. Both models now
produce ~the same structure; haiku's labels are slightly more verbose,
sonnet includes one extra registry box, neither honors `direction LR`
on the extends chain (universal model behavior, not a quality issue).

Conclusion: switch to haiku at the default, ~3.4× cheaper, ~2× faster,
output indistinguishable in quality. The deciding artifact was running
the A/B with the same constrained prompt twice and comparing the SVGs
in a browser. If you ever find a config shape where haiku visibly
underperforms, switch the default back in
`config-flow-pipeline.json`'s `role:extract` — but tune the prompt
first; that closed the gap last time too.

The full walkthrough — three SVGs side-by-side (sonnet+loose,
haiku+loose, haiku+strict) and the asymmetry that explains why the
single prompt didn't work — is in
[`docs/case-study/prompt-tuning/`](../../docs/case-study/prompt-tuning/).

## Adding to your own project

If you publish a yaah-based pipeline and want a config-flow diagram in your
own docs:

```bash
# from your project
git submodule add https://github.com/yaah-harness/yaah.git vendor/yaah
# (or pip install yaah-harness once it's on PyPI)
vendor/yaah/examples/config-flow/visualize my-pipeline.json
```

The SVG lands next to `my-pipeline.json` in `diagrams/`. Commit it.
