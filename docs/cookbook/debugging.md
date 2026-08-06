# Debugging a YAAH pipeline

The playbook for when something isn't right. Six commands, in roughly the
order the operator should reach for them.

The principle: **never run a real-mode pipeline to discover what's wrong**.
Every check below runs offline against the trace + config + state already on
disk. If the answer requires another real-model call, that's a tell — the
trace/state didn't capture enough.

## 1 — `yaah doctor` first when something feels off about the environment

Before anything else: confirm the install isn't broken.

```bash
yaah doctor
```

Checks Python version, which optional deps are importable
(`litellm` / `nats` / `langfuse` / `httpx`), and that the packaged base
configs (`yaah:bases/*.json`) resolve. Exits 1 on hard install problems —
the wheel was built without package-data, Python is too old, etc.

Use when: `pip install` just happened, the container image just changed,
"this worked yesterday."

## 2 — `yaah validate` when load-time things are suspicious

```bash
yaah validate root.json
```

Loads + validates the root + the referenced pipeline file. Catches:

- unknown top-level keys (with `did you mean` hints)
- wrong shape on `transport` / `state` / `providers` / `trace`
- malformed JSON in either file (the failing file is named in the error)
- unresolved `then` / `branch` / `fork` / `fanin` graph targets
- nodes that reference a `_extends` overlay key the base pipeline doesn't
  declare (stale-overlay-key bug)

Use when: a fresh pipeline isn't running, a hand-edited config is
suspicious, a graph rewrite might have left a stale reference.

Advisory lint warnings print to stderr; the `ok: ... is valid` verdict is
always the LAST line. An advisory class that names many nodes at once (e.g.
`transform-provides-undeclared`) collapses by default to a single count line —
`N envelope-transform(s) don't declare provides ... run with --verbose to list
them` — so the verdict isn't buried. Add `--verbose` to expand every node.
`--strict` (fail on any advisory, exit 2) is unaffected by `--verbose`.

**Not** caught by validate (these surface at `yaah run` time):

- unknown node `type` values
- `agent` nodes without `template` / `prompt`
- `provider:model` references whose provider isn't in the providers block
- `fn:module:func` references whose module doesn't import

Each of those produces a one-line message at run time naming the bad value
and the fix; they need a build-time `validate --strict` to surface earlier,
which isn't shipped yet.

## 3 — `yaah explain` to see the EFFECTIVE config

```bash
yaah explain root.json
```

Renders the config after `_extends` expansion and `_fake` overlay. Shows
which keys came from which file. Use when: a config inherits from another
and the merge isn't doing what you expected; `_fake` is overriding more
(or less) than intended.

## 4 — `yaah list` to see what's parked

```bash
yaah list root.json          # human prose
yaah list root.json --json   # machine-parseable
```

The mailbox view. Shows every baton parked at a human gate or waiting on
external input, the stage that parked it, what it's awaiting, and any
concerns it carries.

**`--json` baton shape** — the authoritative, stable contract a driver script
consumes (one object per parked baton, emitted by `runtime._baton_json`; the MCP
`list_gates` tool returns the identical shape):

```json
{
  "id": "b-3f0a…",                // baton id — pass to `yaah resume` / `yaah baton-schema`
  "stage": "review",              // the stage that parked
  "awaiting": "human:approve",    // what it's waiting on (tag), or null
  "parked_at": 1769990400.0,      // wall-clock epoch (float sec) the run suspended, or null;
                                  // pick the GREATEST to disambiguate N stale batons for one gate
  "checkpointed_at": null,        // Level 2 twin of parked_at: null for a suspended gate, the
                                  // last-checkpoint wall-clock for a RUNNING baton (the `running`
                                  // array); pick the GREATEST to disambiguate N killed checkpoints
  "question": "ship it?",         // the gate's question/ask, or null if it asked none
  "concerns": [                   // soft concerns gathered across the run ([] if none)
    {"by": "sceptic", "msg": "double-check the scope"}
  ],
  "escalation": null              // the failed verdict, if it parked by exhausting
                                  // its attempts (Y3); null for a plain human gate
}
```

