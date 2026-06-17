# Copilot instructions — YAAH

Full guide: **[AGENTS.md](../AGENTS.md)** (read it first). YAAH is a domain-free
runtime for orchestrating agentic workers; a pipeline is JSON (`nodes` + `graph`),
a root config says how to run it. Start: [docs/quickstart.md](../docs/quickstart.md),
[docs/tutorial.md](../docs/tutorial.md), [examples/](../examples/).

Rules that bite hardest:
- **Data-flow contract:** an agent's reply is a STRING in `payload["raw"]`. A
  `json_object` validator only *checks* it; a `transform` parse step merges it.
  Every `agent → render`/`branch` edge needs a parse, or you ship a literal
  `{{placeholder}}` at exit 0.
- **A human gate must `branch` on `decision`** (only `then` = a pause, not a gate).
- **Domain-free engine:** nothing in `src/yaah/` may name a stage, tenant field,
  test runner, or anything app-specific — adaptation lives in the app's config.
- **`fn:module:func` in config is executed code** — never let a payload value reach
  `importlib`/a shell/an fs path/a URL unsanitized.
- **One class per file** with a use-case docstring (who calls it, where, why).

Tests are script-style and Python 3.9 compatible: `python3 scripts/run_tests.py`.
Always ship a `.fake.json` overlay so pipelines run offline. Don't commit unless asked.
