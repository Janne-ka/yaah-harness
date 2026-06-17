# Pre-submission self-review

**Source of truth.** The Claude Code skill (`.claude/skills/yaah-review-my-pr/`),
the AGENTS.md section ("Pre-submission self-review"), and the CLI fallback
(`scripts/review_my_pr.py`) all reference this file. When they drift, this file
wins.

A contributor runs this on their own diff before opening a PR. The goal is not
to gatekeep — it's to surface what reviewers would surface anyway, in the
contributor's own editor, with reasons, while the change is still cheap to
rework.

## The frame

Three lenses, in this order:

1. **Simplicity** — fewer concepts, fewer modules, fewer config keys.
2. **Elegance** — the change reads like it belongs.
3. **Ease of use** — the next reader's job does not get harder.

The first three checks below test the lenses directly. The rest test the
specific cosmology invariants from [ADR-0001](../decisions/0001-three-concepts.md).

---

## The checks

### 1. New nouns

Walk the diff. Does it introduce any of:

- A new top-level concept (something on the level of Envelope, Node, Comms)?
- A new built-in node type under `src/yaah/nodes/`?
- A new top-level root-config key?
- A new public class in `src/yaah/core/`, `src/yaah/harness/`, or `src/yaah/comms/`?

If yes, the PR description must either link an ADR or explain why the new
noun pays for itself. **Default verdict: reject.**

### 2. Core purity

Search the diff for new `import` or `from ... import` lines in files under
`src/yaah/core/`, `src/yaah/harness/`, or `src/yaah/comms/`. The only
acceptable imports there are:

- Python standard library
- Other modules under `yaah.core`, `yaah.harness`, `yaah.comms`
- `yaah.envelope`, `yaah.outcomes`, `yaah.nodeconfig` and other engine types

A new third-party import in the core is a hard reject (no third-party imports
in those packages at all).

### 3. Domain leakage

Grep the changed lines under `src/yaah/` for words in
`docs/contributor/banlist.txt`. Domain-specific terms (stage names, app
vocabulary, tenant fields, test-runner names) belong in the consuming app's
config, not in the engine.

If a banlist word appears in `src/yaah/`, either remove it or extend the
banlist with the term and reframe the abstraction generically.

### 4. File shape (new files)

For each new `.py` file under `src/yaah/`:

- **One class per file.** Multiple top-level classes is a smell — usually a
  missed extraction.
- **Class name = filename** (snake-case ↔ PascalCase): `fork_coordinator.py`
  defines `ForkCoordinator`.
- **Top docstring** answers *"who calls this, where, why"* — the use case in
  prose, not a restatement of the class name.

### 5. New node types

If the diff adds a file under `src/yaah/nodes/`, the PR description must
contain an answer to this question:

> Why can't `fork` + `fanin` + `transform` (and the existing node types) express
> this?

A vague answer ("it would be cleaner") is a reject. A concrete answer (with the
composition attempted and its specific failure) is the bar. See the
`subpipeline` retirement (commit `b744de7`) for the reflex.

### 6. Lines added vs removed

Surface the ratio. A PR adding 800 lines and removing 20 is not automatically
wrong, but it requires a paragraph of justification. PRs with negative net
line-count that preserve capability are the highest-credit form of
contribution.

### 7. The three lenses — answered honestly?

Read the PR description. The reviewer applies each lens against the diff and
the PR description together. Each lens has its own sub-rubric; elegance has
six checkable heuristics plus a meta-test.

#### 7a. Simpler?

What concept, module, or config key is now smaller or absent? If nothing is
smaller, the lens is at best `INFO`, not `PASS`. Vague *"yes, slightly"* fails.

#### 7b. More elegant?

**A single criterion, judged by a human.** Does the change look like it
belongs in the codebase? A maintainer reading the merged code six months
from now should not be able to tell which lines were the contributor's.

This is a taste-call, not a rubric. The AI assistant does **not** score it.
It prepares an **evidence pack** the maintainer reads to make the call. In
the report, CHECK 7b emits `INFO  (see evidence pack)`; the pack is appended
below the verdict.

##### The evidence pack

For each applicable item, one bullet citing `file:line`. Omit empty
categories — do not state "no new shapes introduced" if none were.

- **Existing patterns used.** Which named patterns the diff conforms to
  (`fork`+`fanin`+`transform`, `routing_*` multiplexer, `source:key`,
  `provider:model`, `*_coordinator`, `file_*`/`http_*` adapter triads, etc.).
