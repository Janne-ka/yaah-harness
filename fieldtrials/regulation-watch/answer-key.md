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

---

## Surfacing order (important for scoring)

The five landmines do not all appear at once — they chain by dependency, which
is realistic and intended. A builder fixes one, reruns, and meets the next:

| When | Surface | Landmine |
|------|---------|----------|
| `yaah validate` (before any run) | `gate-route-not-in-form` **ERROR** | **L3** |
| after L3 fixed, `yaah validate --strict` | `missing-carry` warning | **L1** |
| after L1 fixed (or run despite warning), `yaah run` | `render_unfilled_placeholders` at `draft` | **L4** |
| after L4 fixed, next `yaah run` | `schema_mismatch` at `judge` | **L5** |
| never fires a signal | silent | **L2** |

L3 now surfaces FIRST as a hard error — the `gate-route-not-in-form` lint
(added after round 1) fires at `yaah validate` (exit 1 in `--json` mode, exit 2
in plain/strict). L2 produces NO lint and NO fault — it is the pure "runs but
wrong" trap. The interesting L3 measurement is now WHICH FIX the builder
picks: upgrade the form to `approve_or_revise` + rename the route (preserving
the loop-back intent), or just delete the phantom route (simpler but drops the
reject path entirely).

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

## L2 — loop writes `loop_feedback`, prompt reads `{{?judge_notes}}`

- **Site:** `transforms.py::tally` writes the judge's notes under `loop_feedback`.
  The `draft` prompt (`prompts/draft.md`) reads `{{?judge_notes}}` (the `?` sigil
  makes it optional) on its "Loop guidance from previous judge" line. `judge_notes`
  is a key `tally` never writes, so on every loop pass (pass 2+) the guidance line
  renders **empty** — the model sees no feedback from the previous judge.
- **Why the `?` sigil matters:** an optional placeholder (`{{?key}}`) renders
  empty when the key is absent instead of faulting. The lint does NOT check whether
  agent-prompt reads match transform-written keys — it only checks `branch`/`render`
  reads against `provides`. So this trap fires NO lint and NO fault.
- **Intended discovery signal:** NONE. The builder must read `transforms.py` and
  compare what `tally` writes (`loop_feedback`) against what `draft.md` reads
  (`{{?judge_notes}}`), or reason that "loop guidance from previous judge" should
  be the judge's notes forwarded by `tally`.
- **Proof the trap is real:** on a revise loop, `tally` writes `loop_feedback`
  (line 47 of `transforms.py`). The `draft` prompt's "Loop guidance from previous
  judge" line reads `{{?judge_notes}}`. `judge_notes` is never set anywhere in the
  pipeline — so the rendered prompt on pass 2 has an empty guidance line regardless
  of how many cycles run.
- **Intended fix:** change `{{?judge_notes}}` to `{{?loop_feedback}}` in `draft.md`
  — point the prompt at the key `tally` actually writes. The `?` sigil is correct:
  `loop_feedback` is absent on the first pass (L4 is the trap for forgetting that).
- **Observed behavior (verified):** no lint fires after L3 is fixed (validate
  shows only L1 as a warning). The `{{?judge_notes}}` read is exempt from
  `render_unfilled_placeholders` because of the `?` sigil. The `missing-carry` lint
  does not fire on agent-prompt reads (it only checks branch/render). The loop runs
  blind on every revise cycle.
- **Scoring:** a builder who catches L2 did so from reading transforms.py /
  draft.md side by side — not from any tool signal. Weight it as a high-value
  reasoning catch. If a builder proposes a "loop-key mismatch" lint ("warn when an
  agent prompt reads a key no upstream transform writes"), that is a top-tier
  trial finding — forward it to the engine team.

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

## L4 — `strict_render: true` agent reads `{{loop_feedback}}` bare

- **Site:** `role:draft` sets `strict_render: true` and its prompt reads
  `{{loop_feedback}}` bare. `draft` is the first stage in the loop body, so on
  the first pass `loop_feedback` has never been written by `tally` yet.
- **Intended discovery signal:** a `render_unfilled_placeholders` fault at the
  `draft` stage on the very first run, surfaced structurally via `yaah run --json`.
- **Intended fix:** the `{{?loop_feedback}}` optional sigil — an absent optional
  placeholder renders empty instead of faulting, so the agent stays
  strict-clean AND still faults on a genuinely-missing REQUIRED key.
- **Observed behavior (verified):** first `yaah run --json`:

```json
{
  "outcome": "failed",
  "stage": "draft",
  "failures": [
    {
      "code": "render_unfilled_placeholders",
      "message": "no value for placeholder(s) loop_feedback at stage 'draft'",
      "fix_hint": "add the key to a 'carry' list from an upstream stage, set it in a prior transform, give it an 'extras' default, or remove the placeholder (engine-injected keys exempt: feedback, tool_manifest)"
    }
  ]
}
```

  Applying `{{?loop_feedback}}` clears the fault; the run then advances to
  `judge` (where L5 waits). Note the strict-render fault happens DURING render,
  before the model call, so it masks anything downstream on the same agent.

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

Applying all five intended fixes (reconcile gate form/branch; declare classify
output; point draft at `{{?loop_feedback}}` for the real loop guidance AND fix
`{{?judge_notes}}` to `{{?loop_feedback}}`; give judge's fake output a `notes`
key) yields a project that:

- passes `yaah validate --strict` clean (exit 0), and
- runs offline to the gate, parks (`awaiting: "digest:approve"`), and on
  `resume` with `{"decision": "approve"}` completes and writes `digest.md`.

Note: L4's fix is `{{loop_feedback}}` → `{{?loop_feedback}}` (add the `?` sigil
on the bare `{{loop_feedback}}` line); L2's fix is `{{?judge_notes}}` →
`{{?loop_feedback}}` (point the decoy line at the key tally actually writes).
Both edits are in `draft.md`.

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
  the strict-render `{{loop_feedback}}` read on an agent that ran AFTER `tally`
  (which sets `loop_feedback`), so it never faulted — `loop_feedback` was always
  present by then. Verified empirically (the run suspended cleanly). L4 was moved
  onto `draft`, the first agent in the loop body, so the first pass runs with
  `loop_feedback` genuinely absent.
- **L3 was silent at validate in the original kit; shipped broken by both round-1
  B builders.** The `gate-route-not-in-form` lint was added after round 1. L3 now
  surfaces as a hard validate error (the first signal the builder sees), so the
  measurement shifts from "does the builder drive the gate with a bad decision?" to
  "which fix path does the builder pick?"
- **L2 was vestigial in round 1** — `draft.md` read `{{loop_feedback}}` correctly.
  Re-seeded this round with `{{?judge_notes}}` as the decoy key on the loop-guidance
  line. The `?` sigil makes it optional (no fault, no lint), so the trap is
  genuinely silent again.
