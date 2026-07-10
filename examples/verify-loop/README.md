# verify-loop — bounded cross-stage loop with best-of-N

Demonstrates: routing the envelope **backward** across stages on a branch, tracking
a payload counter to bound the loop, and reducing to the **best attempt** rather than
the last one.

The production use case is any generative–evaluate–improve loop: write a draft, score
it, feed the score back, repeat — but stop at a bound and ship the best-scoring run,
not whatever the last iteration happened to produce.

```
produce (agent)
   ↓
judge (agent: score + notes)
   ↓
tally (transform: increment cycle, update best if score > best_score)
   ↓ loop_done="no" → produce   (backward branch: back to top)
   ↓ loop_done="yes" → emit_best
emit_best (transform: clean final payload)
```

## Run it (offline, no API key)

```
$ PYTHONPATH=../../src python3 -m yaah.runtime verify-loop.local.json
[trace] stage produce ok (1ms)
[trace] stage judge ok (1ms)
[trace] stage tally ok (2ms)
[trace] stage produce ok (1ms)
[trace] stage judge ok (1ms)
[trace] stage tally ok (0ms)
[trace] stage produce ok (1ms)
[trace] stage judge ok (1ms)
[trace] stage tally ok (0ms)
[trace] stage emit_best ok (0ms)
RESULT: Done(output=Envelope(kind='result', payload={
  'best_artifact': 'A gilded crown of fire sank behind the hills, washing the world in amber.',
  'best_score': 7,
  'total_cycles': 3
}, ...))
```

Three `produce → judge → tally` cycles ran. Scores were:

| Attempt | Artifact | Score |
|---|---|---|
| 1 | The sun dropped low, bleeding orange across the horizon. | 3 |
| **2** | **A gilded crown of fire sank behind the hills, washing the world in amber.** | **7** |
| 3 | Evening stole the light, leaving only embers scattered across the clouds. | 5 |

`best_artifact` is attempt 2 (score 7), not attempt 3 (score 5, the last one). Best-of-N beat last-attempt.

The final payload is exactly the three keys `emit_best` projected — `cycle` and
`loop_feedback` do NOT leak in. `graph.sticky` re-folds those loop-state keys onto
every OTHER stage's output (that is how they survive the produce agent's payload-replace
across the loop), and it would normally re-inject them onto `emit_best`'s output too.
The `emit_best` stage is declared **`final: true`**, which tells the harness to skip
the sticky re-fold on that one stage — its payload is the run's final word. See
["The terminal-cleanup stage"](#the-terminal-cleanup-stage-finaltrue) below.
`total_cycles` is the human-readable name for `cycle`.

## How the loop works

**Cycle counter + branch** in `tally`:

The `tally` transform increments `cycle` after each attempt and decides `loop_done`:
```python
cycle = int(p.get("cycle", 0)) + 1
loop_done = "yes" if (score >= pass_threshold or cycle >= max_cycles) else "no"
```

The stage branches on that value:
```json
"tally": {
  "node": "role:tally",
  "branch": {"on": "loop_done", "routes": {"yes": "emit_best"}, "default": "produce"}
}
```

When `loop_done="no"`, the `default` route sends the envelope **back to produce** — a
back-edge in the graph. The harness's `_drive` loop handles it naturally (each stage
transition increments a step counter; the livelock backstop is 10,000 stage transitions).

**Sticky keys** let state survive the produce agent's payload-replace:

`produce` is a parse-mode agent — its output REPLACES the payload with just `{raw, artifact}`.
Without intervention, `cycle`, `best_score`, `best_artifact`, and `feedback` would vanish.

`graph.sticky: ["cycle", "best_score", "best_artifact", "loop_feedback"]` tells the harness
to re-fold those keys from the stage's input into its output whenever they're absent. So after
produce runs, the harness silently adds them back from the previous tally output. The produce
agent never has to carry them explicitly.

Note: the cross-loop feedback key is named `loop_feedback`, not `feedback`. The engine
reserves `feedback` as a special key — when non-empty in the payload, it auto-appends
the value to the agent's rendered prompt as a "FEEDBACK (fix these and try again)"
section. Using `feedback` for cross-loop prose would inject the judge's notes twice.

**Best-of-N tracking** in `tally`:

```python
if score > best_score:
    best_score = score
    best_artifact = artifact
```

`best_artifact` always holds the highest-scoring attempt seen so far. When the loop exits
(cap hit or threshold passed), `best_artifact` IS the best — the `emit_best` terminal just
surfaces it cleanly.

**Judge carry** threads `artifact` one hop:

`judge` has `carry: ["artifact"]` so the current attempt text survives from produce's
output into tally's input (where `tally` reads and compares it to `best_artifact`).
`artifact` is not sticky (it's a per-iteration value, not run-wide state).

## The terminal-cleanup stage (`final:true`)

`graph.sticky` re-folds the loop-state keys onto **every** stage's output — that is
exactly what keeps `cycle`/`best_score`/`best_artifact`/`loop_feedback` alive across the
produce agent's payload-replace. But the same mechanism fights the terminal cleanup:
`emit_best` deliberately projects a tidy three-key payload, and without help sticky would
re-inject `cycle` and `loop_feedback` right back into the Done output.

The fix is a stage key:

```json
"emit_best": {"node": "role:emit_best", "then": null, "final": true}
```

`final: true` tells the harness to **skip the sticky re-fold on this stage's output** —
its payload is the run's final word. It is legal ONLY on a terminal stage (no `then` /
`branch` / `fork` / `fanout` / `fanin` / `foreach`): sticky is the run frame the harness
re-folds on every edge, so only the last word may drop it. `yaah validate` rejects
`final: true` on any stage that still routes onward, naming why.

## The canonical recipe

See [`docs/cookbook/bounded-loop-best-of-n.md`](../../docs/cookbook/bounded-loop-best-of-n.md)
for the full pattern, the honest limits, and when to use this vs single-stage
`max_attempts` + `feedback`.
