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
- `config-flow-pipeline.json` — 5 stages: snapshot → extract → check → parse → render-svg → land. **No human gate** — config-flow is regenerable; the user re-runs whenever. Default model: `claude-sonnet-4-6` (haiku tested in the 2026-06-19 A/B run; cheaper but produced denser, less-readable layouts on this task — see "A/B comparison" below).
- `config-flow-pipeline-ab.json` — A/B variant. **No gate** — writes BOTH SVGs to disk with `-a` and `-b` appended, in the same dir. The comparison IS the artifact; you open both and pick visually.
- `transforms.py` — `snapshot_config_flow` (the new one) + `parse`/`render`/`write` (copied from arch-drift) + A/B helpers (`UsageAttacher`, `merge_candidates`, `prepare_ab_template`, `write_both_candidates`).
- `prompts/extract.md` — config-flow-specific prompt.
- `fixtures/input.json` — default target (arch-drift.dogfood-self.json — full chain, most to show).
- `visualize` — the wrapper script described above.

## The three ways to run it

| Run | Use when | Cost |
|---|---|---|
| `./visualize <target>` | One-shot SVG production for any config. The convenient default. | ~$0.05–0.20 + ~80s (sonnet; variance) |
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

## A/B comparison mode (sonnet vs haiku)

The single-model pipeline defaults to **`claude-sonnet-4-6`**. The 2026-06-19
A/B run on the arch-drift.dogfood-self target showed both models can produce
a valid config-flow diagram, but sonnet draws cleaner layouts on this task:

| | Sonnet | Haiku |
|---|---|---|
| Cost (this run) | $0.18–$0.34 (variance) | ~$0.045 |
| Wall (extract) | 84–230s (high variance) | ~37s (consistent) |
| Out-tokens | 6900–17400 | ~5900 |
| Layout quality | clean subgraph separation; extends-chain horizontal | denser; mixes flow-vs-class-diagram styles; extends-chain vertical |

Haiku is cheaper and faster, but the layout it picks is less readable —
subgraph boundaries blur, references cross more, the extends chain
renders vertically instead of as a stack. **A better prompt with
explicit layout hints could probably close the gap**, but the current
prompt gives the model freedom and haiku makes worse layout choices.
Until the prompt is tightened, sonnet is the default.

To re-compare on a config you find tougher:

```bash
cd examples/config-flow
python3 -m yaah.runtime config-flow-ab.real.json
# → produces TWO SVGs side-by-side in the target's diagrams/:
#     <stem>-a-claude-sonnet-4-6-<utc>.svg + <stem>-a.svg  (latest pointer)
#     <stem>-b-claude-haiku-4-5-<utc>.svg  + <stem>-b.svg
# → prints paths on stderr; `open <both>` to compare visually
```

Stderr at exit shows tokens-per-candidate so you can see the cost delta.
If you've prompt-tuned haiku into producing layouts as clean as sonnet's,
switch the default in `config-flow-pipeline.json`'s `role:extract`.

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
