# Contributing to YAAH

Thank you for considering a contribution. This document is the contract: read
it once, and PRs go faster for everyone.

## The values, stated up front

YAAH optimizes for three things, in this order:

1. **Simplicity** — fewer concepts, fewer modules, fewer config keys. A
   reviewer's first question is *"can this be expressed with what already
   exists?"* If yes, the PR is rewritten.
2. **Elegance — reuse over reinvention.** The change reuses what already exists:
   existing patterns first (the `routing_*` multiplexer, the `source:key` and
   `provider:model` shapes), then existing code, and it **extends a port rather
   than inventing a concept**. It reads like it belongs — naming and file shape
   follow the surrounding code, so a reader can't tell which PRs were the
   maintainer's. The test: *can this extend something instead of adding something?*
3. **Ease of use** — the change does not make the next reader's job harder. A
   new node type that requires reading three files to understand fails this
   lens, even if it's small. *"Could I explain this to someone in one
   sentence?"* is the test.

A change that improves correctness but hurts simplicity is a hard sell. A
change that adds a feature at the cost of elegance is usually rejected. **The
cheapest, fastest, kindest contribution to YAAH is the one that removes code.**

## Before you open a PR

### 1. Open an issue first (for non-trivial changes)

State the problem before proposing the solution. Drive-by PRs that touch
`core/`, `harness/`, or `comms/` without a prior discussion are usually closed.

### 2. Run the AI-assisted pre-submission check

YAAH ships a portable review prompt. Run it on your own diff before opening a
PR — it catches what reviewers would catch, in your own editor, with reasons.

- **Claude Code:** invoke `/yaah-review-my-pr`.
- **Cursor / Codex / Copilot / Aider / any tool:** point it at the
  "Pre-submission self-review" section of [`AGENTS.md`](AGENTS.md).
- **No AI tool?** Run `python3 scripts/review_my_pr.py` for the deterministic
  subset (banlist grep, core-import check, file-shape check).

The check verifies, among other things:

- Does the diff introduce a **new noun** (concept, node type, config key)?
- Does the diff make `core/`/`harness/`/`comms/` depend on anything new?
- Does the diff add **domain-specific words** to `src/yaah/`?
- Are new files **one class per file**, with a "who calls this, where, why"
  docstring?
- For new node types: is there a paragraph explaining why composition was
  rejected?
- For new examples: does every `agent → render`/`branch` edge have a parse
  step between?
- Does the PR description honestly address the three lenses above?

### 3. Read the cosmology

[ADR-0001](docs/decisions/0001-three-concepts.md) codifies the architectural
invariants YAAH protects. PRs that violate them either require their own ADR
(see [GOVERNANCE.md](GOVERNANCE.md)) or are closed.

## What we don't accept

A short, concrete list — most "no" conversations are about these:

- **A fourth top-level concept** next to Envelope, Node, and Comms.
- **A new built-in node type** when `fork` + `fanin` + `transform` could
  express the same thing. (We added and retired `subpipeline` in 24 hours for
  exactly this reason.)
- **Runtime dependencies in `src/yaah/core,harness,comms`.** The zero-dep core
  is load-bearing for the project's promise. Adapters in
  `src/yaah/adapters/` are where the world is allowed in.
- **Domain-specific terms in `src/yaah/`** — stage names, tenant fields, app
  vocabulary. That knowledge lives in the consuming app's config.
- **"Configurable" as a debate resolution.** Two reasonable defaults beats one
  config flag.
- **Backwards-compat shims** for code that hasn't shipped a stable release.
  We're pre-1.0; just change the code.
- **Comments stating *what* the code does** (names handle that) and error
  handling for impossible internal cases.

## What we love

- **Removing a concept.** A PR with negative line-count that preserves
  capability is the highest form of contribution. We name these in release
  notes.
- **Replacing two adjacent ways with one.** Consolidation.
- **A test that demonstrates a subtle bug we didn't know we had.**
- **Documentation that lets a new reader skip a doc we currently require.**

## How a PR is reviewed

The reviewer's three questions, in order:

1. **Does this honor the three values?** (Simplicity, elegance, ease of use.)
2. **Does this respect the cosmology?** (See [ADR-0001](docs/decisions/0001-three-concepts.md).)
3. **Is the code correct, tested, and minimal?**

Most PRs that pass (1) and (2) merge quickly. Most PRs that fail (1) or (2) are
closed with a pointer to the composition or refactor that would have worked.

## Tests

Script-style (not pytest), Python 3.9 compatible, one process each:

```bash
python3 scripts/run_tests.py          # the whole suite, offline + deterministic
PYTHONPATH=src python3 tests/test_harness.py   # a single test
```

Any new behavior gets a `tests/test_*.py`. `scripts/run_tests.py` enforces a
coverage floor: with `coverage` installed it runs the suite under it and exits
nonzero below `fail_under` in `pyproject.toml` (75%). CI installs coverage, so
the floor gates every PR — check it locally with `pip install 'coverage[toml]'`.

## Where to put extensions you build on YAAH

If you've built something *on top of* YAAH — a custom node, an adapter, an
application pipeline — it usually belongs in **its own repository**, not this
one. See [`ECOSYSTEM.md`](ECOSYSTEM.md) for the tiering (core / official
adapter / community package / application), naming conventions, and how to
list your project in the registry.

The short version: the main repo stays small on purpose. Your code stays
yours; we link to it.

## Code of conduct

Be kind, be specific, assume good faith. We will write a longer document if we
ever need to; until then, this sentence is the policy.

## Questions

Open a GitHub Discussion (or an issue if Discussions isn't enabled yet). For
design-shaped questions, expect to be asked to draft an ADR before code is
written.
