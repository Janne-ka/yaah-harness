# AGENTS.md

Guidance for AI coding assistants (Codex, Cursor, Copilot, Claude Code, …) working
in this repo. It's portable — the essentials are inline here. **Claude Code** users
also have richer structured skills in [`.claude/skills/`](.claude/skills/)
(`yaah-pipeline-authoring`, `yaah-extending`, `yaah-reviewing`); this file is the
distilled cross-tool version.

## What YAAH is

A generic, domain-free runtime for orchestrating **agentic workers**. The harness
owns routing and control; a worker (including an LLM agent) does one job and is
interchangeable. It runs in-process, over a local bus, or distributed over NATS —
placement is configuration, not code. The core has **zero runtime dependencies**;
every third-party library is an opt-in adapter.

## Authoring a pipeline? Start here

If a user asks you to author or modify a YAAH pipeline, the workflow
is deterministic — DO NOT design from first principles:

1. Read **[docs/archetypes.md](docs/archetypes.md)**. Five shapes;
   almost every pipeline is one of them: `linear`, `branch-with-gate`,
   `fork-fanin`, `instrumented`, `meta-tool`.
2. Match the user's request against the **"Reach for this when…"**
   lines in each archetype. Pick the nearest one.
3. Open the named **Reference example** and copy from there. Adapt
   stage names, prompts, transforms — keep the shape.
4. If the user's idea doesn't fit any archetype, re-read once. Most
   "doesn't fit" cases are the pipeline doing too much; split into
   two simpler pipelines that each match an archetype.

The archetype map exists so you don't have to invent. The examples
are battle-tested in ways a fresh design isn't.

## Get oriented first

- **[docs/archetypes.md](docs/archetypes.md)** — the five pipeline
  shapes + reference examples (read FIRST when authoring).
- **[docs/quickstart.md](docs/quickstart.md)** — run a pipeline in 5 minutes.
- **[docs/tutorial.md](docs/tutorial.md)** — every core concept, progressively.
- **[examples/](examples/)** — runnable: `hello-yaah` (linear),
  `review-pipeline` (branch + human gate), `fork-join`
  (parallel + reduce), `arch-drift` (instrumented),
  `config-flow` (meta-tool).
- **[docs/node-reference.md](docs/node-reference.md)** + **[docs/root-config-reference.md](docs/root-config-reference.md)** — every node type and config key (single source of truth: `docs/module-catalog.md`, generated from the code).
- **[docs/cookbook/](docs/cookbook/)** — non-importable reference
  recipes; copy-paste into your own project.
- **[docs/design.md](docs/design.md)** / **[docs/why-yaah.md](docs/why-yaah.md)** — architecture + rationale.

## Mental model

- **Envelope** — one message shape; a run is one envelope flowing stage → stage.
- **Node** — `invoke(input, config) → output`. Pick a built-in type; rarely write one.
- **Comms** — the harness routes between nodes; workers never address each other.
- A **pipeline** is JSON: `nodes` (id → type + config) + `graph` (stages wired with
  `then` / `branch` / `fork` + `fanin`). A **root config** says how to run it
  (transport, providers, which pipeline + input).

## Authoring a pipeline (the rules that bite)

- **The data-flow contract.** An agent's reply is a STRING in `payload["raw"]`. A
  `json_object` validator only *checks* it; a `transform` with `call:"envelope"`
  (`fn(envelope, config) -> dict`, returned dict spreads onto the payload) is what
  merges it. Every `agent → render`/`branch` edge needs a parse step, or `render`
  fails (`render_unfilled_placeholders`) pointing at the missing parse — it no
  longer ships a literal `{{placeholder}}` at exit 0. (`allow_unfilled: true` opts
  a render out, for intentionally-optional fields.)
- **A human gate must `branch` on `decision`** — a gate with only `then` is a pause,
  not a gate; the human's reject is ignored.
- **Compose, don't invent.** `fork`+`fanin`+`transform` express most things; a new
  node type pays a test file + future drift. (A `subpipeline` node was added and
  retired in 24h for this reason.)
