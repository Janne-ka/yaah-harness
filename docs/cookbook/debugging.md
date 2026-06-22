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

Use when: a pipeline doesn't return — most "stuck" pipelines are parked at
a gate, not crashed. A common confusion is restart-then-rerun: the parked
baton survives the restart (durable state), so the second `yaah run`
suspends *the new run* while the old one is still parked.

If a baton is parked you didn't expect: `yaah baton-schema root.json <id>`
shows the decision form the gate is waiting for. Compose the JSON, resume
with `yaah resume`.

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

Use when: a run completed but the output is wrong (the trace shows which
stage's verdict was a retry vs. a final), a stage took longer than expected
(latency p95s in `--cost` output for tokens, individual `duration_ms` in
`--pretty` for stage timing), or to confirm a cost story before a real-model
batch.

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

## Common patterns

| Symptom | First check | If that's clean |
|---|---|---|
| `pip install` succeeded but `yaah run` errors out | `yaah doctor` | `yaah validate` |
| "It worked yesterday" | `yaah doctor` (env changed?) | `yaah explain` (config diverged?) |
| Pipeline hangs / doesn't return | `yaah list` (parked at a gate?) | check NATS transport timeouts |
| Run exits but output is wrong | `yaah trace --pretty --corr <id>` | inspect baton in `state.dir` |
| Cost is higher than expected | `yaah trace --cost prices.json` | per-model rollup; look for retry loops |
| Same task, two different results | `yaah trace --pretty --last 2` | compare stage trees + retry signal |
| Some stage is slow | `yaah trace --pretty` | look at `duration_ms` per stage |

## Composing trace flags

The trace view flags (`--pretty`, `--errors-only`, `--cost`) are mutually
exclusive — each produces a different shape. `--last N` and `--corr <id>`
are FILTERS and compose with any view:

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
