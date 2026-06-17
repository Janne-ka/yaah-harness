# Examples

Runnable YAAH pipelines. The config-driven ones use a **fake** model backend, so
they run for free and deterministically — `cd` into one and run it:

```bash
cd examples/<name> && python3 -m yaah.runtime <name>.local.json
```

(`python3 -m yaah.runtime` puts the example dir on the import path so its `fn:`
transforms resolve. New here? Start with the [Quickstart](../docs/quickstart.md)
and [Tutorial](../docs/tutorial.md).)

## Config-driven pipelines (JSON)

| Example | Shows | Run |
|---|---|---|
| **`hello-yaah/`** | the smallest real pipeline — `agent → validate+retry → parse → render`, and the data-flow contract | `python3 -m yaah.runtime starter.local.json` |
| **`review-pipeline/`** | a **branch** + a **human gate** with durable suspend/resume (`--list` / `--resume`) | `python3 -m yaah.runtime review.local.json` (then `--list`, `--resume <id> decision.json`) |
| **`fork-join/`** | **fork** to parallel lenses + **fan-in** with a `reduce` | `python3 -m yaah.runtime review.local.json` |

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
`.fake.json` overlay to verify it offline before going real.
