---
name: yaah-extending
description: Use when writing or modifying code under src/ or tests/. Not for a separate clean-up phase (do elegance inline); not for authoring a new pipeline config from scratch (use yaah-pipeline-authoring or an app-specific authoring skill).
---

# Extending YAAH

**Standing rule:** never commit unless explicitly asked.

## Overview

YAAH's value is its **discipline**: a domain-free engine, hug-the-world ports, file-based state, agent isolation, scripted gates. Most "improvements" that add a concept *delete* this value. Default to **composing existing primitives** over inventing new ones, and **delete** when an unused capability is found — see commit `b744de7` for the `SubpipelineNode` retirement: 10-file change removing a node `fanout`+`fanin` already composed.

**Before writing code, hold the form in your head.** Read [`docs/archetypes.md`](../../../docs/archetypes.md) (five pipeline shapes — what the engine is *for*) and [`docs/shape-grammar.md`](../../../docs/shape-grammar.md) (the one-page card — every root key, node type, graph construct). Engine changes that would break either are usually wrong.

## When to Use

- Adding a node type, port, adapter, builder, transform
- Editing pipeline JSON in a consuming app
- Adding tests (script-style, not pytest)
- Doing inline elegance/simplification work as part of a larger change
- **Not for:** queuing elegance work as a separate "cleanup phase" later (memory: *elegance-is-focus-not-a-phase*). Inline cleanup IS encouraged — the rule is "don't defer it as a phase," not "don't do it."

## The invariants you MUST preserve

- **Three concepts only.** Envelope, Node, Comms ([ADR-0001](../../../docs/decisions/0001-three-concepts.md)). A fourth top-level concept requires an ADR discussion first, not just an implementation.
- **Domain-free engine.** Nothing in `src/` may name a stage, tenant field, test runner, or anything else specific to a host project. If you're tempted to add an `if stage.name == "code"`, stop.
- **One class per file.** Filename matches the class. Top-of-file docstring states **who calls it, where, and why** (use case). Skip the docstring → reviewer rejects.
- **Hug-the-world ports.** Extend an existing port before inventing a new one. The pattern is *port + `routing_*` multiplexer + concrete `file_*`/`http_*` adapter*. Match the existing triad.
- **Trust boundary is implicit.** `fn:module:func` in config is RCE; payload-derived paths reach `shutil.rmtree`. Never let a payload value reach a shell command, FS path, URL, or `importlib`. If you must, sanitize at the seam and document why.
- **Agent isolation.** Each stage = fresh agent, named `carry` keys only. Never feed an agent its own critic's output.
- **Hard human gates branch on `decision`.** A `human_gate` with only `then` is a pause, not a gate.
- **Engine ships zero attachers** ([ADR-0003](../../../docs/decisions/0003-attacher-port.md)). New attachers go in CONSUMER code, never `src/yaah/`. Canonical references live at `docs/cookbook/attachers/` (non-importable, copy-paste).
- **Decision-forms catalog stays generic** ([ADR-0002](../../../docs/decisions/0002-decision-forms.md)). The `FORMS` dict in `src/yaah/harness/decision_forms.py` holds shape-only entries (`approve`, `approve_or_revise`, `free_text`, `json_schema`). A domain-specific form name (`code_review`, `legal_review`) is a domain leak; consumers extend via the `json_schema` escape hatch.
- **Error messages name the next move.** Every `raise` in `src/yaah/` follows "what went wrong + what to do next" — the rule that lets agents and tired humans self-correct in one read. If a message only states the problem, rewrite it.
- **Parse agent output with `extract_json`, never strict `json.loads`.** Real sonnet/haiku wrap JSON in markdown fences. `yaah.jsonio.extract_json` is the engine helper; the built-in `JsonObjectValidator` already uses it.
- **Minimal first.** In-memory before durable, in-process before distributed, no premature abstraction (memory: *minimal-first-extend-on-need*).

## Decision: new concept vs compose existing?

Three checks. **All must answer "no" to add a new concept:**

1. Can `fanout` + `fanin` + `transform` + existing nodes already express it? (see `ab-fork.json` for asymmetric A/B without subpipeline)
2. Does the new concept add only *organizational* value (file-level encapsulation, naming) rather than computational?
3. Does it pay costs the existing primitives don't (new node type, new test file, new restriction, depth guard)?

