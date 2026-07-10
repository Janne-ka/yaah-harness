# rollback-demo — ADR-0008 rollback capability

A 3-stage offline pipeline that demonstrates YAAH's rollback function
([ADR-0008](../../docs/decisions/0008-rollback-separate-function.md)). Each
stage writes a file (the committed external effect) and records the file path
as the effect descriptor. The `yaah rollback` verb then walks the completed
work in reverse, reporting honestly: one cheap candidate, one costly candidate,
and one stage with no rollback declared (impossible — the node author didn't
supply undo logic).

## Files

| File | Purpose |
|---|---|
| `rollback-demo-pipeline.json` | 3-stage pipeline: write_alpha → write_beta → write_gamma |
| `rollback-demo.local.json` | Root config; **file trace sink declared** (required by ADR-0008 D2) |
| `input.json` | Task payload |
| `undo.py` | `fn:` targets: write_* (transform nodes) + delete_* (rollback targets) |

## The three nodes

```
write_alpha  rollback: {target: fn:undo:delete_alpha, cost: "cheap"}
write_beta   rollback: {target: fn:undo:delete_beta,  cost: "costly"}
write_gamma  (no rollback declared → appears as impossible)
```

Each stage declares `effects_from: "effect"` so the harness copies
`payload["effect"]` — the `{"file": "<absolute-path>"}` descriptor — onto the
trace span at completion. That descriptor is what `--execute` hands to the
rollback target as `ctx["effects"]`.

## Step 1 — run the pipeline

```
$ cd examples/rollback-demo
$ PYTHONPATH=../../src python3 -m yaah.runtime rollback-demo.local.json
```

```
RESULT: Done(output=Envelope(...), baton_id='...')
```

The corr-id is printed in the output's `correlation_id` header (or use
`yaah rollback rollback-demo.local.json` with no corr to list runs — see Step 2).

Verify the external effects were committed:

```
$ ls *.txt
alpha.txt  beta.txt  gamma.txt
```

## Step 2 — inspect the rollback menu (dry run, calls nothing)

```
$ yaah rollback rollback-demo.local.json
```

Lists all runs with rollback candidates:

```
runs with rollback candidates:
  <corr-id>  (2 candidates)

Inspect one:  yaah rollback <root> <run>
```

Show the menu for a run:

```
$ yaah rollback rollback-demo.local.json <corr-id>
```

```
rollback menu for run <corr-id>
  (dry run — nothing is called; add --execute to undo)

  [1] stage write_beta  node write_beta  cost costly
      effects: {"file": "/…/rollback-demo/beta.txt"}
      ran after: write_gamma
  [2] stage write_alpha  node write_alpha  cost cheap
      effects: {"file": "/…/rollback-demo/alpha.txt"}
      ran after: write_beta, write_gamma

impossible (no rollback declared — treated as irreversible):
  - stage write_gamma  node write_gamma
```

Candidates are in reverse file-append order (JSONL line index, NOT `t_start` —
process-local monotonic times are unreliable across a resume; see ADR-0008 D3).
Each candidate shows stage, node, declared cost, the recorded `effects` descriptor,
and the stages that ran after it — so you can judge dependency risk without the
engine guessing domain semantics.

## Step 3 — execute (cheap only, default)

```
$ yaah rollback rollback-demo.local.json <corr-id> --execute
```

```
rollback report for run <corr-id>
  rolled back     : write_alpha#1
  skipped (costly): write_beta#1
  impossible      : write_gamma (node write_gamma)
  failed          : (none)
  not attempted   : (none)
```

After this: `alpha.txt` is gone. `beta.txt` and `gamma.txt` remain.

## Step 4 — include costly, finish the job

```
$ yaah rollback rollback-demo.local.json <corr-id> --execute --include-costly
```

```
rollback report for run <corr-id>
  rolled back     : write_beta#1, write_alpha#1
  skipped (costly): (none)
  impossible      : write_gamma (node write_gamma)
  failed          : (none)
  not attempted   : (none)
```

After this: `alpha.txt` and `beta.txt` are gone. `gamma.txt` remains — the
`impossible` outcome is a first-class, honest report, not an error.

## Step 5 — one stage at a time (operator mode)

```
$ yaah rollback rollback-demo.local.json <corr-id> --execute --only write_alpha
```

`--only` (repeatable) restricts execution to the named stage(s). It addresses
ALL occurrences of that name when a stage ran multiple times (loop). `--accept-partial`
continues past a failed undo (default: stop on first failure so half-undone state
is never silent).

## Key design points (ADR-0008)

- **`clear` is NOT rollback.** `clear`/`compensate` are unchanged; rollback is a
  third, separate function with a separate verb. Never describe one as another.
- **`effects_from` is a STAGE key** (in `graph.stages.<name>`), wired like
  `concerns_from`. It names the payload key whose value is copied to the trace span
  at stage completion as attr `effects`. Rejected on `fork`/`fanin` stages.
- **Rollback targets receive `{correlation_id, stage, node, effects, cost}`** — NOT
  the stage's full payload. A `compensate` fn (which receives the full payload) is
  NOT drop-in reusable as a rollback target; reusing one silently reads absent keys.
- **The file trace sink is mandatory — under the default `mode: "tracer"`.**
  `rollback-demo.local.json` declares it. Any pipeline with a `rollback:` node
  that lacks `sinks: [{type: "file", ...}]` — or declares one beside
  `mode: "none"`/`"envelope"` (those modes never persist sinks) — is rejected
  at `yaah validate` time AND at `yaah run` time.
- **Report shape:** `{run, rolled_back, skipped_costly, impossible, failed, not_attempted}`.
  `impossible` is a first-class outcome (not an error). The report in Steps 3 and 4
  demonstrates all five buckets.
- **List runs:** `yaah rollback rollback-demo.local.json` (no corr-id) lists all
  runs in `trace.jsonl` that have rollback candidates.
- **Non-goals (v1):** no automatic saga on failure; no dependency-aware ordering
  (reverse completion + what-ran-after is the full ordering story); no
  rollback-of-rollback tracking (undo targets should be idempotent); no
  transactional guarantee (`--accept-partial` is the explicit partial-unwind
  consent).