- **Always ship a `.fake.json` overlay** (`_extends` the canonical, swap models to
  `fake:*`) so the pipeline runs offline/CI for free. Verify on it before going real.
- **Generate → validate → repair.** Walk a draft config through
  `yaah.validate.validate_root` / `validate_pipeline` mentally (unknown keys, typed-
  block shapes, enum values, every `then`/`branch`/`fanin` target resolves) before
  handing it over. Don't ship a draft you haven't checked.

## Editing the engine (`src/yaah/`) — invariants, enforced

- **Domain-free.** Nothing in `src/yaah/` may name a stage, tenant field, test
  runner, or anything app-specific. Adaptation lives in the consuming app's config.
- **One class per file**, filename = class, with a top docstring stating **who calls
  it, where, why** (the use case). No docstring → reviewer rejects.
- **Hug-the-world ports.** Extend an existing port (`routing_*` multiplexer + a
  `file_*`/`http_*` adapter) before inventing a new one.
- **Agent isolation.** Each stage is a fresh agent; never feed an agent its own
  critic's output. Counterfactual critics cold-read, never see the author's reasoning.
- **Minimal first.** In-memory before durable, in-process before distributed; no
  premature abstraction. Delete an unused capability the day you notice it.
- **No comments stating WHAT** (names do that) and no error handling for impossible
  internal cases — validate only at boundaries.

## Engine boundary — what `{base_dir}` is, what yaah does NOT own

Two misunderstandings that consumer-side agents recur to. Read this once and
don't propose either as a "fix":

- **`{base_dir}` is agent-tool-only by design, not by oversight.** The
  substitution lives in `build/builders.py::_build_agent::_expand`, applied
  only to `tools[*].usage` and `allowed_tools`. Reason in the file: tool
  scripts ship beside the config, but a repo-bound agent runs with `cwd` in a
  task worktree — the path must be **absolute at runtime yet relocatable in
  the source file**. Other node configs don't have this tension: paths in
  `state.dir`, prompt-source `dir`, file-data `dir`, etc. are resolved via
  `_rel(base, …)` at construction time in `runtime_factories.py`, so they're
  already relative-to-config without any `{…}` syntax. Generalizing
  `{base_dir}` to all configs would add engine surface for no problem the
  engine has.
- **There is no `run_dir`, `task_dir`, or "current run's directory"
  concept in the engine — and there won't be one.** `state.dir` is a string
  passed to the `FileStore` adapter (`runtime_factories.py:289`); the engine
  has no opinion about its parent, its siblings, or how a consumer nests
  per-task artifacts under it. Proposals to add `{run_dir}` (defined as
  "`state.dir`'s parent" or similar) are domain leaks: they bake an
  application's tree-shape convention into the engine. The consumer owns its
  filesystem layout — period. If a consumer wants templated paths in its own
  transforms, it implements the templating in its own transform code.

If you find yourself wanting either, the right move is in **the consumer's
config or transform code**, not in `src/yaah/`.

## Security — the trust boundary is implicit and undefended

- `fn:module:func` in config is **executed** — config is trusted code; a
  payload-derived value reaching `importlib`, a shell, an fs path, or a URL is
  RCE-adjacent. Never let a payload value reach those without sanitizing at the seam.
- An agent's `expose:` allow-list must be only the fields it needs; never expose
  `headers`/`baton`/auth. Tag untrusted text (repo content, model output) so it
  can't be read as instructions.

## Tests

Script-style (not pytest), **Python 3.9 compatible**, one process each:

```bash
python3 scripts/run_tests.py          # the whole suite, offline + deterministic
PYTHONPATH=src python3 tests/test_harness.py   # a single test
```

A test sets up its scenario, asserts, exits 0/non-zero. Add a `tests/test_*.py` for
any new behavior. Don't commit unless explicitly asked.

## Pre-submission self-review

Before opening a PR, review the working-tree diff against YAAH's three values
(**simplicity, elegance, ease of use**) and the cosmology invariants in
[ADR-0001](docs/decisions/0001-three-concepts.md). The full spec — single
source of truth across tools — is
[`docs/contributor/pre-submission-check.md`](docs/contributor/pre-submission-check.md).