The `awaiting` / `parked_at` / `question` / `escalation` fields describe a **park**,
so on an entry in the `running` array they are **always null** — including for a run
that was parked earlier and resumed past its gate (the resume clears them before the
next checkpoint is written). Read `stage` + `checkpointed_at` for a running entry.

**There is no top-level `payload` field, and that is deliberate.** A script that
reaches for `.payload` (expecting the baton's inner envelope) finds nothing — the
discoverable data is already **flattened** onto this object: read the parked
gate's prompt from **`question`** and the run's open concerns from **`concerns`**
directly. The durable baton's inner payload is intentionally not surfaced here:
it flows into the *resumed* run when you `yaah resume`, and exposing it in the
mailbox view would invite scripts to depend on app-specific keys the mailbox
contract does not own.

Use when: a pipeline doesn't return — most "stuck" pipelines are parked at
a gate, not crashed. A common confusion is restart-then-rerun: the parked
baton survives the restart (durable state), so the second `yaah run`
suspends *the new run* while the old one is still parked.

If a baton is parked you didn't expect: `yaah baton-schema root.json <id>`
shows the decision form the gate is waiting for. Compose the JSON, resume
with `yaah resume`.

**Running checkpoints (recovering a KILLED run).** Below the parked gates,
`yaah list` shows a `RUNNING` section — batons persisted mid-run by Level 2
checkpoint durability (docs/durable-state.md §5). On a durable `state:` store,
after each completed stage the harness saves the baton with its cursor advanced +
the next stage's input, so a run whose process was **killed mid-flight** (SIGKILL,
crash, host reboot) is re-drivable instead of lost:

```bash
yaah resume-run root.json <id>   # re-drive from the in-flight stage to the next gate/completion
```

The in-flight stage RE-RUNS (completed stages before it do not — their outputs are
the checkpoint). In `--json`, running checkpoints are an additive `"running"` array
alongside `"batons"`. Two cautions: (1) on a SHARED store a `RUNNING` baton may be a
run still LIVE in another process, not a crashed one — the engine can't tell them
apart (no liveness lease yet), so only resume-run a baton whose process you KNOW is
dead; (2) if the pipeline graph was edited between the kill and the resume, the
cursor stage is matched against the CURRENT graph — a renamed/removed stage resumes
wrong (the graph-fingerprint guard is on the roadmap). A killed run whose in-flight
stage committed a side effect re-runs that effect unless the node is idempotent.

## 5 — `yaah trace --pretty` for the postmortem

```bash
yaah trace state/trace.jsonl --pretty                  # full tree
yaah trace state/trace.jsonl --pretty --corr <run-id>  # one specific run
yaah trace state/trace.jsonl --pretty --last 5         # most recent 5 runs
yaah trace state/trace.jsonl --errors-only             # CI check: exits 1 if any errors
yaah trace state/trace.jsonl --cost prices.json        # spend rollup
```

The per-run tree shows every stage's duration, status, and any model_call /
tool_call children. ✓ = ok, ✗ = error, ⏸ = suspended (parked).

