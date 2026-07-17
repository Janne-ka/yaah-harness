# Answer key — TRIAL RUNNER ONLY

**Do not give this file to a builder agent.** It is the scoring anchor for the
person running the trial. It lists each seeded landmine in Part B, the signal
the engine is supposed to give, the intended fix, and the ACTUAL engine
behavior verified against the current branch (with real output pasted). If the
engine changes and a landmine stops behaving as documented here, `selfcheck.py`
will fail — update this file and the kit together.

All output below was captured on branch `feat/post-taskpack` with the working
tree's uncommitted engine changes (the `missing-carry` lint, the `{{?key}}`
optional sigil, and `yaah run/validate --json`).

---

## Surfacing order (important for scoring)

The five landmines do not all appear at once — they chain by dependency, which
is realistic and intended. A builder fixes one, reruns, and meets the next:

| When | Surface | Landmine |
|------|---------|----------|
| `yaah validate --strict` (before any run) | `missing-carry` warning | **L1** |
| first `yaah run` | `render_unfilled_placeholders` fault at `draft` | **L4** |
| after L4 fixed, next `yaah run` | `schema_mismatch` fault at `judge` | **L5** |
| at `yaah resume` with `{"decision":"reject"}` | `decision_rejected` (exit 1) | **L3** |
| never fires a signal | silent | **L2** |

