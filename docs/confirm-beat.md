# The confirm beat — a second opinion at the done-boundary

## The failure class

An agentic loop exits at the **first plausible-looking success**. A stage runs, its
deterministic validators pass, and the harness commits the Pass and advances — even
when the output is *valid-but-wrong*. Deterministic validators answer "is this output
well-formed / structurally acceptable?", not "is this actually the right answer to the
task?". A JSON blob with all its required keys, tests that compile, a diff that applies
— each passes its validator and reads as *done* while quietly being wrong. There is no
structural beat that says **"halt and check"** before the loop treats a first draft as
finished.

## The beat

`confirm` is an opt-in stage field: the name of a **checker role** — a cheap agent node
— dispatched at the stage's done-boundary.

```
produce ─▶ deterministic validators ─▶ [validators PASS]
                                              │
                                              ▼
                                        confirm declared?  ──no──▶ commit Pass, advance
                                              │yes
                                              ▼
                                   dispatch the confirm role
                                   (OUTPUT + pristine task, COLD)
                                              │
                              ┌───────────────┴────────────────┐
                        {ok:true}                         {ok:false, reason}
                              │                                 │
                        commit Pass, advance          a failed Verdict, reason=feedback
                                                                │
                                                    ── the EXISTING retry loop ──
                                                    retry-with-feedback / escalate / fail
```

The checker returns a minimal structured verdict:

```json
{"ok": true,  "reason": "why it's acceptable"}
{"ok": false, "reason": "the specific defect the producer must fix"}
```

- `ok: true` → the Pass stands, the harness advances (the common path).
- `ok: false` → mapped onto a `Verdict.failed` whose failure message **is** the
  `reason`, and handed to the loop that already exists. It is treated *exactly* like a
  hard validator failure: it spends an attempt, the reason is folded back as
  retry feedback, and on `max_attempts` exhaustion it escalates (`escalate: human` →
  park) or fails (`StageFailed`) — the same terminal behavior a validator failure has
  today.
- A checker that crashes or returns a non-`{ok, reason}` reply is a **misconfiguration**,
  surfaced loud as a failed Verdict (code `confirm_malformed`) through the same loop —
  never silently treated as a pass.

## Reusing the existing loop (not a parallel mechanism)

The whole point is that the beat adds **no new retry/escalate/park path**. It slots one
call into the single shared attempt loop (`Harness._run_attempts`), between "validators
passed" and "return the Pass":

```
verdict, soft = await self._validate(stage, out)      # deterministic validators
if verdict.ok and stage.confirm:
    verdict = await self._confirm(stage, task, out)    # ← the beat: may replace verdict
if verdict.ok:
    return _Pass(out, soft)
# ...unchanged below: transient budget, attempt++, escalate-human, StageFailed, feedback
```

`_confirm` **only** returns a `Verdict`. It never raises, never parks, never counts an
attempt itself. A rejection is just a `verdict` that is not `ok`, so control falls into
the identical policy block that a validator failure falls into. This is why the property
below labelled BOUNDED is not separately engineered — it *falls out of* reusing the loop
that is already bounded by `max_attempts`.

Two engine details keep the reuse honest:

