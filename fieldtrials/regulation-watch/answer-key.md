# Answer key — TRIAL RUNNER ONLY

**Do not give this file to a builder agent.** It is the scoring anchor for the
person running the trial. It lists each seeded landmine in Part B, the signal
the engine is supposed to give, the intended fix, and the ACTUAL engine
behavior verified against the current branch (with real output pasted). If the
engine changes and a landmine stops behaving as documented here, `selfcheck.py`
will fail — update this file and the kit together.

All output below was captured on branch `feat/post-taskpack`. The engine now
includes `gate-route-not-in-form` (added after round 1), so L3 surfaces as a
hard validate ERROR — update this file if the engine changes again.

**Round-3 change:** standalone L2 (silent `{{?judge_notes}}` decoy) has been
retired and merged into L4. The kit now has **4 traps** (L1, L3, L4-layered,
L5). L4 is now a two-state layered trap — see the L4 section below. Historical
round1/round2 records that reference L1–L5 remain coherent: L1/L3/L5 are
unchanged; L2 is retired/merged; L4 is the merged layered trap.

---

## Surfacing order (important for scoring)

The four landmines do not all appear at once — they chain by dependency, which
is realistic and intended. A builder fixes one, reruns, and meets the next:

| When | Surface | Landmine |
|------|---------|----------|
| `yaah validate` (before any run) | `gate-route-not-in-form` **ERROR** | **L3** |
| after L3 fixed, `yaah validate --strict` | `missing-carry` warning | **L1** |
| after L1 fixed (or run despite warning), `yaah run` | `render_unfilled_placeholders` at `draft` | **L4 STATE 1** |
| after L4 STATE 1 fixed (add `?`), next `yaah run` | suspends clean — but loop is blind (silent) | **L4 STATE 2** |
| after L4 STATE 2 + L5 fixed so run reaches gate, `yaah run` | `schema_mismatch` at `judge` | **L5** |

L3 surfaces FIRST as a hard error — the `gate-route-not-in-form` lint fires at
`yaah validate` (exit 1 in `--json` mode, exit 2 in plain/strict). L4 is the
merged layered trap: STATE 1 is loud (render fault), STATE 2 is silent (blind
loop). The interesting L4 measurement is whether the builder sees both states —
a builder who only adds `?` has cleared the fault but left the loop blind. The
interesting L3 measurement is WHICH FIX the builder picks: upgrade the form to
`approve_or_revise` + rename the route, or just delete the phantom route.

---

## L1 — render/branch reads a key an upstream `parse:true` agent drops

- **Site:** `role:classify` is a `parse:true` agent with no `output_schema`. The
  `classify` stage branches on `high_impact` — a key classify produces in its
  reply but does not carry or declare, so the reset drops it.
- **Intended discovery signal:** the `missing-carry` lint, at
  `yaah validate --strict` (or `--json`).
- **Intended fix:** declare classify's output (`output_schema` with
  `high_impact`, or `provides: ["high_impact", ...]`, or `carry`).
- **Observed behavior (verified):** the lint fires. `validate --strict` exits 2;
  `validate` (plain) exits 0 but prints the warning to **stderr**; `validate
  --json` lists it under `warnings[].id == "missing-carry"`. Exact text:

```
warning[missing-carry]: stage 'classify': reads ['high_impact'] which the
upstream agent stage(s) 'classify' do NOT provide — a `parse:true` agent with
no output_schema/provides RESETS the payload (keeping only raw), so the key is
DROPPED and the read fails with render_unfilled_placeholders on any run where
the model doesn't happen to echo it. Add the key(s) to the agent's `carry: [...]`
if they should pass through unchanged, or declare them in the agent's
output_schema/`provides` if the model emits them.
```

Note: `missing-carry` (not `render-key-unprovided`) fires here specifically
because the branch reads its OWN node's output and that node is an
incomplete-reset agent. A read whose producers are further upstream (through a
transform/gate) trips `render-key-unprovided` instead — same fix family.