Each rejected attempt is its own `status: error` record carrying **why** it was
rejected: `retry` (`transient` | `retry` | `feedback`), `attempt`/`n` (which
attempt, on the `max_attempts` and `error_retries` budgets respectively), and
`error` — the failing verdict's detail, truncated at 500 chars with a trailing
`...[truncated]` marker (JSONL stays line-readable; the full text lives in the
stage's artifact). This is the answer to "four paid model calls ran and the
stage still failed — what did the validator object to?".

Use when: a run completed but the output is wrong (the trace shows which
stage's verdict was a retry vs. a final), a stage took longer than expected
(latency p95s in `--cost` output for tokens, individual `duration_ms` in
`--pretty` for stage timing), or to confirm a cost story before a real-model
batch.

## 5b — `yaah trace --counts` for the invocation-count report

```bash
yaah trace state/trace.jsonl --counts                    # table, tokens only
yaah trace state/trace.jsonl --counts prices.json        # table with $ cost
yaah trace state/trace.jsonl --counts prices.json --json # machine shape (list of rows)
```

Groups every `model_call` record by **(stage, model, ladder rung)** and reports
the columns you'd otherwise hand-roll from `jq`: `stage · model · calls ·
tokens_in · tokens_out · cost · p50 · p95`. This is the "how many calls, to
which model, from which stage, and how long" view — a cross-run cousin of
`--cost` (which is per-model only, no stage or duration breakdown).

- **Ladder rungs stay separate.** A model_call that carries `ladder_from` (the
  M7 escalation second rung) is a distinct row marked `(ladder)`, never merged
  into the rung-1 calls — even when it resolved to the same model.
- **`p50`/`p95` are nearest-rank** over each group's per-call `duration_ms` (an
  actually-observed call latency, not an interpolated number). A call missing a
  duration contributes nothing rather than a fabricated `0`.
- **Cost is honest.** A priced model shows a `$` amount (including an explicit
  `$0.0000` for a real zero-token call); an unpriced model shows `-` (cost
  unknown) — never a silent `$0.00`. Same "cost is opt-in" rule as `--cost`.
- **Zero-token rows are kept** — they're a forensic signal (a call that ran but
  produced nothing), not noise.

### What the `$` figure is made of

A `model_call` record splits INPUT into three classes, because they bill at
three rates: `tokens_in` (fresh input, the `"input"` rate), `tokens_cache_read`
(~0.1x that) and `tokens_cache_write` (~1.25x). `--cost` / `--counts` price each
separately; a price-map row can override the two derived cache rates with an
explicit `"cache_read"` / `"cache_write"` per-1k rate (do that for 1h-TTL cache
writes, which bill at 2x — the trace records no TTL).

> **Old traces are an upper bound, not a wrong number.** Records written before
> 2026-08 carry only `tokens_in`, and it is the SUM of all three classes, so the
> whole prompt prices at the full input rate. On a long, cache-heavy agentic
> stage that over-reports spend several-fold. Such traces cannot be re-priced —
> the split the arithmetic needs was never recorded — so read their `$` as a
> ceiling and don't compare them against a post-fix run.

Use when: you want the per-stage/per-model call breakdown for a cost or latency
story, or to confirm a laddered stage escalated as expected (the `(ladder)` row
appears with its own token/latency profile).

## 6 — Reading raw envelopes when the trace isn't enough

When the trace doesn't surface the thing you need (the prompt that was
sent, the agent's exact reply, a payload key that flowed through), the
fallback is the state store directly. For `state: {type: file, dir: ...}`,
the parked baton's envelope sits in `<state.dir>/batons/<id>` as JSON.

This is honest about a limitation: the trace today captures spans
(timing + cost + tool name + outcome), not payload content. Prompt /
response capture is a planned trace contributor; until it ships, the
state store + the agent's stage-attached input payload are how to see
"what was actually said."

## 7 — diagnose a dead run from JSON (`run --json`)

When a run dies, you don't have to read the prose `pipeline failed: ...` line and
guess. Add `--json` to `run` or `resume` and the failure comes back as one JSON
object on stdout — the exit code is unchanged (still non-zero), so scripts and CI
keep working.

```bash
yaah run root.json --json
```

A hard stage failure prints (this is a real blob from a run whose `test`
validator exited non-zero):

```json
{
  "outcome": "failed",
  "stage": "build",
  "failures": [
    {
      "code": "shell_exit",
      "message": "exit 1 != expected 0",
      "fix_hint": ""
    }
  ]
}
```

The loop is: **parse `outcome` → read `stage` + `failures` → act on `code` +
`fix_hint` → re-validate.**

1. **`outcome`** is one of `failed` / `done` / `suspended` (and `cleared`).
   Branch on it first.
2. On `failed`, **`stage`** names the stage that died and **`failures[]`** is the
   list of what broke. Each entry has `code` (a stable machine string you can
   switch on), `message` (the human detail), `fix_hint` (what to do), and — for
   codes that carry structured fields — an optional `data` object.
3. **Act on the `code`.** `shell_exit` above means a validator command failed;
   read `message` for the exit code. When a code carries `data`, use it instead
   of re-parsing the prose — e.g. a render that failed on an unfilled placeholder
   hands you the exact keys:

   ```json
   {
     "outcome": "failed",
     "stage": "render",
     "failures": [
       {
         "code": "render_unfilled_placeholders",
         "message": "no payload value for: title",
         "fix_hint": "add a parse step before this render, or set allow_unfilled:true if these fields are intentionally optional",
         "data": { "unfilled": ["title"] }
       }
     ]
   }
   ```

   `data.unfilled` is the placeholder list the render couldn't fill — almost
   always a missing parse step (an agent's reply is a string in `payload["raw"]`
   until a `transform` parses and merges it).
4. **Re-validate** after the fix: `yaah validate root.json` catches load-time
   mistakes before you spend another run.

A **succeeding** run prints `{"outcome": "done", "baton_id": ..., "payload":
{...}}` — the final envelope's payload, structured, so you can read the result
without scraping the `RESULT:` repr. A run that **parks at a gate** prints
`{"outcome": "suspended", "baton_id": ..., "awaiting": ..., "concerns": [...],
"ask": ...}` — the same fields the prose `GATE` line shows; drive it with
`baton-schema` → `resume` (see §4). `resume --json` uses the identical shapes.
Note the suspended outcome has no `stage` field (the outcome object doesn't
carry one) — when you need the parking stage, `yaah list root.json --json`
shows it per baton.

Over MCP (`mcp-serve`) the `run` / `resume` tools return the same structured
failure object in the `isError` content, so an agent client parses it the same
way instead of scraping a flattened error string.

Use when: a run failed and you want to act on the failure programmatically (a
debugger agent, a CI gate, a repair loop) rather than eyeball the prose.

## Common patterns

| Symptom | First check | If that's clean |
|---|---|---|
| `pip install` succeeded but `yaah run` errors out | `yaah doctor` | `yaah validate` |
| "It worked yesterday" | `yaah doctor` (env changed?) | `yaah explain` (config diverged?) |
| Pipeline hangs / doesn't return | `yaah list` (parked at a gate?) | check NATS transport timeouts |
| Run exits but output is wrong | `yaah trace --pretty --corr <id>` | inspect baton in `state.dir` |
| Need the failure as data (script / agent / CI) | `yaah run root.json --json` | act on `code` + `fix_hint`, `data` when present |
| Cost is higher than expected | `yaah trace --cost prices.json` | per-model rollup; look for retry loops |
| Same task, two different results | `yaah trace --pretty --last 2` | compare stage trees + retry signal |
| Some stage is slow | `yaah trace --pretty` | look at `duration_ms` per stage |

## Composing trace flags

The trace view flags (`--pretty`, `--errors-only`, `--cost`, `--counts`) are
mutually exclusive — each produces a different shape. `--json` (machine output
for `--counts`), `--last N`, and `--corr <id>` are MODIFIERS/FILTERS and compose
with any view:

```bash
# Just the errors from the last 10 runs
yaah trace x.jsonl --errors-only --last 10

# Cost rollup zoomed to one run
yaah trace x.jsonl --cost --corr abc123 prices.json

# Tree view of just the most recent run
yaah trace x.jsonl --pretty --last 1
```

If both a view and the default JSON aggregate are needed, run twice with
different flags — the JSONL file isn't consumed.

## When to ask "is this a yaah bug or a pipeline bug?"

A failing run is almost always a pipeline bug (wrong prompt, missing
parse step, wrong validator). The engine bugs that DO exist usually
fall in three buckets:

1. **Config edge case** — `yaah validate` accepts something `yaah run`
   rejects. File an issue with both outputs.
2. **State store corruption** — `yaah list` shows a baton with a
   missing pending envelope. Rare; if it happens, save the state dir.
3. **NATS transport flake** — request timeouts that don't match per-stage
   timeouts. `validate_budgets` catches the static case; dynamic
   coincidences need observation.

Most of the time the answer is in the trace, not the engine. The
playbook above is enough for almost everything.