- **Patterns paralleled but not matched.** Places where the diff resembles
  an existing pattern but breaks its shape (a new orchestrator without the
  `_coordinator` suffix; a new adapter that bypasses the `routing_*`
  multiplexer pattern).
- **New shapes introduced.** Anything that has no precedent in the codebase.
- **Naming deviations.** Places where naming breaks the surrounding
  conventions.
- **Density notes.** Padding (commented-out code, restating-name comments,
  validation for impossible internal cases) or thinness (bare `"failed"`
  errors with no context).
- **Surprises in reading order.** Files where helpers or mechanics appear
  before the thesis the file is about.

The pack is structured prose for human reading. **No PASS/WARN/FAIL on the
elegance lens itself — that's the maintainer's job.** The AI's role is to
surface the evidence the taste-call rests on, not to make the call.

#### 7c. Easier to use?

Could you explain the change to a new reader in one sentence? Does it remove
a doc the reader would otherwise have needed, or shorten an existing one? If
not, the lens is at best `INFO`.

---

The PR description should address all three lenses with specifics. Hand-waves
fail. The contributor should be able to point at the specific reader, the
specific config, or the specific concept that gets smaller.

### 8. The data-flow footgun

For each pipeline JSON file changed or added under `examples/` or `tests/`:

- For every edge `agent → render` or `agent → branch`, is there a `transform`
  with `call: "envelope"` in between?
- If not, the render fails with `render_unfilled_placeholders` pointing at the
  missing parse (it used to ship literal `{{placeholder}}` strings at exit 0).
  This is the single bug that bites every new contributor — still add the parse
  transform. See [`docs/quickstart.md`](../quickstart.md) §3 and
  [`docs/tutorial.md`](../tutorial.md) Part 1.

### 9. Tests for new behavior

If the diff changes behavior in `src/yaah/`, there must be a corresponding
change under `tests/test_*.py`. New behavior with no test is a reject.
Refactor-only PRs (no behavior change) get a pass here.

---

## The report

After running the checks, the AI assistant (or the script, for the
deterministic subset) emits:

```
YAAH pre-submission review

Lines: +<added> / -<removed>   net <delta>

CHECK 1  — new nouns           : PASS | WARN | FAIL  (one-line reason)
CHECK 2  — core purity         : PASS | WARN | FAIL  (...)
CHECK 3  — domain leakage      : PASS | WARN | FAIL  (...)
CHECK 4  — file shape          : PASS | WARN | FAIL  (...)
CHECK 5  — new node types      : PASS | WARN | FAIL | N/A
CHECK 6  — line ratio          : INFO  (no verdict, just the numbers)
CHECK 7a — simpler?            : PASS | WARN | FAIL  (...)
CHECK 7b — elegance evidence   : INFO  (human judgment; see evidence pack)
CHECK 7c — easier to use?      : PASS | WARN | FAIL  (...)
CHECK 8  — data-flow footgun   : PASS | WARN | FAIL | N/A
CHECK 9  — tests for behavior  : PASS | WARN | FAIL | N/A

VERDICT: ready | needs revision | blocked

Evidence pack (for the maintainer's elegance call):
  - <file:line> existing pattern used: ...
  - <file:line> pattern paralleled but not matched: ...
  - <file:line> new shape introduced: ...
  - (etc. — omit categories with no entries)

If "needs revision" or "blocked": one paragraph for each failing check
explaining what to change. CHECK 7b never causes "blocked" — elegance is the
maintainer's call, applied at human review, not by the AI.
```

The contributor pastes the summary block into the PR description under
"Pre-submission check" (the PR template asks for it).

## What the AI is NOT asked to do

- Judge code correctness — that's the reviewer.
- Run tests — the contributor runs `scripts/run_tests.py`.
- Approve the PR — only humans do that.
- Be polite for its own sake. A direct "this adds a fourth concept, here's why
  it won't be accepted" is more respectful than warm vague concern.

## Updating the checks

The checks evolve as the project discovers new ways contributors trip. To add
or change a check:

1. Edit this file.
2. Update the Claude skill, the AGENTS.md section, and the script to match.
3. The CI check `check_pr_skill_sources_in_sync.py` fails if they drift.

Adding a check is an ADR-worthy decision when it changes what kinds of
contribution are accepted. Removing a check rarely needs an ADR — fewer rules
is the default direction.
