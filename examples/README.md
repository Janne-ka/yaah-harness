# Examples

Runnable YAAH pipelines. The `*.local.json` overlay of each uses a **fake** model
backend, so it runs for free and deterministically — `cd` into one and run it:

```bash
cd examples/<name> && yaah run <name>.local.json
```

(Not installed? `python3 -m yaah.runtime <config>` is the equivalent of `yaah run
<config>`; from a source checkout prefix `PYTHONPATH=src`. `fn:` transforms
resolve relative to the config's directory, so a config keeps its `transforms.py`
beside it. New here? Start with the [Quickstart](../docs/quickstart.md) and
[Tutorial](../docs/tutorial.md).)

## Config-driven pipelines (JSON)

| Example | Shows | Run |
|---|---|---|
| **`hello-yaah/`** | the smallest real pipeline — `agent → validate+retry → parse → render`, and the data-flow contract | `yaah run starter.local.json` |
| **`review-pipeline/`** | a **branch** + a **human gate** with durable suspend/resume (`yaah list` / `yaah resume`) | `yaah run review.local.json` (then `yaah list`, `yaah resume <id> decision.json`) |
| **`fork-join/`** | **fork** to parallel lenses + **fan-in** with a `reduce` | `yaah run review.local.json` |
| **`arch-drift/`** | an **instrumented** pipeline: snapshot → extract → render → diff → human gate → land, with tracing + cost (the canonical instrumented archetype) | `MERMAID_RENDERER=:canned yaah run arch-drift.local.json` |
| **`config-flow/`** | a **meta-tool** pipeline — operates on *other* yaah configs and draws their flow as an SVG | `MERMAID_RENDERER=:canned yaah run config-flow.local.json` |

## Harness / agent-loop (tool-using agents)

Pipelines where an agent uses tools — either YAAH-driven (`agent_loop`) or
model-driven (native CLI tools).

| Example | Shows | Run |
|---|---|---|
| **`spike-harness/`** | the `agent_loop` primitive end-to-end against a scripted tool backend (harness owns the loop) | `yaah run local.json` |
| **`coding-agent/`** | both tool-use patterns — YAAH-driven `agent_loop` and model-driven `claude_cli` — fixing a one-char bug | `YAAH_CODING_AGENT_WORKDIR="$PWD/fixtures/buggy_code" yaah run local.json` |

## Scripted (Python) POCs

For wiring a harness directly in code (no config file):

- **`config_pipeline.py`** — `build()` a harness from a config dict; swap fake↔real by editing one `model` string.
- **`agent_stage_poc.py`** — a single agent stage end to end.

```bash
PYTHONPATH=src python3 examples/config_pipeline.py
```

## Building your own

Describe what you want to an AI assistant pointed at [`AGENTS.md`](../AGENTS.md) —
it knows the node types and the conventions and will draft the JSON. Then run the
`*.local.json` overlay (fake provider; for SVG examples add
`MERMAID_RENDERER=:canned`) to verify it offline before going real.