---

## L2 — retired (merged into L4-layered)

**L2 is retired as a standalone trap** as of round 3. The key mismatch between
the loop-guidance placeholder and the key `tally` writes has been merged into the
L4 layered trap. Historical references to L2 in round1/round2 records (where it
described a `{{?judge_notes}}` decoy read by `draft.md`) remain correct for those
rounds. In round 3, look at L4-layered for the equivalent signal.

---

## L3 — gate `form: "approve"` but the branch has approve AND reject routes

- **Site:** `role:gate` declares `form: "approve"` (a single-button form whose
  only decision value is `"approve"`). The `gate` stage branches on `decision`
  with routes `{"approve": "digest", "reject": "draft"}`. The `reject` route
  references a decision value the form can never produce.
- **Intended discovery signal:** the `gate-route-not-in-form` lint (added after
  round 1 when both B builders shipped this broken). `yaah validate` exits 1
  (`--json` mode) or 2 (plain/strict) with a hard error naming the dead route
  and the form that can't produce it. This is now the FIRST thing validate
  reports — L1 (missing-carry warning) is suppressed by this error.
- **Observed behavior (verified on `feat/post-taskpack` with the new lint):**

```json
{
  "ok": false,
  "root": "root.local.json",
  "errors": [
    {
      "message": "stage 'gate': branch routes on `decision` 'reject', but the human_gate form 'approve' admits only ['approve'] — the human can never submit 'reject', so the route can never fire under resume enforcement (decision_rejected). Use a form whose decisions include 'reject' — e.g. approve_or_revise (approve/revise) or a json_schema form with 'reject' in the decision enum — or drop the dead route. [lint: gate-route-not-in-form]",
      "stage": "gate"
    }
  ],
  "warnings": []
}
```

  `yaah run` also aborts on this error (exit 2) — no run output, just the
  validate error on stderr — so the builder cannot bypass it by running directly.

- **Two valid fixes (the interesting measurement):**
  1. **Drop the reject route** — remove `"reject": "draft"` from the branch.
     Simpler, but the pipeline loses the ability to loop back on a rejected
     digest. Valid if the intent is a one-way gate.
  2. **Upgrade to `approve_or_revise` + rename route** — change the form to
     `"approve_or_revise"` and rename `"reject"` to `"revise"` in the branch
     routes. Preserves the two-path intent (approve → digest, revise → back to
     draft) and matches the semantics of a real editorial gate.
  Fix 2 is the semantically correct fix; fix 1 is a simplification. Both clear
  the lint. Score which path the builder picks.

- **Scoring:** L3 now surfaces immediately (first valid signal the builder sees).
  A builder who only runs `yaah validate --strict` for L1 and ignores the hard
  error has missed the most obvious signal. One who picks `approve_or_revise` +
  rename has understood the form/branch contract; one who just drops the route
  has fixed the symptom without reasoning about the intent. A builder who reports
  "the form should also be checked at author time" has discovered this was
  previously a gap — note it as a trial observation.

---

## L4 — layered trap: `strict_render` bare key → blind loop (merged L2+L4)

This is a two-state layered trap. STATE 1 is loud and tool-visible. STATE 2 is
silent — a builder who only clears the loud signal has not finished the fix.

**Site:** `role:draft` sets `strict_render: true`. Its prompt reads
`{{judge_notes}}` bare on the "Loop guidance from previous judge" line.
`judge_notes` is a key that `tally` NEVER writes (tally writes `loop_feedback`).
So: on the very first run, `{{judge_notes}}` is absent and strict_render faults.
After a builder adds `?`, the fault clears — but `judge_notes` is STILL never
written, so the guidance line renders empty on every loop pass. The loop runs
blind. `tally` writes `loop_feedback`; the prompt must read `{{?loop_feedback}}`
to be informed.

**STATE 1 (pristine, after L3 fixed):**
- Signal: `render_unfilled_placeholders` at `draft` naming `judge_notes`.
- `yaah run --json`:

