---
name: yaah-extending
description: Use when writing or modifying code under yaah/src/ or yaah/tests/. Not for a separate clean-up phase (do elegance inline); not for authoring a new pipeline config from scratch (use yaah-pipeline-authoring or an app-specific authoring skill).
---

# Extending YAAH + the example app

**Standing rule:** never commit unless explicitly asked.

## Overview

YAAH's value is its **discipline**: a domain-free engine, hug-the-world ports, file-based state, agent isolation, scripted gates. Most "improvements" that add a concept *delete* this value. Default to **composing existing primitives** over inventing new ones, and **delete** when an unused capability is found — see commit `b744de7` for the `SubpipelineNode` retirement: 10-file change removing a node `fanout`+`fanin` already composed.

## When to Use

- Adding a node type, port, adapter, builder, transform
- Editing pipeline JSON in a consuming app
- Adding tests (script-style, not pytest)
- Doing inline elegance/simplification work as part of a larger change
- **Not for:** queuing elegance work as a separate "cleanup phase" later (memory: *elegance-is-focus-not-a-phase*). Inline cleanup IS encouraged — the rule is "don't defer it as a phase," not "don't do it."

## The invariants you MUST preserve

- **Domain-free engine.** Nothing in `yaah/src/` may name a stage, tenant field, test runner, or anything else specific to a host project. If you're tempted to add an `if stage.name == "code"`, stop.
- **One class per file.** Filename matches the class. Top-of-file docstring states **who calls it, where, and why** (use case). Skip the docstring → reviewer rejects.
- **Hug-the-world ports.** Extend an existing port before inventing a new one. The pattern is *port + `routing_*` multiplexer + concrete `file_*`/`http_*` adapter*. Match the existing triad.
- **Trust boundary is implicit.** `fn:module:func` in config is RCE; payload-derived paths reach `shutil.rmtree`. Never let a payload value reach a shell command, FS path, URL, or `importlib`. If you must, sanitize at the seam and document why.
- **Agent isolation.** Each stage = fresh agent, named `carry` keys only. Never feed an agent its own critic's output.
- **Hard human gates branch on `decision`.** A `human_gate` with only `then` is a pause, not a gate.
- **Minimal first.** In-memory before durable, in-process before distributed, no premature abstraction (memory: *minimal-first-extend-on-need*).

## Decision: new concept vs compose existing?

Three checks. **All must answer "no" to add a new concept:**

1. Can `fanout` + `fanin` + `transform` + existing nodes already express it? (see `ab-fork.json` for asymmetric A/B without subpipeline)
2. Does the new concept add only *organizational* value (file-level encapsulation, naming) rather than computational?
3. Does it pay costs the existing primitives don't (new node type, new test file, new restriction, depth guard)?

If any "yes" — **compose, don't add**. If you already added it, **delete it** (and its tests, demos, references) the day you notice. Note the retirement in `yaah/docs/TODO.md`.

## Style

- **No comments unless the WHY is non-obvious.** Don't explain WHAT — the names do that. Don't reference the current task/PR/issue — that belongs in the commit message.
- **No error handling for impossible cases.** Trust internal contracts; validate only at system boundaries.
- **No backwards-compat shims** for code you're free to change.
- **Tests are script-style:** `"""Run: cd yaah && PYTHONPATH=src python3 tests/test_x.py"""` at top, `if __name__ == "__main__": asyncio.run(main())` at bottom. Python **3.9 compatible** (the lib is consumed by old envs).
- **Backlog goes in `yaah/docs/TODO.md`**, not new doc files (memory: *todo-location*).

## Add-a-node-type recipe

```python
# yaah/src/yaah/nodes/widget_node.py
"""WidgetNode — <one line: what concept this adds>.

Used by: <which builder / which pipeline JSON role>.
Where: <when in the graph — pre-validator, terminal, fan-out branch>.
Why: <the use case that motivated it; what existing primitive failed to compose>.

Targets Python 3.9+.
"""
class WidgetNode:
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope: ...
```

Then: register in `nodes/__init__.py`, builder in `build/builders.py` (`_build_widget` + `r.register("widget", _build_widget)`), test in `tests/test_widget.py`, row in `yaah/docs/architecture.md` node-types table. **All in one change.** Retiring a node uses the same checklist in reverse — see `b744de7` (SubpipelineNode retirement, 10 files, one commit) for the worked example.

## Common mistakes

| Mistake | Reality |
|---|---|
| Adding a new node concept when `fanout`/`fanin`/`transform` compose it | You're paying a node type, a test file, and future drift for organizational gain. Compose. |
| Leaking `work_tmp/`, `the example app`, `test_bank.py` into `yaah/src/` | Engine stops being portable. Put adaptation into the app's config. |
| Inventing a new port before checking the existing triad | The triad exists for a reason. Add a `routing_*` entry or a `file_*` adapter. |
| Skipping the use-case docstring | Future-you can't tell why this file exists. Reject in review. |
| Adding "for safety" error handling at internal seams | Hides the real failure. Trust the contract. |
| Doing a separate "clean up later" pass | Build well now. Flag big cross-cutting refactors as backlog, don't do them inline as a phase (memory: *elegance-is-focus-not-a-phase*). |
| Committing without explicit user ask | Standing rule (memory: *ask-before-committing*). |

## After the change

Verify with the script-style suite:
```bash
cd yaah; PY="${PY:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
for f in tests/test_*.py; do PYTHONPATH=src "$PY" "$f" >/tmp/o 2>&1 && p=$((p+1)) || { f2=$((f2+1)); fl="$fl $f"; }; done
echo "PASS=$p FAIL=$f2$fl"
```
A failing test should distinguish missing infra (NATS server) from real defect. Update `yaah/docs/TODO.md` for follow-ups.
