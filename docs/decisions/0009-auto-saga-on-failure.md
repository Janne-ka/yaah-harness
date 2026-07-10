# 0009 — Auto-saga on terminal failure (opt-in cross-stage unwind)

**Status:** Accepted — SHIPPED 2026-07-07 (slices 1+2; slice-3 docs/example folded into the integration pass). The partial-undo PARK variant remains a named future extension.
**Original status:** Proposed — design-only (normative; builder-ready). Supersedes ADR-0008
D4's "no automatic saga" non-goal, which deferred this "to a LATER design if real
usage demands it." The maintainer has now ordered it built.
**Date:** 2026-07-07

## Context

ADR-0008 shipped rollback as three separate functions: `clear` (drop engine
state), `compensate` (undo the FAILING stage at failure time), and the operator
VERB `yaah rollback` (a human walks a run's PRIOR completed stages in reverse and
undoes them, honestly reporting the five buckets). D4 deliberately deferred the
AUTOMATIC version — a saga that fires on any terminal failure with no human
trigger — on four named dangers:

1. a **transient blip** could trigger semi-irreversible undos;
2. a **human is already in the loop** at failure (escalate/park), so auto-unwind
   competes with the person;
3. **partial unwinding is worse than none**, and an unattended saga owns that risk
   silently;
4. the verb has **no rollback-of-rollback tracking**, so an auto-saga could
   re-run on a retried resume.

This ADR answers each danger with a mechanism, not a hope. The core realisation
that makes it safe: **auto-saga fires at exactly one structural point — the
`StageFailed` that escapes `Harness._settle` — and everything D4 feared is either
already prevented at that point or converted into an explicit, bounded rail.**

Grounding facts verified against the engine (2026-07-07):

- `StageFailed` only escapes `_settle` **after** `_run_attempts` exhausts the
  transient budget (`error_retries`) *and* `max_attempts` (harness.py
  `_run_attempts`). Post-retry is structural — the saga cannot fire mid-retry.
- A stage with `escalate: "human"` **parks** (returns `Suspended`, not an
  exception); a `clearable` stage cancelled mid-flight returns `Cleared`. Neither
  raises `StageFailed`. So a hook on the `StageFailed` catch **cannot** fire on a
  parked or cleared run — D4 danger #2 is answered by construction.
- `_handle_error` (the failing stage's `compensate`/`clear`) runs **inside**
  `_exec_stage`, before `StageFailed` propagates out of the harness. So by the
  time any saga sees the failure, the LOCAL undo already ran — the ordering D4
  requires is automatic.
- On the default in-proc transport, `InProcessComms.publish` **awaits** its
  subscribers and `emit` awaits `publish`, so every completion span and the
  failing stage's error span are durably on disk (`FileTraceSink.handle` writes
  per-record) **before** `StageFailed` reaches the runtime. The trace file is
  complete and current at failure time.
- The harness holds `self.graph` (stages) but **not** the raw node config
  (`rollback:` blocks) nor the trace-file path; the runtime boundary (`run_root`)
  holds root + base + pipeline. So the saga belongs at the runtime boundary,
  like `drive`/gate_driver — NOT inside `_drive`/`_settle`. The harness stays
  ignorant (ADR-0001 cosmology).

## Decision

**Auto-saga = a per-graph opt-in that, when a run's terminal outcome is
`StageFailed`, replays ADR-0008's rollback executor over the run's PRIOR
completed stages in reverse — cheap-only by default — then ALWAYS re-raises the
original failure, with the five-bucket report recorded and, on a partial
unwind, a loud pointer to the manual `yaah rollback` verb (the park variant was
disproven against the engine — see D3 and design-eval #2). It reuses
`src/yaah/rollback.py` wholesale and adds NO new store.** It is a policy on top
of the harness, armed at the runtime boundary; the harness is untouched.

### D1. Config surface — `graph.on_failure`, next to the capabilities it uses

The opt-in lives at **graph level**, in the pipeline file, beside the `rollback:`
node decls and `effects_from:` stage decls that make an unwind possible:

```json
"graph": {
  "start": "...",
  "on_failure": "rollback",          // cheap-only saga; absent = no saga (default)
  "stages": { "...": "..." }
}
```

or, to also undo `costly` effects (opt-in, see D2):

```json
"on_failure": {"mode": "rollback", "include_costly": true}
```

- **Why graph-level, not root-level.** The undo STORY is a property of the
  pipeline: whether a graph is safe to auto-unwind depends on its nodes'
  declared `rollback:`/`effects_from:`, all of which live in the pipeline file.
  An author reading the pipeline must see "this graph auto-unwinds on terminal
  failure" in the same file, not buried in a deployment root. (The counter —
  operational toggles like `run`/`decisions`/`interactive` live at root — is
  noted; a root-level *override* for a deployment that wants to disable an
  armed graph is a natural, non-speculative follow-up, NOT built in v1.)
