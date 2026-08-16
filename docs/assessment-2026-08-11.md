# Assessment — 2026-08-11 — can s_factory be simplified?

Four-axes quality evaluation (elegant / simple / working / extensible, weighted
to elegance + simplicity + maintainability) of the accumulated s_factory delta
since the 2026-08-06 assessment: M38 (runner evidence) + M39 (scope/drift gates)
+ M40 (worktree env-equivalence) + M41 (settle-runner, data-facts, git policy)
+ the engine v0.3.1/v0.3.2 changes. Committed at library `bce4ac2`/`c8fab46`,
yaah `f05f513`/`v0.3.2`. Method: three parallel Opus evaluators (transforms +
graph · new modules + operator surfaces · prompts + docs + philosophy); this is
a design-health lens, NOT a bug hunt — each batch was adversarially bug-reviewed
before commit. Delta size: 39 files, +8353/−336; transforms.py now 6977 lines;
142 graph stages.

## Headline answer: YES — and the levers are specific, low-risk, and already have a template in-repo

s_factory is **correct and philosophy-faithful** (7 of 8 enforced principles held
cleanly; the 8th — DECISIONS-as-canonical-home — has drifted). It is not decaying
in correctness; it is decaying in **navigability**, in three concrete ways, each
of which is a mechanical, behavior-preserving simplification:

1. **transforms.py is a 6977-line, 38%-prose junk drawer of six unrelated domains** — split it at its own seams (the M41 modules already prove the pattern).
2. **The same incidents are narrated verbatim in three-to-four homes** — collapse to one canonical home + pointers.
3. **~8 new overlay host facts are coherent but undiscoverable** — one reference page + extend doctor.

None require a design change. The idioms are good; the problem is that four
feature batches landed *inline* on an already-large file and *at full length* in
four doc homes. Total effort for the high-payoff set: ~2–3 focused days.

## Ratings (weighted to elegance + simplicity)

| Cluster | Elegant | Simple | Working | Extensible |
|---|---|---|---|---|
| transforms.py + graph | 2.5 | 2 | 4.5 | 3.5 |
| new modules + operator surfaces | 4 | 4 | 5 | 5 |
| prompts + docs + philosophy | 3.5* | 3.5* | 5 | 5 |

*prompts strong; docs coherence 2.5 — the four-way re-narration drags the cluster.

The split is telling: **the new standalone modules are A-grade** (settle_commands,
data_facts, task_request, factory_prune all one-concern, reuse the fleet
primitives, degrade-not-crash). The debt is concentrated in the **old bulky
clusters that predate the extraction discipline** and in **docs mass**.

## The simplification plan — prioritized by payoff

### Tier 1 — high payoff, low risk, do first

**S1. Split transforms.py at its 6 natural seams (~1 day, mechanical, zero behavior change).**
The graph dispatches by dotted name and re-export via `from X import *` keeps every
ref valid, so extraction is safe. Concrete seams (line ranges at HEAD):
- `worktree_transforms.py` — `4966–6161` (worktree state/salvage + M40 env, ~1200 lines, own `_git`/`_worktree_*` family)
- `git_ledger.py` — `6204–6708` (M41c ledger + commit + merge, ~500 lines — belongs beside factory_ledger.py, not inline)
- `ab_instrument.py` — `46–284` + `1595–2264` (~700 lines)
- `test_runner_transforms.py` — `2522–3462` (~940 lines)
- `scope_transforms.py` — drift/scope (`317–363`, `1388–1493`, `3734–4007`)
- `grounding_transforms.py` — sceptic/grounding/settle/premise/subject (`414–951`, `3475–3733`)
Turns a file no one can hold in their head into six a reviewer opens by concern —
and satisfies the CLAUDE.md "one class/file + use-case docstring" spirit the
monolith violates wholesale. The M41 modules (`settle_commands`/`lang_profiles`/
`data_facts`, imported at `transforms.py:24–26`) are the working template.

**S2. Split lang_profiles.py (~half day).** It is six concerns in 1087 lines: its
namesake language grammar PLUS four runner-output data registries PLUS an
imperative windowing algorithm (`_merge_blocks`/`_clip_out`/`_render_blocks`/
`extract_runner_window`, `715–889`) that is not data at all. Move runner-output
handling (`428–631`, `715–938`) to `runner_output.py`; leaves lang_profiles
holding exactly language grammar. Cuts the file ~in half, restores one-concern.