If any "yes" — **compose, don't add**. If you already added it, **delete it** (and its tests, demos, references) the day you notice. Note the retirement in `docs/ROADMAP.md`.

## Style

- **No comments unless the WHY is non-obvious.** Don't explain WHAT — the names do that. Don't reference the current task/PR/issue — that belongs in the commit message.
- **No error handling for impossible cases.** Trust internal contracts; validate only at system boundaries.
- **No backwards-compat shims** for code you're free to change.
- **Tests are script-style:** `"""Run: cd yaah && PYTHONPATH=src python3 tests/test_x.py"""` at top, `if __name__ == "__main__": asyncio.run(main())` at bottom. Python **3.9 compatible** (the lib is consumed by old envs).
- **Backlog goes in `docs/ROADMAP.md`**, not new doc files (memory: *todo-location*).

## Add-a-node-type recipe

```python
# src/yaah/nodes/widget_node.py
"""WidgetNode — <one line: what concept this adds>.

Used by: <which builder / which pipeline JSON role>.
Where: <when in the graph — pre-validator, terminal, fan-out branch>.
Why: <the use case that motivated it; what existing primitive failed to compose>.

Targets Python 3.9+.
"""
class WidgetNode:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope: ...
```

Then: register in `nodes/__init__.py`, builder in `build/builders.py` (`_build_widget` + `r.register("widget", _build_widget)`), test in `tests/test_widget.py`, rows in [`docs/node-reference.md`](../../../docs/node-reference.md) AND [`docs/shape-grammar.md`](../../../docs/shape-grammar.md)'s Node types table. **All in one change.** Retiring a node uses the same checklist in reverse — see `b744de7` (SubpipelineNode retirement, 10 files, one commit) for the worked example. The auto-generated [`docs/module-catalog.md`](../../../docs/module-catalog.md) regenerates from the code (`python3 scripts/build_catalog.py`); don't edit it by hand.

## Common mistakes

| Mistake | Reality |
|---|---|
| Adding a new node concept when `fanout`/`fanin`/`transform` compose it | You're paying a node type, a test file, and future drift for organizational gain. Compose. |
| Leaking `work_tmp/`, app-specific names, or host fixtures into `src/` | Engine stops being portable. Put adaptation into the app's config. |
| Inventing a new port before checking the existing triad | The triad exists for a reason. Add a `routing_*` entry or a `file_*` adapter. |
| Skipping the use-case docstring | Future-you can't tell why this file exists. Reject in review. |
| Adding "for safety" error handling at internal seams | Hides the real failure. Trust the contract. |
| Doing a separate "clean up later" pass | Build well now. Flag big cross-cutting refactors as backlog, don't do them inline as a phase (memory: *elegance-is-focus-not-a-phase*). |
| Committing without explicit user ask | Standing rule (memory: *ask-before-committing*). |
| Adding `raise ValueError(...)` that states the problem with no fix | Every error in `src/yaah/` should name what to do next. Rewrite to "<what> — <how to fix>" before moving on. The Y3 baton-resume rewrite is the reference shape. |
| Using `json.loads` to parse agent output in a new transform / validator | Real sonnet/haiku fence their JSON. Use `from yaah.jsonio import extract_json`; the built-in `JsonObjectValidator` already does. |
| Adding a new attacher under `src/yaah/agents/` | ADR-0003 rule: engine ships ZERO attachers. Add it to `docs/cookbook/attachers/` as a non-importable reference, OR keep it in the consumer's transforms.py. Never both. |
| Adding a new entry to `harness/decision_forms.py` for a domain-specific case | The catalog is generic by design (ADR-0002). Use the `json_schema` escape hatch for one-off shapes; don't pollute the catalog. |

## After the change

Verify with the script-style suite (runner is `scripts/run_tests.py` — one process per `tests/test_*.py`, enforces coverage floor):
```bash
python3 scripts/run_tests.py
# single test in isolation: PYTHONPATH=src python3 tests/test_widget.py
```
A failing test should distinguish missing infra (NATS server) from real defect. Update `docs/ROADMAP.md` for follow-ups.

Then run the pre-submission rubric:
```bash
python3 scripts/review_my_pr.py    # deterministic checks 2/3/4
# then read `.claude/skills/yaah-review-my-pr/SKILL.md` for the semantic checks
```