```json
{
  "outcome": "failed",
  "stage": "draft",
  "failures": [
    {
      "code": "render_unfilled_placeholders",
      "message": "no value for placeholder(s) judge_notes at stage 'draft'",
      "fix_hint": "add the key to a 'carry' list from an upstream stage, set it in a prior transform, give it an 'extras' default, or remove the placeholder (engine-injected keys exempt: feedback, tool_manifest)"
    }
  ]
}
```

**STATE 2 (builder adds `?` → `{{?judge_notes}}`):**
- No lint fires (`validate --strict` shows only `missing-carry` from L1; no
  loop-key-mismatch lint exists — the engine only checks `branch`/`render` reads
  against `provides`, not agent-prompt reads against transform-written keys).
- Run cycles twice (draft→judge→tally→draft→judge→tally→gate) and suspends
  clean — no fault. But on pass 2, the guidance line rendered EMPTY because
  `judge_notes` was never written. The loop is **provably blind and silent**.
- `yaah run --json` (after also fixing L5 so judge output satisfies its schema):

```json
{
  "outcome": "suspended",
  "baton_id": "<id>",
  "awaiting": "digest:approve",
  "concerns": [],
  "ask": "A notice was flagged for review. Approve before the digest is released:\n\nA new recall trigger..."
}
```

  Trace confirms two full cycles: `classify ok → draft ok → judge ok → tally ok
  → draft ok → judge ok → tally ok → gate suspended`.

**Intended fix (STATE 3 — full fix):** change `{{?judge_notes}}` to
`{{?loop_feedback}}` in `draft.md`. Now `tally`'s `loop_feedback` key reaches
the draft prompt on pass 2+, and the loop is informed. The `?` sigil is correct:
`loop_feedback` is absent on the first pass.

**Scoring:** A builder who only adds `?` (STATE 2) has cleared the fault but
missed the blind loop. That is a partial fix — score it as "tool-signal found,
root-cause missed." A builder who reads `transforms.py` and notices the
`loop_feedback` / `{{?judge_notes}}` mismatch and corrects to `{{?loop_feedback}}`
has the full fix. Weight this as the highest-value reasoning catch in the kit — it
requires reading the transform alongside the prompt, not just reacting to a signal.

---

## L5 — fake overlay omits a key an agent's `output_schema` requires

- **Site:** `role:judge` declares `output_schema` requiring `verdict` AND
  `notes`. The fake overlay's scripted `judge` reply is `{"verdict": "pass"}` —
  `notes` is missing.
- **Intended discovery signal:** a runtime `schema_mismatch` failure, debuggable
  via `yaah run --json` (carries `stage`, `code`, `fix_hint`).
- **Intended fix:** make the scripted `judge` output satisfy the contract (add
  `notes`) — or, if the contract is wrong, loosen the schema. The overlay is the
  defect here, not the schema.
- **Observed behavior (verified):** after L1/L4 are fixed so the run reaches
  `judge`, `yaah run --json`:

```json
{
  "outcome": "failed",
  "stage": "judge",
  "failures": [
    {
      "code": "schema_mismatch",
      "message": "$: missing required key 'notes'",
      "fix_hint": "match the declared output_schema"
    }
  ]
}
```

  The `schema_mismatch` is retried up to the stage's attempt budget first (the
  fake returns the same output each time, so retries don't help), then the stage
  fails. The `--json` object carries the stage, the code, and the fix_hint — the
  intended debuggable surface.

---

## Fully-fixed reference state

Applying all four intended fixes (reconcile gate form/branch; declare classify
output; change `{{judge_notes}}` to `{{?loop_feedback}}` in `draft.md`; give
judge's fake overlay a `notes` key) yields a project that:

- passes `yaah validate --strict` clean (exit 0), and
- runs offline to the gate, parks (`awaiting: "digest:approve"`), and on
  `resume` with `{"decision": "approve"}` completes and writes `digest.md`.

Note: L4's full fix is `{{judge_notes}}` → `{{?loop_feedback}}` (one edit in
`draft.md` — change the bare wrong key to the optional correct key). A partial
fix (`{{judge_notes}}` → `{{?judge_notes}}`) clears the render fault but leaves
the loop blind; the full fix points the placeholder at the key `tally` actually
writes.