First, run the deterministic subset:

```bash
python3 scripts/review_my_pr.py
```

Then perform the **semantic** checks (1, 5, 6, 7, 8, 9) against the diff. The
checks in one screen:

1. **New nouns** — does the diff introduce a new top-level concept, a new file
   under `src/yaah/nodes/`, a new top-level root-config key, or a new public
   class in `core/`/`harness/`/`comms/`? If yes, the PR description must link
   an ADR. Default: reject.
2. **Core purity** (mechanical — script handles it).
3. **Domain leakage** (mechanical — script handles it via the banlist).
4. **File shape** (mechanical — script handles it).
5. **New node types** — any new file under `src/yaah/nodes/` requires a
   concrete paragraph in the PR description answering *"why can't
   `fork`+`fanin`+`transform` express this?"*. Vague answers fail.
6. **Line ratio** — surface `+added / -removed` as `INFO`; PRs with net
   negative line-count that preserve capability are the highest-credit form
   of contribution.
7. **Three lenses** — the PR description answers, with specifics, *simpler?
   more elegant? easier to use?* Hand-waves fail. The **elegance lens is a
   human judgment, not a check** — the AI does not score it. Instead, prepare
   an **evidence pack** for the maintainer (cite `file:line` per bullet):
   existing patterns used; patterns paralleled but not matched; new shapes
   introduced; naming deviations; density notes (padding or thinness);
   surprises in reading order. Emit `CHECK 7b — elegance evidence : INFO`
   and append the pack after the VERDICT line. CHECK 7b never blocks. Full
   spec in
   [`docs/contributor/pre-submission-check.md`](docs/contributor/pre-submission-check.md).
8. **The data-flow footgun** — for every pipeline JSON in the diff
   (`examples/`, `tests/`), every edge `agent → render` or `agent → branch`
   has a `transform` with `call: "envelope"` in between. Without it the render
   fails (`render_unfilled_placeholders`) — formerly silent `{{placeholder}}` at
   exit 0.
9. **Tests for behavior** — behavior change in `src/yaah/` requires a
   `tests/test_*.py` change. Refactor-only PRs get a pass here.

Emit a report in this shape (the PR template asks to paste it in):

```
YAAH pre-submission review

Lines: +<added> / -<removed>   net <delta>

CHECK 1  — new nouns           : <PASS | WARN | FAIL>  <one line>
CHECK 2  — core purity         : <PASS | WARN | FAIL>  <one line>
CHECK 3  — domain leakage      : <PASS | WARN | FAIL>  <one line>
CHECK 4  — file shape          : <PASS | WARN | FAIL>  <one line>
CHECK 5  — new node types      : <PASS | WARN | FAIL | N/A>  <one line>
CHECK 6  — line ratio          : INFO  <one line>
CHECK 7a — simpler?            : <PASS | WARN | FAIL>  <one line>
CHECK 7b — elegance evidence   : INFO  (human judgment; see evidence pack)
CHECK 7c — easier to use?      : <PASS | WARN | FAIL>  <one line>
CHECK 8  — data-flow footgun   : <PASS | WARN | FAIL | N/A>  <one line>
CHECK 9  — tests for behavior  : <PASS | WARN | FAIL | N/A>  <one line>

VERDICT: <ready | needs revision | blocked>

Evidence pack (for the maintainer's elegance call):
  - <file:line> <category>: <what you saw>
  - ...
```

For each `WARN`/`FAIL`, append a paragraph: quote the file, suggest the
composition or pattern that would have worked, or point at the existing
mechanism the change should follow.

**Be direct.** A blunt "this adds a fourth concept, here's why it won't be
accepted" is more respectful than warm vague concern. Claude Code users get a
richer wrapper at `.claude/skills/yaah-review-my-pr/`.

## Deeper guides

Claude Code: the [`.claude/skills/yaah-*`](.claude/skills/) skills carry the full
authoring Q&A, the extend-a-node recipe, and the review cluster method. Other tools:
this file plus the docs above are the source of truth.
