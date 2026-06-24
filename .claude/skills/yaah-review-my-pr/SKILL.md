---
name: yaah-review-my-pr
description: Use before opening a YAAH PR. Reviews the working-tree diff against YAAH's three values (simplicity, elegance, ease of use) and the cosmology invariants in ADR-0001 — catches what human reviewers would catch, with reasons, while the change is still cheap to rework. Not for code-correctness review (run the test suite); not for design discussion (open an issue first).
---

# Review my PR

**Standing rule:** report the verdict; never modify the diff and never commit.

## When to use

- Contributor invokes `/yaah-review-my-pr` before opening a PR.
- Author wants a quick reality check on a work-in-progress diff.
- Not for: code correctness (run `python3 scripts/run_tests.py`); architectural
  discussion that should be an issue or an ADR first.

## What this skill is

A wrapper around the canonical check spec at
[`docs/contributor/pre-submission-check.md`](../../../docs/contributor/pre-submission-check.md).
That file is the source of truth. This skill is *how Claude Code runs it*.

The check has nine items. The first three test the three values directly:
**simplicity, elegance, ease of use**. The rest test the cosmology invariants
from [ADR-0001](../../../docs/decisions/0001-three-concepts.md).

## Method

Run the deterministic subset first (cheap, catches the obvious stuff), then
the semantic checks (read the diff carefully, judge the lenses).

### 1. Ground the diff

```bash
git diff --stat                          # what changed
git diff HEAD                            # the actual diff
git status                               # any untracked files matter too
```

If the contributor is on a feature branch:

```bash
git diff main...HEAD                     # full PR-shaped diff vs main
```

Note the **line ratio** (added vs removed, net delta) — surface it in the
report.

### 2. Run the deterministic checks

```bash
python3 scripts/review_my_pr.py
```

This handles checks **2 (core purity)**, **3 (domain leakage)**, and **4
(file shape)** mechanically — they're grep-shaped and reliable. Capture its
output verbatim and quote it in your final report; do not re-do those checks
by hand.

### 3. Run the semantic checks

For each remaining check, read the diff with the rule in mind:

| Check | What you're looking for |
|---|---|
| 1 — new nouns | New top-level class in `core,harness,comms`; new file under `nodes/`; new top-level root-config key. If yes: is there an ADR linked in the PR description? |
| 5 — new node types | Any new file under `src/yaah/nodes/` requires a paragraph in the PR description answering *"why can't `fork`+`fanin`+`transform` express this?"*. Vague answers fail. |
| 7 — three lenses | Read the PR description. Does it answer all of: *simpler? more elegant? easier to use?* with concrete pointers? Hand-waves fail. |
| 8 — data-flow footgun | Mostly load-time enforced now (ADR-0004 parse-by-default + B1.1 graph linter). Only flag if the diff sets `"parse": false` on an agent without adding an explicit `transform` downstream — and `validate.py` already rejects that pattern at load. PASS by default unless the diff bypasses both checks somehow. |
| 9 — tests for behavior | Behavior change in `src/yaah/` ⇒ corresponding `tests/test_*.py` change. Refactor-only PRs get a pass. |
| 10 — form consistency | If the diff adds an example pipeline, does it match one of the five archetypes in [`docs/archetypes.md`](../../../docs/archetypes.md)? If it adds a node type, is the corresponding row added to [`docs/node-reference.md`](../../../docs/node-reference.md) AND [`docs/shape-grammar.md`](../../../docs/shape-grammar.md)? If it adds a root-config key, is `docs/root-config-reference.md` updated? A code change with stale doc surfaces is WARN minimum (not just a nit). |

### 4. Judge the three lenses (this is the part only you can do)

**Simpler?** Concretely — what concept, module, or config key is now smaller
or absent? If nothing is smaller, this lens is at best `INFO`, not `PASS`.

**More elegant?** **Do not score this lens.** Elegance is a taste-call —
*does this look like it belongs?* — and only the maintainer answers it. Your
job is to **prepare** the maintainer by surfacing the evidence the call rests
on.

Produce an **evidence pack**: structured prose, no verdict. For each
applicable category, one bullet citing `file:line`. Omit empty categories.

