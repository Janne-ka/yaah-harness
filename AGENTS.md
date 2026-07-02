# AGENTS.md

Guidance for AI coding assistants (Codex, Cursor, Copilot, Claude Code, …) working
in this repo. It's portable — the essentials are inline here. **Claude Code** users
also have richer structured skills in [`.claude/skills/`](.claude/skills/)
(`yaah-pipeline-authoring`, `yaah-extending`, `yaah-driving`, `yaah-reviewing`, `yaah-review-my-pr`); this file is the
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
- **[docs/why-data-not-code.md](docs/why-data-not-code.md)** — the one big idea
  (wiring is data, work is code) in one short read; start here for the *why*.
- **[docs/design.md](docs/design.md)** / **[docs/why-yaah.md](docs/why-yaah.md)** — architecture + rationale.

## Mental model

- **Envelope** — one message shape; a run is one envelope flowing stage → stage.
  Its `correlation_id` names the whole run; the trace layer keys it as the
  short `corr` (same value, two spellings — `corr` is the JSONL span key).
- **Node** — `invoke(input, config) → output`. Pick a built-in type; rarely write one.
- **Comms** — the harness routes between nodes; workers never address each other.
- A **pipeline** is JSON: `nodes` (id → type + config) + `graph` (stages wired with
  `then` / `branch` / `fork` + `fanin`). A **root config** says how to run it
  (transport, providers, which pipeline + input).

## Authoring a pipeline (the rules that bite)

- **Agent output: parse-by-default** (ADR-0004). An `agent` node returns its
  model text in `payload["raw"]` AND auto-merges the parsed JSON keys onto
  the payload (`extract_json`-tolerant of markdown fences). So
  `{"summary": "..."}` from the model becomes `payload["summary"]` directly
  — downstream `render` / `branch` find the keys they need without an
  intermediate `transform`. On parse failure the agent emits a failed
  verdict that the harness's retry+feedback loop catches the same way a
  `json_object` validator would. Opt out with `"parse": false` on the
  agent node for streaming/raw-only cases; the load-time graph linter
  will then require a `transform` between the agent and any
  render/branch.
- **A human gate must `branch` on `decision`** — a gate with only `then` is a pause,
  not a gate; the human's reject is ignored.
- **`fn:` targets resolve relative to the config's directory** — keep
  `transforms.py` next to the config and it just resolves; for shared/production
  code, package it (`pip install -e .`) and use a dotted path. Full note in
  [docs/node-reference.md](docs/node-reference.md#transform--call-a-functionnodeurl).
- **Compose, don't invent.** `fork`+`fanin`+`transform` express most things; a new
  node type pays a test file + future drift. (A `subpipeline` node was added and
  retired in 24h for this reason.)
- **Always ship a `.fake.json` overlay** (`_extends` the canonical, swap models to
  `fake:*`) so the pipeline runs offline/CI for free. Verify on it before going real.
- **Generate → validate → repair.** Walk a draft config through
  `yaah.validate.validate_root` / `validate_pipeline` mentally (unknown keys, typed-
  block shapes, enum values, every `then`/`branch`/`fanin` target resolves) before
  handing it over. Don't ship a draft you haven't checked.

## Editing the engine (`src/yaah/`) — invariants

Two tiers: **enforced at LOAD time** by `validate.py` / `build/`, and
**convention** maintained by author discipline + the pre-submission
rubric (`docs/contributor/pre-submission-check.md`). Be honest about
which is which.

**Load-enforced (the runtime rejects violations):**

- **Domain-free top-level keys** — `validate.py` checks every root key
  against `_ROOT_KEYS` with did-you-mean.
- **Pipeline shape** — every `then`/`branch`/`fork`/`fanin` target
  resolves; every `node`/`validators[*]` reference resolves; unknown
  stage / node keys rejected; data-flow contract (agent → render /
  branch needs a parse transform between) checked.
- **Trace + transport + state enums** — `validate.py` covers these.

**Convention (author discipline + pre-submission rubric checks):**

- **Declare your port.** Every shipped impl names its port in the class
  header — `class MemoryBackend(StoreBackend, Scannable, CompareAndSet)`,
  `class RenderNode(Node)`, `class FakeProvider(ApiProvider)`. The ports are
  `@runtime_checkable` Protocols with `@abstractmethod`, so a DECLARED
  subclass missing a method fails at instantiation, and mypy checks the
  signatures. Structural (non-declaring) impls still work — external
  extenders and test doubles aren't forced to import yaah — but shipped
  code declares. Caveat: Protocol *attributes* (e.g. `Tracer.captures`)
  are checked by mypy only, not at instantiation. Frozen by the
  `__mro__` port tests (test_store/test_nodes/test_bus/test_trace).
- **Domain-free engine prose.** Nothing in `src/yaah/` may *name* a
  stage, tenant field, test runner, or anything app-specific.
  Caught by `scripts/review_my_pr.py`'s grep checks, not by runtime.
- **One class per file**, filename = class, with a top docstring
  stating **who calls it, where, why**. Caught by code review.
- **Hug-the-world ports.** Extend an existing port (`routing_*`
  multiplexer + a `file_*`/`http_*` adapter) before inventing one.
  Caught by review.
- **Agent isolation.** Each stage is a fresh agent; never feed an
  agent its own critic's output. Counterfactual critics cold-read,
  never see the author's reasoning. **Not runtime-checked** — author
  discipline + the pre-submission rubric's "agent isolation" item.
- **Minimal first.** In-memory before durable, in-process before
  distributed; no premature abstraction. Delete an unused capability
  the day you notice it. Cultural rule; review catches deviations.
- **No comments stating WHAT** (names do that) and no error handling
  for impossible internal cases — validate only at boundaries.
  Cultural rule; review catches.

The honest framing matters: load-enforced rules are runtime-correct
even when the author misses them; convention rules survive only as
long as the author/reviewer pair holds. Don't claim convention is
enforcement — readers spot it and the credibility cost compounds.

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
  passed to the `FileBackend` adapter (`runtime_factories.py:308`); the engine
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
8. **The data-flow footgun** — agents are parse-by-default (ADR-0004), so
   the common shape `agent → render`/`branch` works without a parse step.
   The pre-submission check fires only when `"parse": false` is set on an
   agent flowing into a render/branch with no transform between; `validate.py`
   catches it at LOAD time with the actionable message.
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
