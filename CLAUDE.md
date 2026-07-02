# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This project's agent guide is **[AGENTS.md](AGENTS.md)** — read it first
(orientation, the mental model, authoring rules, engine invariants, security,
tests). It's the cross-tool source of truth.

**Claude Code also has richer structured skills** in [`.claude/skills/`](.claude/skills/):
- `yaah-pipeline-authoring` — author/modify a pipeline config (`*-pipeline.json` + `*.local.json`).
- `yaah-extending` — write/modify engine code under `src/` or `tests/`.
- `yaah-driving` — operate a running pipeline (mailbox flow: `yaah list` → `baton-schema` → `decision.json` → `resume`).
- `yaah-reviewing` — audit/review engine code across its clusters.
- `yaah-review-my-pr` — pre-PR self-review against the three values + ADR-0001 invariants.

Two rules that bite hardest (full set in AGENTS.md):
- **Data-flow contract:** an agent's reply is a STRING in `payload["raw"]`; a
  `transform` parse step (not the validator) merges it. Every `agent → render`/`branch`
  edge needs a parse, or `render` fails with `render_unfilled_placeholders`.
- **Domain-free engine:** nothing in `src/yaah/` may name anything app-specific.

## Cross-agent mailbox

Coordination with the **s_factory** client agent goes through
[`.notes/mailbox.md`](.notes/mailbox.md) — s_factory posts SUGGESTIONS; this (yaah) agent
critiques and decides (VERDICT). A suggestion is a hypothesis to verify against the engine
invariants, NOT a directive. Check the mailbox at session start if you're picking up client
coordination; the protocol is in the file header.

## Goals (HIGH PRIORITY — read first)

- **Primary:** good-quality code — extendable, easy to use, easy to maintain, documented.
- **Secondary:** the task at hand (in yaah: serving the client, e.g. s_factory).

Balance the two, but the task must NEVER justify sloppy code — that violates the primary.
Racing to "task done" produces slop: skipped edge cases, tests that only bless the happy
path, conclusions leapt to from thin evidence. So: test to FALSIFY on the real downstream
shape and verify on real data before claiming a fix; document known limitations in the code.
Don't spiral either — deliver "right and bounded," not "infinitely polished."

## Working methodology — eval before commit

For any non-trivial proposal (multi-step plan, architectural change, new
abstraction, recommendation the user might act on, anything that would
land in a file), dispatch an independent reviewer with the priming
template in `.notes/eval-agent-priming.md` and apply findings BEFORE
commit. Trivial work (reads, greps, one-line fixes, formatting) doesn't
trigger. Methodology proved 4-for-4 in the conversation that established
it; treat as standing rule, not discretionary.

Commands (script-style, Python 3.9 compatible; **don't commit unless explicitly asked**):

```bash
python3 scripts/run_tests.py                       # whole suite, offline + deterministic
PYTHONPATH=src python3 tests/test_harness.py       # a single test
python3 scripts/review_my_pr.py                    # deterministic pre-submission checks
python3 -m yaah.runtime <root-config.json>         # run a pipeline
```