**S3. Collapse the tripled/quadrupled incident narratives to canonical-home + pointer (~half day, prose-only).**
The 2026-08-05 worktree divergence is told in full three times (docstring 70 lines
+ graph note **3341 chars** + DECISIONS); M40-1 worktree-equivalence four times
(DECISIONS + README + SKILL + demo.project.json, same 29/29·7/6/7 measurement in
all four); M41 settle grammar three times. Rule: **incident lives in DECISIONS;
docstring states the invariant + one pointer; graph note states routing only.**
Targets: worktree-precheck note 3341→~600 chars; check_worktree_state docstring
70→~20 lines. Measured graph-note bloat: 56,697 note chars across 137 nodes,
25 notes >1000 chars, 3 >2000.

### Tier 2 — medium payoff

**S4. One overlay host-fact reference page + extend doctor (~1 day) — the junior-dev fix.**
README:9 explicitly defers the config reference; today "what can I set?" means
reading 300–500-word prose walls crammed inside JSON string values across
demo.project.json + factory-complete.json notes. Enumerate every fact (repo/root,
runner, merge repo, budget_usd, data_facts_path/stale_days, worktree_env_links/
copy_max_mb, deny_paths + settle budgets, runner_*_patterns, not_runnable_
signatures, window/artifact dials, strict_merge_target) with default + the one
shared precedence rule (*set replaces / unset = builtin / ""·false disables*).
Extend factory_doctor to the currently-unvalidated facts (settle budgets,
data_facts staleness, deny_paths, runner vocabulary). The config *idiom* is
elegant and must not change — only its discoverability has fallen behind.

**S5. One `_concern(code, message, fix_hint="", **extra)` helper (~half day).**
Measured: 77 inline concern dict literals + 6 divergent one-off builders. The
most-repeated primitive in the file has no shared constructor — a future concern-
schema change lands 77 times instead of once.

### Tier 3 — low effort, close the recurring smells

**S6. Fold the twin run-root readers** (`_worktree_location_from_run_root:5045` /
`_task_from_run_root:5069` — the exact "twin pair" prior assessments kept
flagging; collapse to one `_from_run_root(fn, default)`, ~1hr).
**S7. Trim the legacy worktree config mirror** (the `extras.get("root"/…)` fallback
nobody should set, ~8 docstring lines defending a dead branch; ~2hr + stale-overlay grep).
**S8. factory_doctor skip-list triplication** (`742–779`, collapse to `_skip_task_checks`, ~S).
**S9. Prompt polish** — one canonical untrusted-fence clause reused verbatim (it's
reworded at every site; the BLOCKED-escape template proves shared phrasing works);
sub-headers in eval.md (99 lines, 2 headers) and spec-grill.md (218 lines).

### Deferred / watch
- **Fake-overlay burden**: each new `cwd_from` stage needs a manual null in ≥5
  fake roots — bounded today, make it derived before it hits ~10 stages.
- **transforms prose ratio** will re-grow; S1+S3 reset it but the DECISIONS-not-
  docstring discipline (S3's rule) is what keeps it down.

## Philosophy preservation (the reassuring half)

| Principle | Status |
|---|---|
| Markdown-not-code (steer in prose, deterministic checks in code) | HELD — code routes on the model's own self-classification and RAISES a concern rather than overriding; the one vocab coupling is test-pinned and concern-raising, not a silent leak |
| Agent isolation / no self-correction loop | HELD — "the test is the defect" is a shared ROUTE delivered per-role, never shared reasoning |
| Counterfactual sceptics cold-read | HELD — drift classify reads the diff; only a human gate sees it, never routed back to the code agent |
| Advisory inputs labelled at every seam | HELD (wording drifts — S9) |
| Config as adaptation layer | HELD — worktree-env is "declared only, no heuristic"; no host-specifics in yaah_app |
| Hard vs soft gates | HELD — M39's three hard stops each gate a production change GREEN can't reveal |
| Fail-loud over silent fallback | HELD — no new `or "."`; the whole M35/M40 direction is fail-closed |
| DECISIONS.md canonical home | **DRIFTED** — entries are canonical rationale but duplicated in full elsewhere; S3 is the fix |

## Bottom line

Can s_factory be simplified? **Yes — meaningfully, and safely.** The single
highest-payoff move is S1 (split transforms.py; ~1 day, no behavior change, and
the pattern already exists three modules over). S1+S2+S3 together — roughly two
days — take the codebase from "a 7000-line file and four-way-duplicated docs that
a junior can't navigate" to "six concern-sized modules with one canonical home per
fact," without touching a single routing decision or gate. The functionality is
sound; what's owed is the extraction and de-duplication that four fast feature
batches deferred. Nothing here is urgent (correctness is green and adversarially
verified); all of it is worth doing before the UI phase adds another surface.
