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
  └─ config-flow.real.json            ← swaps in real claude + real mmdc + by_model:null
       └─ config-flow.dogfood.json    ← auto-approve at the gate (used by `visualize`)
```

Plus:
- `config-flow-pipeline.json` — 8 stages: snapshot → extract → check → parse → render-svg → report → gate → land (revise loops back to extract).
- `transforms.py` — `snapshot_config_flow` (the new one) + `parse`/`render`/`write` (copied from arch-drift).
- `prompts/extract.md` — config-flow-specific prompt.
- `templates/report.html` — gate-review page.
- `fixtures/input.json` — default target (arch-drift.dogfood-self.json — full chain, most to show).
- `visualize` — the wrapper script described above.

## The three ways to run it

| Run | Use when | Cost |
|---|---|---|
| `./visualize <target>` | One-shot SVG production for any config. | ~$0.05 + ~70s |
| `python3 -m yaah.runtime config-flow.real.json` | Default target (arch-drift), park at the gate for review. Use `yaah list` + `yaah resume` to drive. | ~$0.05 + ~70s + your time |
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

## Interactive (review-before-land) mode

When the diagram needs human judgment before it's "official," skip the
wrapper and run the real config directly:

```bash
cd examples/config-flow
# 1) writes report.html, parks at the gate
python3 -m yaah.runtime config-flow.real.json
# 2) look at the parked baton
python3 -m yaah.runtime list config-flow.real.json
# 3) (driver-skill helper) get the decision schema
python3 -m yaah.runtime baton-schema config-flow.real.json <baton-id>
# 4) deliver a decision
echo '{"decision":"approve"}' > /tmp/d.json
python3 -m yaah.runtime resume config-flow.real.json <baton-id> /tmp/d.json
```

Or revise with feedback (loops back to the extractor with the feedback in the prompt):

```bash
echo '{"decision":"revise","feedback":"group the extends chain into a subgraph"}' > /tmp/d.json
python3 -m yaah.runtime resume config-flow.real.json <baton-id> /tmp/d.json
```

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