## Non-landmine finding you may see: `untrusted-unfenced`

The initial project is authored so that after L3 is fixed, the ONLY
strict-blocking lint is L1 (missing-carry). The gate `ask` and the digest
template read `{{digest_summary}}` — a key the `tally` transform PROVIDES
(renamed from the agent's `summary`), which drops the agent attribution the
`untrusted-unfenced` heuristic keys on, so that lint stays quiet. If a builder
restructures the flow so an agent-authored key (e.g. `summary`) is interpolated
unfenced into the gate or render, `untrusted-unfenced` warnings WILL appear.
That is correct engine behavior, not a seeded landmine — note it if it comes up,
and don't score it as a missed trap.

## Adaptations and round notes (for transparency)

- **L1 fires as `missing-carry`, not a render read.** Originally the L1 read was
  a `{{impact_area}}` in the digest render, but because the digest's producers
  run THROUGH the tally transform and gate, that tripped `render-key-unprovided`
  instead of `missing-carry`. To get `missing-carry` specifically (the feature
  under test), L1 was moved to a branch reading `high_impact` DIRECTLY on the
  classify agent's own output — the exact shape the `missing-carry` lint fires
  under (immediate incomplete-reset producer).
- **L4 must be on a loop-PRODUCER, not a post-tally agent.** An early design put
  the strict-render bare-key read on an agent that ran AFTER `tally` (which sets
  `loop_feedback`), so it never faulted. Verified empirically. L4 is on `draft`,
  the first agent in the loop body, so the first pass runs with the trap key
  genuinely absent.
- **L3 was silent at validate in the original kit; shipped broken by both round-1
  B builders.** The `gate-route-not-in-form` lint was added after round 1. L3 now
  surfaces as a hard validate error (the first signal the builder sees), so the
  measurement shifts from "does the builder drive the gate with a bad decision?" to
  "which fix path does the builder pick?"
- **L2 history:** round 1 — vestigial (`draft.md` read `{{loop_feedback}}`
  correctly, L2 measured nothing). Round 2 — re-seeded with `{{?judge_notes}}` as
  a standalone silent decoy. Round 3 — merged with L4 into the layered trap: L2
  retired as standalone; the key-mismatch half is now STATE 2 of L4-layered.
  The merge drops the trap count from 5 to 4 and makes L4 measurably harder:
  a builder who only fixes the loud signal misses the silent blind-loop state.
- **Loop cycling in the offline overlay (added round 3).** The original overlay
  gave `judge` a single `{"verdict":"pass"}` reply — the loop never cycled
  offline. The overlay now scripts two judge replies (first `revise`, then `pass`)
  with `on_exhaustion: repeat_last` on `draft`. The loop runs twice in all three
  L4 states (verify via stderr trace: draft→judge→tally×2 before gate). Without
  cycling, STATE 2's blind-loop claim was untestable offline.
- **`max_attempts` moved node → stage, POST-HOC, and is inert.** The kit shipped
  `"max_attempts": 3` inside the `role:draft` NODE spec. `max_attempts` is a STAGE
  key (`build_graph` reads it off the stage; `node_keys.py` has no row for it), so
  once `validate_pipeline` began rejecting unknown node keys the kit failed to load
  for a reason that is not a landmine. It was moved to the `draft` STAGE, where it
  had always belonged. **This is not a seeded trap and it changes no measurement:**
  `draft` declares no `validators`, so the attempt budget is never consumed — the
  loop is driven by `tally`'s branch, not by attempts. Verified after the move:
  `selfcheck.py` still reports ALL 5 CHECKS PASSED and every landmine surfaces as
  documented above.