| Category | What to surface |
|---|---|
| Existing patterns used | `fork`+`fanin`+`transform`, `routing_*` multiplexer, `source:key`, `provider:model`, `*_coordinator`, `file_*`/`http_*` adapter triads, etc. Name them. |
| Patterns paralleled but not matched | New orchestrator without the `_coordinator` suffix; new adapter that bypasses the `routing_*` multiplexer; anything that resembles an existing shape but breaks it. |
| New shapes introduced | Anything without precedent in the codebase. |
| Naming deviations | Places where naming breaks surrounding conventions. |
| Density notes | Padding (commented-out code, restating-name comments, validation for impossible internal cases) or thinness (bare `"failed"` errors with no context). |
| Surprises in reading order | Files where mechanics appear before the thesis. |

In the report, emit `CHECK 7b — elegance evidence : INFO  (see evidence
pack)` and append the pack after the VERDICT line.

**Do not invent items not in the diff.** No category should be stated unless
it has actual bullets. The maintainer reads the pack and makes the call —
that's the design, not a failure of the design.

**Easier to use?** Could you explain the change to a new reader in one
sentence? Does it remove a doc the reader would otherwise have needed, or
shorten an existing one?

### 5. Emit the report

Use exactly this shape (the PR template expects to paste it in):

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
CHECK 10 — form consistency    : <PASS | WARN | FAIL | N/A>  <one line>

VERDICT: <ready | needs revision | blocked>

Evidence pack (for the maintainer's elegance call):
  - <file:line> <category>: <what you saw>
  - ...
```

CHECK 7b never blocks. The maintainer applies the elegance call at human
review, using the pack you prepared.

For each `WARN` or `FAIL`, append a paragraph explaining what to change.
Be concrete: quote the file and line, suggest the composition that would have
worked, or point at the existing pattern the change should follow.

**Be direct.** A blunt "this adds a fourth concept, here's why it won't be
accepted" is more respectful than warm vague concern. Match the voice of
[`AGENTS.md`](../../../AGENTS.md) and [`CONTRIBUTING.md`](../../../CONTRIBUTING.md).

## Verdict thresholds

- **ready** — all checks PASS or N/A (line-ratio INFO is fine).
- **needs revision** — one or more WARN, no FAIL. Author can address inline.
- **blocked** — any FAIL. The PR should not be opened in its current shape;
  rework, or open an ADR first.

## What this skill must NOT do

- **Do not edit the diff.** Reviewers report; authors rewrite.
- **Do not run the test suite.** That's the contributor's job; the values
  rubric is orthogonal to correctness.
- **Do not call this an approval.** Only human reviewers approve PRs.
- **Do not skip the deterministic script** because "I can see what it would
  say." The script's output is the receipt — the report quotes it.

## Common mistakes

| Mistake | Reality |
|---|---|
| Marking CHECK 1 PASS because no class was renamed | A new file under `src/yaah/nodes/` is also a new noun. So is a new top-level config key. |
| Treating "the new config key is optional" as PASS | An optional key is still a key. Budget §5 of ADR-0001 applies regardless. |
| Marking CHECK 8 N/A because the diff "looks like Python" | Look for `*.json` under `examples/` and `tests/` — those are pipelines, not data. |
| Inferring CHECK 9 PASS from "looks like the code is correct" | Behavior change without a test fails CHECK 9 regardless of correctness. |
| Soft language to avoid hurting feelings | Vague "this might be better as…" loses the contributor a review cycle. Be direct, cite the rule, suggest the fix. |

## Related

- [`docs/contributor/pre-submission-check.md`](../../../docs/contributor/pre-submission-check.md)
  — the spec this skill implements.
- [`docs/decisions/0001-three-concepts.md`](../../../docs/decisions/0001-three-concepts.md)
  — the cosmology being protected.
- [`CONTRIBUTING.md`](../../../CONTRIBUTING.md) — the values rubric.
- [`AGENTS.md`](../../../AGENTS.md) §"Pre-submission self-review" — the same
  prompt for non-Claude-Code tools.
- [`yaah-reviewing`](../yaah-reviewing/SKILL.md) — the *maintainer*-side
  review skill (six-cluster fan-out for milestone audits). Different scope:
  that's for auditing the whole codebase; this is for a contributor's own
  diff.
