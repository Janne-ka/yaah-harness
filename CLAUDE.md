# CLAUDE.md

This project's agent guide is **[AGENTS.md](AGENTS.md)** — read it first
(orientation, the mental model, authoring rules, engine invariants, security,
tests). It's the cross-tool source of truth.

**Claude Code also has richer structured skills** in [`.claude/skills/`](.claude/skills/):
- `yaah-pipeline-authoring` — author/modify a pipeline config (`*-pipeline.json` + `*.local.json`).
- `yaah-extending` — write/modify engine code under `src/` or `tests/`.
- `yaah-reviewing` — audit/review engine code across its clusters.

Two rules that bite hardest (full set in AGENTS.md):
- **Data-flow contract:** an agent's reply is a STRING in `payload["raw"]`; a
  `transform` parse step (not the validator) merges it. Every `agent → render`/`branch`
  edge needs a parse, or you ship a literal `{{placeholder}}` at exit 0.
- **Domain-free engine:** nothing in `src/yaah/` may name anything app-specific.

Tests: `python3 scripts/run_tests.py` (script-style, Python 3.9 compatible). Don't
commit unless explicitly asked.