L2 produces NO lint and NO fault — it is the pure "runs but wrong" trap. L3 is a
HALFWAY trap: it is silent at validate (no lint cross-checks a branch route
against a form's decision enum), but the engine now enforces the gate's form at
**resume** (N1), so driving the parked gate with the form-forbidden `reject`
value fails LOUD instead of silently taking the dead route. A builder who only
chases validate output still ships the phantom route; one who exercises the gate
meets the `decision_rejected` error and must reconcile the form with the branch.

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

## L2 — retry loop writes `loop_feedback`, prompt reads `{{feedback}}`

- **Site:** `transforms.py::tally` writes the judge's notes under `loop_feedback`
  (the collision-safe name). The `draft` prompt (`prompts/draft.md`) reads
  `{{feedback}}` — the engine-reserved key, not the one tally writes.
- **Intended discovery signal:** NONE. This trap is deliberately doc-only. The
  trial asks whether a "loop-key mismatch" lint SHOULD exist.
- **Intended fix:** point the prompt at the key the loop actually writes
  (`{{loop_feedback}}` — see L4 for the strict-render sigil), and drop the stray
  `{{feedback}}` read (or accept that `{{feedback}}` is the engine's validator-
  retry channel, which this custom loop does not feed).
- **Observed behavior (verified):** no lint fires (validate shows only L1). The
  run does not fault on it either — `feedback` is in the engine's
  `_ENGINE_INJECTED` exempt set, so even under `strict_render` an absent
  `{{feedback}}` renders empty instead of faulting. The loop simply retries
  blind. The scaffolded `AGENTS.md` rule 3 warns about exactly this ("make sure
  the key your loop WRITES is the key your prompt READS — a mismatch silently
  reads nothing"), so the DOC surface covers it; the engine does not.
- **Scoring:** a builder who catches L2 did so from docs/reasoning, not tooling.
  Weight it accordingly. If a builder proposes a "loop-key mismatch" lint, that
  is a valid trial finding to forward to the engine team — not a failure.

---

## L3 — gate `form: "approve"` but the branch has approve AND reject routes

- **Site:** `role:gate` declares `form: "approve"` (a single-button form whose
  only decision value is `"approve"`). The `gate` stage branches on `decision`
  with routes `{"approve": "digest", "reject": "draft"}`. The `reject` route
  references a decision value the form can never produce.
- **Intended discovery signal:** there is no lint that cross-checks branch routes
  against a form's decision enum (confirmed: the full lint inventory has no such
  rule; the `gate-decision-ignored` lint fires only for the OPPOSITE shape — a
  ≥2-outcome form with NOTHING branching on `decision`). So L3 is invisible at
  authoring time. It surfaces only when the gate is EXERCISED with the
  form-forbidden value — where the engine now enforces the form (N1).
- **Observed behavior (verified on `feat/post-taskpack`):**
  1. **Silent at validate.** With only L3 present, `validate --strict` is
     completely clean (no warnings, no errors, exit 0).
  2. **The form IS enforced on resume (N1, TIER-0 fix).** Driving the parked
     gate with a decision the form forbids is REJECTED loud. Submitting
     `{"decision": "reject"}` against the `approve`-only form exits 1 with a
     `decision_rejected` failure — the run does NOT take the dead `reject`
     route (before enforcement, it silently looped back to `draft` and re-parked):

```
$ yaah resume <root> <baton> decision.json --json   # decision.json = {"decision":"reject"}
{
  "outcome": "failed",
  "stage": null,
  "code": "decision_rejected",
  "failures": [
    {
      "code": "decision_rejected",
      "message": "resume decision for baton '...' does not conform to the gate's form 'approve' — $.decision: 'reject' not in enum ['approve'] — fetch `yaah baton-schema` for the required shape and submit a conforming decision, or set `strict_resume: false` in the root config if this gate's form is mis-declared",
      "fix_hint": "fetch `yaah baton-schema` and submit a conforming decision",
      "data": {"form": "approve", "errors": ["$.decision: 'reject' not in enum ['approve']"], "baton_id": "..."}
    }
  ]
}
$ echo $?
1
```

  The rejected gate stays PARKED and re-submittable — a conforming
  `{"decision":"approve"}` then completes the run (takes the `approve` route →
  `digest` → done). So the trap's teaching moment is the `decision_rejected`
  error itself: the builder must find that the `approve` form can never produce
  `reject`.
- **Intended fix:** make the form and the branch agree. Either upgrade the form
  to `approve_or_revise` (a real two-outcome decision) and route on its actual
  values, or drop the phantom `reject` route and keep the single-button form.
  (`strict_resume: false` in the root config is an escape hatch, not the fix — it
  silences enforcement for a gate whose form is genuinely mis-declared.)
- **Scoring:** L3 is silent at authoring but loud at resume. A builder who never
  exercises the gate with a non-`approve` value still ships the phantom route
  (validate gives no help). One who drives the gate meets `decision_rejected` and
  must reconcile the form with the branch. A builder who additionally reports
  "the engine should ALSO lint a route the gate's form can never produce
  (author-time)" has found a real remaining gap — forward it.

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

Applying all five intended fixes (declare classify output; point draft at
`{{?loop_feedback}}` and drop the `{{feedback}}` read; reconcile the gate form
with its branch; give judge's fake output a `notes` key) yields a project that:

- passes `yaah validate --strict` clean (exit 0), and
- runs offline to the gate, parks (`awaiting: "digest:approve"`), and on
  `resume` with `{"decision": "approve"}` completes and writes `digest.md`.

## Non-landmine finding you may see: `untrusted-unfenced`

The initial project is authored so that the ONLY strict-blocking lint is L1.
The gate `ask` and the digest template read `{{digest_summary}}` — a key the
`tally` transform PROVIDES (renamed from the agent's `summary`), which drops the
agent attribution the `untrusted-unfenced` heuristic keys on, so that lint stays
quiet. If a builder restructures the flow so an agent-authored key (e.g.
`summary`) is interpolated unfenced into the gate or render, `untrusted-unfenced`
warnings WILL appear. That is correct engine behavior, not a seeded landmine —
note it if it comes up, and don't score it as a missed trap.

## Adaptations made while building this kit (for transparency)

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
- **L3 turned out to be doubly silent.** Beyond having no lint, the runtime does
  not enforce the gate's declared form against the submitted decision on resume,
  so the phantom `reject` route is actually reachable by an out-of-band decision
  (documented above). This is the real engine behavior, kept as-is.
