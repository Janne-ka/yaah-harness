# Bounded cross-stage loop + best-of-N

Copy this pattern when you need a produce–evaluate–improve loop across
**multiple stages**, want to **cap the total iterations**, and want to
**keep the best attempt** rather than shipping whatever the last iteration produced.

**Reference implementation:** [`examples/verify-loop/`](../../examples/verify-loop/)

---

## The shape

```
produce (agent)
   ↓
judge  (agent or transform: scores attempt, writes score + notes)
   ↓
tally  (transform: increment cycle, update best, decide loop_done)
   ↓ loop_done="no"  → produce   ← backward branch
   ↓ loop_done="yes" → emit_best
emit_best (transform: clean final payload)
```

The graph has a **back-edge**: the `tally` stage's `default` branch route points back
to `produce`. The harness's `_drive` loop handles this naturally — each stage
transition increments an internal step counter (livelock backstop: 10,000 *stage
transitions*, not loop iterations; a 4-stage loop exhausts this at ~2,500 iterations)
so a well-bounded loop runs without engine changes.

---

## The two key payload fields

| Key | Seeded in `input.json` | Updated by | Meaning |
|---|---|---|---|
| `cycle` | `0` | `tally` (post-increment) | How many attempts have completed |
| `best_score` | `-1` | `tally` | Highest score seen across all attempts |
| `best_artifact` | `""` | `tally` | Text of the best attempt |
| `loop_feedback` | `""` | `tally` (set to judge's notes) | Fed back into the next produce prompt |

All four are declared in `graph.sticky` so the harness re-folds them automatically
when the produce agent's payload-replace would otherwise drop them.

```json
"graph": {
  "sticky": ["cycle", "best_score", "best_artifact", "loop_feedback"],
  ...
}
```

**Why `loop_feedback` and not `feedback`?** The engine reserves `feedback` as a
special key for its built-in retry mechanism: when a non-empty `feedback` value is
present in the payload on entry to an `agent` stage, the harness auto-appends it to
the rendered prompt as a "FEEDBACK (fix these and try again)" block
(`agent.py::invoke`, `_ENGINE_INJECTED`). Using `feedback` for your own cross-loop
notes would cause them to appear twice in the prompt — once via `{{feedback}}` in
your template and once via the engine's auto-append. Use any non-reserved name.

---

## The tally transform

```python
def tally(envelope, config):
    p = envelope.payload
    extras = config.extras or {}
    max_cycles    = int(extras.get("max_cycles", 3))
    pass_threshold = int(extras.get("pass_threshold", 8))

    score         = int(p.get("score", 0))
    cycle         = int(p.get("cycle", 0)) + 1      # post-attempt increment
    best_score    = int(p.get("best_score", -1))
    best_artifact = p.get("best_artifact", "")
    artifact      = p.get("artifact", p.get("raw", ""))

    # Guard: catch off-by-one before ScriptedProvider exhausts or tokens burn.
    if cycle > max_cycles:
        raise RuntimeError("cycle {} > max_cycles {} — check branch logic".format(
            cycle, max_cycles))

    if score > best_score:
        best_score    = score
        best_artifact = artifact

    loop_done = "yes" if (score >= pass_threshold or cycle >= max_cycles) else "no"

    return {
        **p,
        "cycle":         cycle,
        "best_score":    best_score,
        "best_artifact": best_artifact,
        "loop_done":      loop_done,
        "loop_feedback":  p.get("notes", ""),   # not "feedback" — see reserved-key note above
    }
```

Configure `max_cycles` and `pass_threshold` in the node's `config:` block:

```json
"role:tally": {
  "type": "transform",
  "call": "envelope",
  "target": "fn:transforms:tally",
  "config": {"max_cycles": 3, "pass_threshold": 8},
  "provides": ["cycle", "best_score", "best_artifact", "loop_done", "loop_feedback"]
}
```

---

## The branch (tally stage)

```json
"tally": {
  "node": "role:tally",
  "branch": {"on": "loop_done", "routes": {"yes": "emit_best"}, "default": "produce"}
}
```

`"default": "produce"` is the back-edge. The harness evaluates it the same way it
evaluates any `then` target — there is no special loop construct.

---

## Why the bound lives in tally+branch, not in `max_attempts`

`max_attempts` is a **single-stage** retry cap: it limits how many times ONE stage can
be re-run after a validator failure. It does not cross stage boundaries.

The cross-stage loop described here is structurally different: the envelope leaves
`produce`, passes through `judge`, and only THEN decides whether to loop back. The
counter in `tally` is the right place to check it because `tally` is the only stage
that has seen both the current score AND the accumulated history.

Use `max_attempts` + `feedback: true` when:
- You want to retry ONE stage (e.g. an agent that keeps emitting malformed JSON).
- The verdict that triggers the retry is a validator (json_object, json_schema, etc.)
  in that stage's `validators` list.
- The retry and the original attempt are functionally the same call.

Use this pattern when:
- Multiple stages are part of the iteration unit (produce + judge + tally).
- You need custom per-iteration state (scores, history, best-so-far).
- Downstream stages must see intermediate results (the judge's notes, the current best).

---

## The best-of-N reduce

`best_artifact` and `best_score` are updated greedily in `tally`:

```python
if score > best_score:
    best_score    = score
    best_artifact = artifact
```

After `emit_best`, the final payload carries the **highest-scoring** attempt, regardless
of when it occurred. The reference run demonstrates this: scores 3, 7, 5 across three
attempts; the winner is attempt 2 (score 7), not attempt 3 (the last).

The `emit_best` stage is declared `final: true` so the tidy projection is the run's
actual final output:

```json
"emit_best": {"node": "role:emit_best", "then": null, "final": true}
```

`graph.sticky` re-folds the loop-state keys (`cycle`, `loop_feedback`, …) onto every
stage's output — that is what keeps them alive across the loop — and would otherwise
re-inject them onto the terminal cleanup's output too. `final: true` tells the harness
to skip the re-fold **on this one stage**, so the Done payload is exactly what
`emit_best` returned (`best_artifact`, `best_score`, `total_cycles`). It is legal only on
a terminal stage (validate rejects it alongside any `then`/`branch`/`fork`/`fanout`/
`fanin`/`foreach`): sticky is the run frame, and only the last word may drop it.

The reduce logic is in the transform, not the engine — swap `score > best_score` for
any custom comparator (e.g. "lowest cost", "passes all heuristics") without changing
the pipeline shape.

---

## Honest limits

**The counter is payload state — a reducer bug silently unbinds the loop.**

If `tally` forgets to increment `cycle`, or the branch condition has an off-by-one,
the loop runs until the harness's 10,000-step livelock backstop fires (a confusing
failure message). The guard rail in the tally transform above (`cycle > max_cycles →
raise`) catches this before it compounds. The lint cannot check loop termination —
it does not trace execution paths.

**`best_artifact` is only as good as the scoring function.**

Greedy by numeric score is the simplest compare, but a noisy judge (e.g. a real LLM
that inconsistently assigns 7s) can crown the wrong attempt. Use a deterministic
judge (rule-based transform, shell_check, or a conservative scoring rubric) when the
selection matters.

**Loop state is payload — it persists exactly as far as the baton does.**

`cycle`, `best_score`, `best_artifact` are ordinary payload keys. They survive
process restarts if the baton is persisted: `state: {type: "file"}` (durable) keeps
them; `state: {type: "memory"}` (the example's default) loses them on exit.
`graph.sticky` is a runtime re-fold mechanism, not a durability layer — it fires
every stage to prevent a payload-replacing agent from silently dropping these keys,
but it does not change WHERE they are stored. If you add a `human_gate` inside the
loop, configure file-backed state so the cycle count and best candidate survive the
suspension. See [`docs/cookbook/deploy.md`](deploy.md).

Because sticky fires every stage, it also re-injects the loop-state keys into a
terminal cleanup stage's tidy output — declare that stage `final: true` to skip the
re-fold and make its projection the run's actual final word (see "The best-of-N
reduce" above). `final` is legal only on a terminal stage.

**ScriptedProvider scripts must have at least `max_cycles` entries.**

For offline testing, the `fake_scripted` provider advances through its `by_model`
sequence and silently returns `""` (the default) once exhausted. An off-by-one in
`max_cycles` means a silent empty reply is parsed and scored, not a loud error.
Keep the guard rail in `tally` and ensure the script length matches `max_cycles`.

---

## Checklist for adapting this recipe

1. Seed `cycle`, `best_score`, `best_artifact`, `loop_feedback` (and any other
   loop-state keys) in `input.json` with their zero values.
2. Add them to `graph.sticky`.
3. Add `carry: ["artifact"]` (or whichever per-iteration value the judge and tally need)
   to the judge node — `carry` threads ONE key one hop; `sticky` threads MULTIPLE keys
   across the whole loop.
4. Avoid naming your cross-loop feedback key `feedback` — see the reserved-key note in
   the "two key payload fields" section.
5. Verify `tally`'s guard rail (`cycle > max_cycles → raise`) covers your loop bound.
6. Run with the `fake_scripted` provider first — script enough entries for `max_cycles`
   calls each, and verify the final `best_artifact` is the highest-scoring attempt, not
   the last.