- **The veto is exempt from the content-sniffing transient classifier.** The loop has a
  *separate* `error_retries` budget for transient infra faults, gated by
  `_is_transient_verdict`, which sniffs failure TEXT for infra words ("timeout", "rate
  limit", "503"). The confirm `reason` is **agent-authored free text**, so a substantive
  veto that merely mentions such a word would be misread as an infra blip — riding the
  error budget (spending no `max_attempts` attempt, folding no feedback) and silently
  tripling producer cost. `confirm_rejected` and `confirm_malformed` are therefore
  exempted (early-return `False`), exactly as `foreach_error` is — the same
  looks-like-X-treat-as-X trap this feature exists to close. A confirm PROVIDER that is
  genuinely down replies `Kind.ERROR` (code `node_error`), which stays transient so it
  still rides the error budget.
- **Every dispatch emits one `confirm` trace note** (pass or veto), so the second agent's
  cost/latency is visible even on the happy path — the CHEAP property invites a cost
  surprise, and a hidden second call is how it would bite.

## The five properties

| # | Property | How it holds |
|---|----------|--------------|
| 1 | **CHEAP** | The checker is a normal agent node; its model (intended: a cheap/haiku-class model) and knobs are that node's own `NodeConfig`. The engine dispatches to the named role via `comms.request` and threads the node's config through untouched — it never hardcodes a model nor reuses the producer's. |
| 2 | **COLD** | The checker's input is built by `_confirm_context` from the ENVELOPE alone: the node OUTPUT unioned with the **pristine** task (the stage input captured *before* the retry loop folds in feedback). The producer's self-correction scratch — its rejected `priorAttempt` draft and the validator `feedback` critique — is stripped and never reaches the checker. A second opinion that reads the first opinion's discarded drafts is not independent. |
| 3 | **ADVERSARIAL** | The checker's prompt is **app-supplied markdown**, carried by the referenced agent node exactly as every other agent's prompt is. The engine only names the role and dispatches it; it takes no view of what the prompt says. Markdown, not code. |
| 4 | **BOUNDED** | A checker that keeps returning `ok:false` terminates at `max_attempts` → escalate/park (or `StageFailed`), never loops forever — because it feeds the already-bounded loop. It is not a separate mechanism that could spin. |
| 5 | **OPT-IN** | No `confirm` declared → the beat is not entered, nothing is dispatched, behavior is byte-identical to today. |

## Schema

`confirm` is a stage-level field: a non-empty string naming a **declared `agent` node**
(the same resolution rule as a `validators` entry, restricted to agent type — only an
agent returns the free-form `{ok, reason}` contract; a transform/shell node is refused
at load). Its model and prompt live on that node.

Illustrative stage fragment (the checker node is declared in the pipeline's `nodes`
alongside the producer):

    "review": {
      "node": "role:writer",
      "validators": ["role:schema-check"],
      "confirm": "role:second-opinion",
      "max_attempts": 3,
      "feedback": true,
      "escalate": "human"
    }

`validate_pipeline` enforces, at load time:

- `confirm` must be a non-empty string (a typo'd empty is not a silent skip);
- the role must resolve to a declared node, and that node must be `agent`-typed (a
  transform/shell/etc. can't return the `{ok, reason}` contract, so it would park every
  confirm as `confirm_malformed` — refused loud at load instead);
- `confirm` is **rejected on a fork/fanin stage** — those complete on the fork
  coordinator's separate no-output path, never through the attempt loop where the beat
  runs, so a confirm there would silently never fire.

The beat applies to single-node, `fanout`, and `foreach` stages — every shape that
flows through `_run_attempts`.

## Design note: why a distinct beat and not an "agent-backed validator"

A tempting alternative is to make the checker just another entry in `validators: [...]`
— an agent that returns a `Verdict` — reusing even the validator dispatch. It was
rejected for three concrete reasons:

1. **Cold context.** A `validators` entry receives only the OUTPUT envelope, and
   receives it whether or not the deterministic validators passed. The confirm beat is
   defined to run *after* the cheap deterministic gates pass (don't pay for a model
   second-opinion on an output a free check already rejected) and to see the OUTPUT **+
   the pristine task** with producer scratch stripped. That context shaping is the
   feature; a plain validator slot cannot express it.
2. **Structural signal.** The engine already relies on "only a checker replies
   `Kind.VERDICT`" to catch an agent wrongly listed in `validators:`. Blurring an agent
   into a validator would erode that check. The confirm agent instead returns ordinary
   `{ok, reason}` output and the engine maps it — keeping the checker/agent boundary
   crisp.
3. **Ordering intent.** `confirm` names *the* done-boundary second opinion, one per
   stage, semantically distinct from the ordered list of cheap-first structural gates.
   Collapsing them would lose the "this is the halt-and-check beat" intent at the
   config surface.

The cost of the distinct beat is one new stage field and ~40 lines in the harness — and
it still reuses the entire retry/escalate/park machinery, which is where the real
complexity (and the real risk of a parallel mechanism) would have been.
