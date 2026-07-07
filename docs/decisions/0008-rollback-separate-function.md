# 0008 — Rollback is a separate function (clear is not rollback)

**Status:** Accepted — SHIPPED 2026-07-07 (all three slices: config+record, `yaah rollback` tool, docs+`examples/rollback-demo/`). Auto-saga (D4) remains a deliberate non-goal.
**Date:** 2026-07-07

## Context

A stage with committed EXTERNAL side effects (wrote to a client API, pushed
files) fails — or a human decides after the fact that a run's effects must be
undone. Today the engine offers:

- `on_error: "clear"` (default) — drops ENGINE-side state only. It reads like
  cleanup and is recorded like cleanup, but nothing external is undone — the
  "false sense of rollback" (slop-audit D#6).
- `on_error: {compensate: T}` — a per-stage undo run AT FAILURE TIME for the
  FAILING stage only. The cross-stage walk-back over COMPLETED stages is
  explicitly deferred in its own docstring.

Maintainer ruling (2026-07-07, verbatim in substance): *clear is not rollback;
rollback is a SEPARATE function; rollback means special logic in most nodes;
for some occasions the cost is too high, and occasionally it is not possible.*

Three constraints follow:
1. **Undo logic is domain logic.** The engine cannot derive how to un-write an
   amendment; the author supplies it per node (as with `compensate`).
2. **Cost and impossibility are first-class outcomes**, not errors. A rollback
   report that says "these two effects cannot be undone" is a SUCCESS of the
   function, honestly delivered.
3. **Partial unwinding can be worse than none.** Half-undone state must never
   happen silently; the operator explicitly owns that risk or it doesn't run.

## Decision

**Rollback = declared per-node capability + a recorded effect descriptor + an
operator VERB that walks completed work in reverse and reports honestly.**
`clear` and `compensate` are untouched; rollback is a third, separate function.

### D1. Capability: node-level `rollback` config key

```json
"push_amendment": {
  "type": "post", "...": "...",
  "rollback": {"target": "fn:undo:delete_amendment", "cost": "cheap"}
}
```

- `target` (required): a `call_target` restricted to **`fn:` / `http:`** in v1
  — the rollback tool runs OUTSIDE a harness (a CLI reading files), and a
  `node:` target hard-requires comms (`external_call.py` raises without it);
  validate REJECTS `node:` here rather than let it validate, record, appear in
  the menu, and die at --execute (design-eval #3). `fn:`/`http:` run comms-free.
- `cost` (optional, default `"cheap"`): `"cheap"` | `"costly"`. A HINT from the
  author, not a computed truth (the fast counter-arg wanted trace-computed
  cost; rejected — whether record C depends on B is domain semantics the
  engine cannot know. The menu compensates: it SHOWS what ran after each
  candidate so the human judges dependencies).
- Absent `rollback` = **honestly unknown, treated as irreversible**: listed in
  the report under `impossible`, never guessed at.
- Validate: unknown keys inside `rollback` rejected (mirror `_check_on_error`'s
  shape-checking); `target` a non-empty `fn:`/`http:` string; `cost` one of the
  two values. (A TOP-LEVEL typo — `rollbck:` — is a silent no-op today for ALL
  node keys; pre-existing engine gap, noted not fixed here.)

### D2. The record: an effect descriptor on the EXISTING trace, no new store

The fast counter-arg ("the trace already has it / drop the journal") was half
right: capability lives in config, and completed stages are already traced —
but trace records deliberately carry NO payload values, and an undo needs the
effect HANDLE (the amendment id the API returned). So:

- New STAGE key `effects_from: "<payload-key>"` (wired like `concerns_from`'s
  pull, at the shared `_exec_stage` completion seam): on stage COMPLETION, the
  harness copies `payload[<key>]` — the author-chosen, SMALL effect descriptor
  — onto the stage's completion span as attr `effects`. NOTE: this is the
  FIRST payload value ever written into a trace record (concerns only ever
  recorded a COUNT — there is no bounding precedent to copy), so the bound is
  defined here from scratch (design-eval #5/#11): if the JSON-serialized
  descriptor exceeds 2048 chars, the descriptor is NOT stored — never a
  mid-string clip of JSON (unparseable garbage) — the record instead carries
  `effects: null, effects_truncated: true, effects_head: "<first 256 chars,
  plain string>"`. The menu lists such a candidate with a loud
  descriptor-dropped warning; its undo receives `effects: None`.
- `effects_from` is REJECTED by validate on a `fork`/`fanin` stage (design-eval
  #6): the fork PARENT's completion span is emitted on a separate no-output
  path, so the copy would silently record nothing — forbidden loudly instead.
  Linear, branch-child, and foreach stages all complete through the shared
  seam and record fine.
- The phase contributor whitelists `effects` / `effects_truncated` so they
  reach projected records.
- **Cross-file check (ERROR), on BOTH paths:** a pipeline in which any node
  declares `rollback` MUST have a persisted JSONL trace sink (`trace.sinks`
  containing `{"type": "file", ...}` — specifically FileTraceSink;
  progress_file/stats_file write human tails/aggregate snapshots and cannot
  feed the tool). The record IS the rollback input. Design-eval #2 caught the
  spec's own trap: this check needs root+pipeline in scope, which only
  `validate_config` has — and `validate_config` does NOT run on `yaah run`
  (only on `yaah validate`/MCP/replay), so "checked at load" would have been a
  lie. Pinned: ONE shared helper, called from `validate_config` (author time)
  AND from the runtime assembly (run time — runtime holds both root and
  pipeline), so `yaah run` refuses before committing effects it can't record.
  (No rollback declared → no requirement; the console-only default trace
  correctly ERRORs when rollback is declared.)
- Lint (WARNING): a node declaring `rollback` whose stage(s) have no
  `effects_from` — the undo will receive no effect handle (legal: some undos
  key off corr alone; the author should say so knowingly).

### D3. The verb: menu first, execution explicit

```
yaah rollback <root> [<correlation-id>] [--json]
yaah rollback <root> <correlation-id> --execute
     [--include-costly] [--only <stage>]... [--accept-partial]
```

- No corr → LIST runs found in the trace file that have rollback candidates.
- With corr, default = **the MENU (a dry run)**: candidates in REVERSE
  completion order — ordered by **file append position** (JSONL line index),
  NEVER by span `t_start`: trace times are process-local monotonic (the
  engine's own two-clock docstring), and a run resumed in a fresh process — THE
  rollback scenario — emits post-resume spans on a new zero-point, so a
  t_start sort interleaves/inverts the undo order (design-eval #1, must-fix).
  Append order is chronological across processes by construction.
  Candidate selection takes REAL COMPLETION SPANS only (design-eval #4):
  status `ok` AND not a point-span (`t_start == t_end`) AND not carrying the
  `resumed` attr — else a gate's resume note masquerades as a candidate.
  A stage that ran TWICE (a branch-backward loop) yields TWO candidates with
  their own descriptors, keyed by file position and shown with occurrence
  numbers; `--only <stage>` addresses ALL occurrences of that name
  (design-eval #8). Each candidate shows: stage, node, declared cost, the
  recorded `effects`, and **the stages that ran after it** (the
  dependency-visibility the human needs to judge order risk). Stages whose
  node declares no rollback are listed under `impossible`.
- `--execute`: run each candidate's `target` in reverse order with ctx
  `{correlation_id, stage, node, effects, cost}`. DELIBERATELY NOT compensate's
  ctx (design-eval #7): compensate hands the failing stage's full `payload`;
  rollback hands the bounded `effects` descriptor recorded at completion. A
  compensate fn is NOT drop-in reusable as a rollback target — an author
  reusing one would silently read an absent `payload` key; the divergence is
  named here and in the node reference rather than papered over.
  - `costly` candidates are SKIPPED unless `--include-costly`.
  - `--only <stage>` (repeatable) restricts to named stages — the
    one-at-a-time operator mode the counter-arg argued for.
  - **Stop on the first FAILED undo** unless `--accept-partial` — half-unwound
    state is never silent; continuing past a failure is an explicit,
    flag-consented choice (counter-arg #4, accepted).
- The report (printed; `--json` for machines):
  `{"run": corr, "rolled_back": [...], "skipped_costly": [...],
    "impossible": [...], "failed": [...], "not_attempted": [...]}` —
  `impossible` is a first-class outcome, not an error.

### D4. What rollback is NOT (v1 non-goals, deliberate)

- **No automatic saga on failure.** Auto-unwind on any terminal failure is
  dangerous (a transient blip triggering semi-irreversible undos) and a human
  is already in the loop at failure (escalate/park). A per-pipeline
  `on_failure: rollback` opt-in is a LATER design if real usage demands it.
- **No dependency-aware ordering.** Reverse completion order + the
  what-ran-after view; the engine does not model domain dependencies.
- **No rollback-of-rollback tracking.** The verb does not record prior
  rollbacks; undo targets SHOULD be idempotent (documented; running the verb
  twice re-calls them). A rollback event record is future work.
- **No transactional guarantee.** Stop-on-failure + explicit
  `--accept-partial` is the whole consistency story, stated plainly.
- `compensate` unchanged: it remains the immediate, local, at-failure undo;
  rollback is the cross-stage, possibly-later, human-triggered function.

## Invariants

1. Payload VALUES never enter trace records except the author-chosen
   `effects_from` descriptor (bounded, author-owned).
2. Undeclared rollback is reported as `impossible` — never guessed, never
   silently skipped.
3. The verb without `--execute` calls NOTHING (the menu is pure read).
4. Domain-free engine: all undo logic arrives as `call_target`s.
5. `clear`, `compensate`, and rollback remain three separate functions with
   three separate names; none is described as another.

## Migration slices (parallel-buildable against this spec)

1. **Config + record lane**: validate (D1 node key, D2 `effects_from` stage
   key + root trace-sink cross-check ERROR + WARNING) + schema regen + harness
   records `effects` on the completion span + phase contributor whitelist.
   Tests: config rejects/accepts; span carries the clipped descriptor; lint
   ERROR/WARNING fire.
2. **Tool lane**: `src/yaah/rollback.py` (trace-file reader + candidate
   resolution from root/pipeline config + menu + execute + report) + `yaah
   rollback` CLI verb. Tests: fixture JSONL menu correctness (reverse order,
   what-ran-after, impossible), execute honors include-costly/only/
   accept-partial/stop-on-failure, end-to-end: run an offline pipeline with
   file trace + rollback decls, execute rollback, assert undo calls + report.
3. **Docs + example lane**: node-reference/shape-grammar entries + a runnable
   `examples/` demo (effects = files written; rollback deletes them; one
   `costly` + one undeclared node so the report shows all four buckets).

## Review record (adversarial design eval, 2026-07-07 — pre-implementation)

An opus eval falsified the draft against the engine source (the ADR-0006/0007
precedent). FIVE must-fix spec bugs, all folded into the sections above before
any code:

1. **t_start ordering wrong across processes** — trace times are process-local
   monotonic; a resumed run (THE rollback scenario) would interleave the undo
   order → file append position is the ordering key (D3).
2. **"Checked at load" was a lie** — validate_config never runs on `yaah run`;
   the cross-check now also runs in the runtime assembly (D2).
3. **node: targets can't execute from the CLI** (call_target requires comms) →
   v1 restricts rollback targets to fn:/http: at validate time (D1).
4. **Resume/retry point-spans masquerade as candidates** (status ok too) →
   selection pinned: real completion spans only (D3).
5. **Mid-string JSON clipping = garbage** → oversize descriptors are dropped
   with `effects_truncated` + a plain-string head, never clipped JSON (D2).

Design defects folded in: #6 effects_from silently no-op on fork/fanin parents
→ validate-rejected; #7 rollback ctx ≠ compensate ctx → divergence named, not
papered over; #8 twice-run stages → candidates keyed by file position with
occurrence numbers; #9 the check is a validate_config+runtime concern, not
"root-level". Nits: #10 rollback-block validator mirrors _check_on_error (the
top-level node-key typo gap is pre-existing, noted); #11 the concerns bounding
analogy was false (concerns record a COUNT) — the effects bound is defined
from scratch. Verified sound: the _exec_stage seam covers linear/branch-child/
foreach recording; _STAGE_KEYS + phase-whitelist are one-line adds; file-sink-
only is the right lint target (progress/stats sinks can't feed the tool);
corr filtering works (every record carries corr); the console default
correctly errors.

## Review record (fast counter-arg, 2026-07-07 — pre-ADR)

A haiku contrarian attacked the sketch; verdicts applied to this spec:
1. "Menu, not replay" — ACCEPTED (D3: dry-run default, `--only`, execution
   explicit).
2. "Drop the journal, trace has it" — RESHAPED (D2: no new store — but the
   trace carries no values, so the author-declared `effects_from` descriptor
   rides the existing trace record; capability lives in config).
3. "Cost is trace-computed, config labels lie" — HALF-REJECTED (D1: cost is
   domain semantics the engine can't compute; kept as an author hint, menu
   shows what-ran-after for human dependency judgment).
4. "Partial rollback worse than none" — ACCEPTED (D3: stop-on-first-failure,
   `--accept-partial` explicit).
5. "Journal only pays off with auto-saga" — REJECTED on a fact it couldn't
   see: the descriptor is the undo CONTEXT no other record preserves; without
   it even the menu cannot act.
Forced requirement: rollback-declaring pipelines REQUIRE a persisted trace
sink, checked at load (D2), not discovered at undo time.