- **Value grammar mirrors `on_error`.** A bare string `"rollback"` (the only mode
  in v1, but named so the value self-documents and leaves room), or an object
  `{"mode": "rollback", "include_costly": bool}`. Absent → no saga (fully
  backward compatible; every existing pipeline is unchanged).
- **Read at the runtime boundary**, directly off `pipeline["graph"]["on_failure"]`
  — NOT threaded onto the `Graph`/`Stage` dataclasses. The saga is a runtime
  policy; the harness needs to know nothing about it (keeps `_drive` domain-free
  and unchanged).
- **`_GRAPH_KEYS` gains `on_failure`** (design-eval #5 — `validate_pipeline`
  rejects unknown graph keys, so without this concrete add every armed pipeline
  fails to load: the ADR-0008 `_STAGE_KEYS`-for-`effects_from` analogue). Plus
  the generated-schema entry.
- **Validate + arm cross-checks** (one shared helper, called from
  `validate_config` at author time AND from the runtime assembly at run time —
  the same dual-surface pattern ADR-0008 D2 pinned for the trace-sink check,
  because `validate_config` never runs on `yaah run`). Design-eval #3 caught
  that this helper's return list is RAISED unconditionally at both surfaces —
  a "warning" returned there is a hard failure — and #4 that its signature is
  `(root, pipeline_nodes)` so it cannot even see `graph.on_failure`. Pinned:
  - the helper's signature widens to take the PIPELINE (nodes + graph); both
    call sites updated — a named slice item, not an implicit "extend".
  - `on_failure: "rollback"` (or the object form) **REQUIRES a persisted file
    trace sink** (mode `tracer`) — ERROR, both surfaces, exactly as for
    `rollback:` nodes.
  - `on_failure` armed but **no node declares `rollback:`** → also an **ERROR**
    (was a WARNING in the draft): the saga could never undo anything — a
    self-contradictory config, the same class as sinks-under-`mode: none`.
    Erroring keeps everything in the ONE fatal helper (no root-scoped warning
    channel exists, and inventing one for this is not v1 work).
  - the non-inproc transport caveat (trace tail-lag, D5) becomes a DOCUMENTED
    limit in the reference docs, not a coded warning — same no-warning-channel
    reasoning; the risk is bounded and stated.
  - Unknown keys inside the object form rejected; `mode` must be `"rollback"`;
    `include_costly` a bool (mirror `_check_on_error`'s shape checking).
- **The corr source is pinned (design-eval #6):** the saga takes the run corr
  from `StageFailed.output.correlation_id`. That field is Optional and an
  absent header falls back to a fresh envelope id — so the saga GUARDS: if
  `output` is None, or the corr does not appear in the trace file at all, the
  saga is SKIPPED with a loud stderr notice naming why (never a guessed corr,
  never a silent empty unwind — ADR-0008 invariant #2 applies to the auto path
  too). A skipped saga still re-raises the original failure.

### D2. Interaction with `escalate` and the terminal-outcome contract

The saga fires **only** on a `StageFailed`-class terminal outcome — never on
`Suspended` (a parked human gate) or `Cleared` (a cancelled/reset run). This is
not a runtime check to remember; it is **structural**: the arm point IS the
`except StageFailed` boundary, and the other outcomes return normally through a
different path.

- `escalate: "human"` on the failing stage → the stage **parks** instead of
  raising `StageFailed` → the saga never fires; the human owns the decision
  (D4 danger #2, answered). If the human's later resume itself drives the run to
  a genuine ungated `StageFailed`, the saga fires **then** — correct: that IS an
  ungated terminal failure. The saga is precisely "unwind when no human is in the
  loop at the failure."
- `Cleared` (a `clearable` stage cancelled by a `*`/addressed clear) is the reset
  story, not a failure — the saga never fires. Nothing committed a completion
  the saga would treat as done-and-undoable anyway.

**Scope: cheap-only by default (D4 danger #1's severity control).** An unattended
auto-undo of a `costly`/semi-irreversible effect is the sharpest edge of D4. So
the saga's default is `include_costly=False` — costly candidates land in the
report's `skipped_costly` bucket, untouched, for a human to weigh with the verb.
`include_costly: true` in config is the author's explicit, file-visible consent
to auto-undo costly effects. (This reuses `rollback.execute`'s existing
`include_costly` parameter verbatim — no new logic.)

**Ordering — compensate first, then the saga (pinned, and automatic).** The
failing stage's own `on_error` (`clear` or `{compensate}`) runs inside
`_exec_stage` at failure time, BEFORE `StageFailed` propagates to the runtime. So
the LOCAL undo of the failing stage always precedes the cross-stage saga. There
is no overlap: the failing stage emitted an **error** span, not a completion
span, so `rollback._is_completion` (status `ok`, non-point, no `resumed`)
excludes it from the saga's candidate set — the saga only ever walks the PRIOR
COMPLETED stages. Any partial effect the failing stage committed before dying is
`compensate`'s job (the author declares it on that node); the saga does not
touch it.

**When the local compensate itself FAILED, the saga does not auto-run (pinned —
haiku #3, reshaped).** A failed compensate leaves the failing stage's committed
partial effects in an UNKNOWN state; unwinding the prior stages around that
unknown could make things worse, and an unattended saga must not own that
judgment. Detection needs no new plumbing: under the default
`on_compensate_fail: "error"`, `_handle_error` raises a `StageFailed` whose
verdict carries `Failure("compensation_failed", ...)`. Rule: when the terminal
verdict contains the `compensation_failed` code, the saga is **SKIPPED loudly**
— it emits its `saga` trace record with `skipped: "compensation_failed"` and
empty buckets, then re-raises; the human inspects and drives the verb. Under
`on_compensate_fail: "warn"` the code is absent by the author's explicit,
declared tolerance of a failed local undo — the saga runs; that consequence is
named on the `on_compensate_fail` reference so the author chooses it knowingly.

### D3. Partial-undo failure → stop, report LOUDLY, re-raise (design-eval #2 reshape)

The saga runs `rollback.execute(..., accept_partial=False)` — the verb's
**stop-on-first-failed-undo** default. `--accept-partial` is a human's explicit
consent to a half-unwound run; an unattended saga is NEVER granted it.

**The original draft parked the run as `Suspended human:saga:<stage>` "reusing
the escalate/park machinery." The design eval PROVED that reuse is impossible**
(#2, must-fix): by the time the saga runs, `_settle` has already DELETED the
baton and re-raised (`harness.py` StageFailed path) — there is nothing to park;
`resume()` contractually drives the pipeline FORWARD from a parked stage, so an
"acknowledge" decision would RE-RUN the pipeline after the very failure the
saga just unwound; and no acknowledge-shaped decision form exists. Building the
park honestly = manufacture a fresh baton + a synthetic form + a NEW
terminal-acknowledge path through resume — genuinely new machinery, exactly
what this section promised to avoid.

**v1 pins the conservative cut instead — no park:** on a partial unwind the
saga stops, writes its `saga` trace record, attaches the five-bucket report to
the surfaced failure (see the table), and prints an UNMISSABLE stderr block
naming the half-unwound state and the exact finishing command
(`yaah rollback <root> <corr>` — whose menu already shows the failed undo and
the `not_attempted` remainder, with `--only`/`--include-costly`/
`--accept-partial` as the human's controls). The manual verb IS the human path
(ADR-0008's menu-first philosophy); a park that can't be resumed-to-closure is
not. The `human:saga:` park + terminal-acknowledge resume path is a NAMED
FUTURE EXTENSION (requires the new machinery above), not v1.

When the saga completes cleanly (all in-scope candidates undone; `skipped_costly`
and `impossible` are honest successes, not failures), the saga does **NOT** mask
the failure: it **re-raises the original `StageFailed`** (ADR-0008's principle —
recovery runs, then the failure still surfaces). The run still failed; the saga
merely left clean state and a report. Callers see `StageFailed` exactly as before,
backward compatible.

| saga outcome | terminal result | rationale |
|---|---|---|
| clean (undid all in-scope; costly/impossible reported) | **re-raise `StageFailed`** + report | run failed; state is clean; failure must still surface |
| partial (an undo raised) | **re-raise `StageFailed`** + report + LOUD stderr pointer to `yaah rollback` | half-unwound state must be unmissable; parking is impossible without new machinery (eval #2) |
| nothing to undo (no candidates) | **re-raise `StageFailed`** | same as clean, empty report |
| skipped (`compensation_failed` in the verdict) | **re-raise `StageFailed`**, `saga` record notes the skip | local state unknown — the human drives the verb (D2) |

**Where the report lands (pinned):** on the `saga` trace record (the durable
copy — see D4's contributor requirement) AND printed; `StageFailed` itself is
not extended in v1 (its `output` is the failing stage's artifact — overloading
it with saga state would change a public shape for a report the trace already
carries).

### D4. Idempotency — a `saga` trace record is the single-corr ledger (D4 danger #4)

The verb has no rollback-of-rollback tracking; the auto path, reading the trace
anyway, gets a bounded one **without a new store**: on completion the saga emits a
**`saga` trace record** for the run corr, through the SAME tracer that feeds the
file sink, carrying the five-bucket report (stage/node/occurrence IDENTITIES
only — no payload values; the buckets are already values-free, consistent with
the keys-only trace contract). Because the record `name` is `"saga"`, not
`"stage"`, `rollback._resolve` (which keys only on `name == "stage"`) never
mistakes it for a candidate.

**MANDATORY projection wiring (design-eval #1, must-fix — the whole ledger was
a silent no-op without it):** `record.project()` emits only structural keys plus
what CONTRIBUTORS return, and the phase contributor whitelists a FIXED attr set
— none of the five buckets is in it, so the emitted saga record would persist
with EMPTY buckets and every later saga would exclude nothing (re-undoing on the
retried resume — the exact danger this section closes). Pinned: the five bucket
attrs (`rolled_back`, `skipped_costly`, `impossible`, `failed`, `not_attempted`)
plus a `skipped` marker are added to the phase contributor's whitelist,
NAMED as a migration-slice item with its own falsifying test (a projected saga
record round-trips its buckets through the file sink). This is the same edit
ADR-0008 D2 made for `effects*` — the precedent that exposed the omission.

Before running, the saga reads prior `saga` records for this corr and **excludes
candidates a prior saga already rolled back**, keyed by `(stage, occurrence)`
(the same file-position occurrence key `rollback.py` already assigns:
`_resolve` numbers a stage's runs 1, 2, ... in file-append order within the
corr). That key is **stable across re-reads** because the trace is append-only:
a resume only APPENDS records, so a stage's existing occurrence numbers never
shift — a later saga re-resolving the grown file assigns the same numbers to the
same completions, and a new post-resume run of the same stage gets the NEXT
number. This makes the auto path idempotent across a **retried resume**:

- Run fails → saga undoes A, B → emits `saga{rolled_back:[A#1,B#1]}`.
- Human resumes; the run re-fails at a LATER stage C that committed a NEW effect →
  the second saga reads the trace, sees A#1/B#1 already rolled back, **excludes
  them**, and undoes only C#1. It neither re-undoes A/B nor silently misses C.

This is a small, principled extension to `rollback.execute` (an optional
`exclude` set of `(stage, occurrence)` keys) that also gives the verb a future
`--skip`. **Honest residual limit (stated, not hidden):** the ledger is
per-corr and read by the auto path only. A human running `yaah rollback
--execute` after an auto-saga still re-calls the targets — ADR-0008 D4's rule is
unchanged: **undo targets SHOULD be idempotent**. The trace ledger closes the
retried-resume hole, not the cross-tool-double-run hole.

### D5. Execution source — the trace FILE, reused via `rollback.py` (not an in-memory log)

The saga reads the persisted trace via `rollback.read_trace` /
`rollback.load_pipeline` and runs `rollback.execute` — **the shipped executor,
reused, not duplicated.** The alternative (an in-memory effects log the harness
keeps for the current run, which the maintainer flagged as "cleaner") is
**REJECTED**, and the trade-off is named:

- **Completeness across a cross-process resume is disqualifying for the in-memory
  log.** A run can park in process A and resume in process B; the terminal
  failure then occurs in B, whose in-memory log holds ONLY the post-resume
  stages. The pre-park committed effects of process A are invisible to it — an
  in-memory saga would **silently under-unwind**, violating ADR-0008 invariant #2
  ("undeclared/absent is reported, never silently skipped"). The trace FILE has
  every stage across every process by construction. That completeness is
  non-negotiable for a function whose entire value is honesty about what it did
  and didn't undo.
- **Currency at failure time is the file's only weak spot, and it is bounded.**
  On the default in-proc transport it is a non-issue: `publish` awaits the sink,
  so the file is complete and current when `StageFailed` surfaces (verified). On
  a **decoupled async transport (NATS)** where the file sink is a remote
  subscriber, the file may lag the in-process failure by the last few records.
  v1's answer (MAINTAINER PIN, resolving the draft's self-contradiction the
  builder reported): the residual risk is a JUST-committed effect not yet on
  disk — which the saga simply does not see, and which therefore appears in
  NEITHER `rolled_back` NOR `impossible`; the report shows what was seen, and
  `stop-on-partial` bounds the blast radius. This is a DOCUMENTED limit, not a
  coded warning (the D1 helper is fatal-only — eval #3 — and no shipped tracer
  exposes a drain/flush barrier the saga could await: `BusTracer.drain` returns
  `[]` by contract, so the draft's "preference (a)" was unimplementable and is
  DROPPED). This limitation also lands on the
  `on_failure` reference; it is strictly smaller than the in-memory log's
  cross-process hole, which has no bound at all.
- **"ADR-0008 chose no-new-store for the VERB — the saga is a different
  consumer."** Correct, and the answer is the same store (none): the saga is a
  second reader of the record ADR-0008 already mandates and already enforces a
  file sink for. No journal, no new persistence.

### D6. Where it hooks — a shared runtime-boundary seam

Both run entrypoints (`run_root` and the MCP `run` tool, which already share
`_seed_task`) route their terminal call through **one shared helper** — call it
`_settle_terminal(root, base, pipeline, store, harness, coro)` — that awaits the
run/`drive` coroutine and, on `StageFailed`, arms the saga per D1–D5. This mirrors
`_seed_task`'s shared-seam pattern and keeps the arming logic in exactly one
place. The saga reaches the tracer via the harness (`harness._tracer`, or a
minimal accessor) so its `saga` record lands in the same file the verb and the
ledger read. `drive()` re-raises `StageFailed` (it does not catch), so wrapping
the whole `drive`/`run` call catches a failure from the initial run OR from any
resume inside the gate loop.

## Invariants

1. The saga fires on `StageFailed` and nothing else — never on `Suspended` or
   `Cleared`. Structural (the arm point is the `except StageFailed` boundary).
2. The saga never masks the failure: EVERY saga outcome re-raises the
   original `StageFailed` (the D3 table); a partial unwind additionally prints
   the unmissable pointer to the manual verb.
3. Default scope is cheap-only; auto-undoing a `costly` effect requires explicit
   file-visible `include_costly: true`.
4. `accept_partial` is never granted to the automatic path — on a failed undo
   the saga STOPS, reports, and points the human at the manual verb (no park in
   v1 — eval #2).
5. No new store: the saga reads and writes the SAME trace file ADR-0008 mandates.
6. Domain-free engine: all undo logic arrives as `call_target`s via the reused
   `rollback:` decls (same `fn:`/`http:` restriction — one target contract works
   from both the CLI verb and the in-process saga).
7. The harness is untouched: the saga is a runtime-boundary policy, like `drive`.

## Non-goals (v1, deliberate)

- **No new undo capability.** The saga reuses `rollback:`/`effects_from:` exactly;
  it adds no per-node config beyond the `graph.on_failure` toggle.
- **No dependency-aware ordering.** Reverse completion order, inherited from the
  verb; the engine does not model domain dependencies.
- **No auto-undo of costly effects** without explicit `include_costly`.
- **No silent partial unwind** — partial stops, reports in the `saga` record,
  and prints the unmissable finishing pointer (never silent; never a park in v1).
- **No cross-tool rollback-of-rollback tracking** beyond the per-corr trace
  ledger; a manual verb run after an auto-saga still re-calls (targets SHOULD be
  idempotent).
- **No node-target undos in the saga** (even though the harness has comms):
  keeping ONE target contract that runs from both callers beats a capability that
  works in one and not the other.
- **No root-level override** in v1 (graph-level declaration only); a deployment
  override is a documented future extension, not built.
- **No auto-saga on a lagging remote sink beyond best-effort** — where currency
  can't be confirmed, the limitation is documented (D5), not papered over.

## Migration slices (parallel-buildable against this spec)

1. **Config + arm lane.** `_GRAPH_KEYS` gains `on_failure` (eval #5) + validate
   the value (string `"rollback"` | object `{mode, include_costly}`) + schema
   regen; WIDEN `check_rollback_trace_sink`'s signature to take the pipeline
   (nodes + graph — eval #4), update BOTH call sites; armed `on_failure`
   requires a file sink under mode `tracer` (ERROR, both surfaces) and armed
   with no `rollback:` node is an ERROR too (self-contradictory config —
   eval #3: the helper is fatal-only, warnings don't fit it); the non-inproc
   tail-lag caveat goes to docs, not code. Tests: accept/reject shapes; both
   ERRORs fire on `yaah validate` AND the run path.
2. **Saga executor lane.** The runtime-boundary hook (run_root + resume_gate +
   MCP run — the eval verified StageFailed escapes uncaught to all three) + the
   saga policy: corr from `StageFailed.output` with the skip-loudly guard
   (eval #6), read trace (D5), the `(stage, occurrence)` `exclude` extension to
   `rollback.execute`, run cheap-or-`+costly` in reverse with
   `accept_partial=False`, emit the `saga` trace record — **including the phase
   contributor whitelist add for the five bucket attrs (eval #1: without it the
   ledger persists EMPTY and idempotency is a no-op), with a round-trip
   falsifying test** — and branch per the D3 table (clean/partial/empty/skipped
   ALL re-raise; partial adds the loud stderr pointer to `yaah rollback` — the
   park is a named future extension, eval #2). Tests (offline, file trace +
   `rollback:` decls + `on_failure`): force a terminal failure → assert undo
   calls, five-bucket report, and re-raise; assert `skipped_costly` under
   default and undone under `include_costly`; assert an undo that raises →
   re-raise + report with `not_attempted` populated + the stderr pointer;
   assert NO fire on `Suspended` (escalate) and `Cleared`; assert no double-run
   on a retried resume (the `saga`-record exclusion, round-tripped through the
   REAL file sink); assert the saga is SKIPPED loudly when the terminal verdict
   carries `compensation_failed` (and RUNS under `on_compensate_fail: "warn"`);
   assert a looped stage (ran twice, occurrence 1+2 rolled back, third run
   post-resume) undoes only occurrence 3 on the second saga; assert the
   corr-guard skip (output None / corr absent from trace).
3. **Docs + example lane.** Node-reference / shape-grammar `graph.on_failure`
   entry; the compensate-vs-saga ordering (D2) and the async-sink currency limit
   (D5) documented; extend `examples/rollback-demo` (or a sibling `examples/
   auto-saga`) with `on_failure` armed — one cheap undo that fires automatically,
   one `costly` node shown skipped, one undeclared node shown `impossible`, and a
   forced-failure driver so the demo produces a real saga report.

## Review record (adversarial design eval, 2026-07-07 — pre-implementation)

An opus eval falsified the draft against the engine source (the 0007/0008
precedent). Findings, all folded into the sections above before any code:

1. **[MUST-FIX] The saga ledger persisted with EMPTY buckets** — `project()`
   emits only structural keys + contributor output, and the phase contributor
   whitelists a fixed attr set; none of the five buckets was in it, so D4's
   idempotency was a silent no-op (a retried resume would re-undo everything).
   → the whitelist add is now a pinned slice item with a round-trip test.
2. **[MUST-FIX] The partial-undo park could not reuse the escalate machinery**
   — the baton is DELETED before the saga runs; `resume()` only drives forward
   (an acknowledge would re-run the failed pipeline); no acknowledge form
   exists. → D3 reshaped: v1 drops the park for a loud report + pointer to the
   manual verb; the park is a named future extension requiring genuinely new
   machinery.
3. **[MUST-FIX] Warnings folded into a fatal-only helper** — both call sites
   raise on any returned string. → armed-with-no-rollback-nodes promoted to
   ERROR (self-contradictory config); the transport caveat demoted to docs.
4. Helper signature can't see the graph → widened to take the pipeline, both
   call sites named as slice work.
5. `graph.on_failure` would be REJECTED as an unknown graph key → `_GRAPH_KEYS`
   add pinned.
6. The corr source was unspecified with an Optional/divergable obvious choice
   → pinned to `StageFailed.output.correlation_id` with a skip-loudly guard.
Verified sound by the same eval: trigger coverage (StageFailed escapes uncaught
to run_root/drive/resume_gate on every terminal-failure shape; Suspended and
Cleared structurally can't fire it), compensate-first ordering, the
`compensation_failed` skip detection, in-proc trace currency (publish awaits
the per-record sink), occurrence-key stability + the exclude extension's
feasibility, saga records never becoming candidates, no-new-store.

## Review record (fast counter-arg, 2026-07-07 — pre-ADR)

A haiku contrarian attacked the five pinned choices; each verdict was verified
against the engine source before folding in (advice, not truth):

1. "Graph-level bakes a prod/dev deployment decision into author config —
   allow a root override" — **REJECTED as v1 build** (no present stakeholder
   for the split; building for a hypothetical violates the no-speculative-
   building rule). The coupling is OWNED explicitly in D1 and the root-level
   override stays a documented future extension in the non-goals.
2. "StageFailed-only boundary is clean — but document that Suspended never
   fires the saga even on later failure after resume" — **AGREE, with a
   correction**: the reviewer's phrasing was wrong about the design — a
   post-resume run that reaches a genuine ungated `StageFailed` DOES fire the
   saga then (D2 already pins this); only the park itself never fires it.
3. "Compensate-failure state model is murky: if the local undo failed, the
   saga unwinds priors around a stage in unknown state" — **ACCEPTED,
   RESHAPED into D2's skip rule**. Half of the attack was misaimed (the saga
   never touches the failing stage — no completion span, verified against
   `rollback._is_completion`), but the residue was the sharpest finding of the
   review: pinned as "saga SKIPPED loudly when the terminal verdict carries
   `compensation_failed`; runs under the author's explicit `warn` tolerance."
   The park-UX half was also folded (D3: acknowledge-shaped decision; the verb
   is the control surface — no duplicated controls in a decision schema).
4. "'Occurrence' is undefined — lock the keying schema" — **REJECTED as a
   gap** (the reviewer couldn't see `rollback.py`: occurrence is already the
   file-append-position key `_resolve` assigns), **ACCEPTED as a doc need**:
   D4 now states WHY the key is stable across re-reads (append-only file ⇒
   numbers never shift), and slice 2 gained the looped-stage test the
   reviewer's danger #3 implied.
5. "Async (NATS) sinks make the trace stale at failure time — gate it with a
   config ERROR or an explicit acknowledgment" — **HALF-ACCEPTED**: made loud
   as an arm-time **WARNING** (D1/D5), not an ERROR — the lag is bounded to
   one run's tail and stop-on-partial bounds the consequence, while an ERROR
   would forbid auto-saga on every distributed deployment.

Dangers flagged beyond the five: "no undo-for-the-undo after a partial park" —
**REJECTED** (matches ADR-0008 D4's standing no-rollback-of-rollback non-goal;
the verb + idempotent targets are the whole story, restated in the non-goals);
"trace flush/buffering staleness even in-proc" — **already answered** (verified:
`InProcessComms.publish` awaits the sink and `FileTraceSink` writes per-record
with no buffer; grounding facts, Context).
