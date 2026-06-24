---
name: yaah-driving
description: Use when an operator needs to drive a running YAAH pipeline through its
  human-gate workflow — list parked batons, read the decision form, compose
  decision.json, resume. Not for authoring pipelines (yaah-pipeline-authoring); not
  for engine code (yaah-extending); not for diagnosing engine bugs (read the trace
  directly).
---

# Driving YAAH

**Standing rule:** never commit unless explicitly asked. (Operator workflows
write small files like `decision.json`; that's fine. Don't `git commit` them.)

## When to Use

- The pipeline parked at a `human_gate` and the operator wants to know what to
  do next.
- The operator asks: "what's waiting on me?", "what does this baton need?",
  "deliver an approval", "advance the pipeline".
- A scheduled / CI run needs to advance without a human (auto-drive).
- Not for: writing the pipeline itself, debugging engine failures, or
  inspecting trace JSONL for performance.

## The mental model

A YAAH pipeline that hits a `human_gate` **suspends** — the engine writes the
parked envelope into durable state (a `baton`), exits, and returns control
to the operator. The mailbox flow is:

```
yaah list <root>                          # see every parked baton
yaah baton-schema <root> <baton-id>       # learn what decision.json needs
<write decision.json>                     # compose the operator's answer
yaah resume <root> <baton-id> decision.json   # deliver; engine runs to next gate or completion
```

That's the whole loop. Each step has a specific footgun captured below.

## Step 1 — list

```bash
yaah list <root>                  # human-readable lines
yaah list <root> --json           # parseable (use this when driving from a script or another agent)
```

The output names each parked baton's id, the stage it's awaiting on, and how
long it's been parked. If `yaah list` says `(no suspended gates)`, the
pipeline either finished or never paused — there's nothing to drive.

## Step 2 — baton-schema

```bash
yaah baton-schema <root> <baton-id>
```

This is the **single source of truth** for what `decision.json` must contain.
Output is a JSON Schema; compose `decision.json` to match. The form falls in
one of four shapes (the catalog from [ADR-0002](../../../docs/decisions/0002-decision-forms.md)):

| Declared `form:` | What `decision.json` looks like |
|---|---|
| `approve` | `{}` — the existence of the file is the approval |
| `approve_or_revise` | `{"decision": "approve"}` OR `{"decision": "revise", "<key>": "..."}` |
| `free_text` | `{"text": "<the operator's edit / instruction>"}` |
| `json_schema` | Match the inline schema (the baton-schema output spells it out exactly) |

If `yaah baton-schema` exits with **"no form declared"**, the gate's
`human_gate` node is missing `form:`. That's an **authoring bug**, not a
driving problem — punt to `yaah-pipeline-authoring`. Don't guess the shape
and write a decision.json blindly; you'll silently mis-deliver.

## Step 3 — compose decision.json

Match the schema exactly. Common patterns:

```json
// approve — file body is empty object
{}

// approve_or_revise (approve)
{"decision": "approve"}

// approve_or_revise (revise) — extra keys carry the operator's correction
{"decision": "revise", "text": "tone is too casual; rewrite formally"}

// free_text
{"text": "publish with the headline 'Q4 review'"}
```

Place the file anywhere readable; the path is just an argument to `resume`.

## Step 4 — resume

```bash
yaah resume <root> <baton-id> decision.json
```

**Expectations:**

- This BLOCKS the terminal until the engine reaches the next gate or
  completion. The originally-suspended engine process has already exited;
  THIS process runs the engine forward in-process. The runtime now prints
  a banner saying so when resume starts.
- If the run hits another gate, the terminal prints `GATE baton_id=…
  awaiting=…` and the cycle starts over (`yaah list` → `baton-schema` →
  new `decision.json` → new `resume`).
- If the run completes, the terminal prints `RESULT: Done(…)`.
- Each baton is **single-shot**. Resuming a delivered baton exits with a
  clear error naming the diagnostic (`run yaah list`).

## Reading errors when they appear

| Message starts with | What it means | What to do |
|---|---|---|
| `no resumable baton 'X' — run yaah list` | The id is wrong, OR the baton was already resumed (single-shot), OR its TTL expired. The exact cause is non-distinguishable; the engine collapses them. | Run `yaah list <root>`. If the id isn't there: it's gone. Get a fresh id from the list. |
| `baton 'X' status is 'Y', not 'suspended'` | The baton exists but isn't parked. Status `running` means another driver is in flight; `done` means it already finished. | If `running`, wait for that driver to finish (or check whether a previous resume hung). If `done`, the run completed; there's nothing to drive. |
| `baton 'X' has no suspended stage (engine invariant violation: ... this is a bug)` | A real engine bug — should not happen in production. | File an issue with the trace; do not retry naively. |
| `error: no form declared` (from `yaah baton-schema`) | The gate's `human_gate` node didn't set `form:`. Authoring bug. | Punt to the pipeline author; can't drive safely without knowing the shape. |

## Watching live progress

If the root configures a `progress_file` trace sink (`trace.sink: {type:
"progress_file", path: "progress.log"}`), tail it from a second terminal:

```bash
tail -f progress.log
```

Each line is one stage completion. A `suspended` line now includes the
`awaiting=<label>` inline (recent polish), so you can see what just parked
without re-running `yaah list`:

```
12:01:33 review_gate     suspended (3ms) awaiting=human:arch-review
```

This is faster than `yaah list` for the second-and-later parks.

## Auto-drive (unattended / CI / cron)

When no operator exists, the root config can pre-answer every gate:

```jsonc
{
  "_extends": "starter.real.json",
  "decisions": {
    "<gate-stage-name>": {"auto": "approve"}
  }
}
```

Per-gate; if a pipeline has three gates, each needs its own entry. The
`instrumented` archetype's `.dogfood.json` overlay is the reference shape;
see [`examples/arch-drift/arch-drift.dogfood.json`](../../../examples/arch-drift/arch-drift.dogfood.json).

When auto-drive is on, the engine never parks — `yaah list` will return
empty even mid-run. That's intentional; the run goes straight to RESULT.

## Common mistakes

| Mistake | Reality |
|---|---|
| Guessing `decision.json` shape without running `baton-schema` first | The shape is form-dependent and the schema is the truth. A wrong shape silently mis-routes or rejects. Always run `baton-schema` first. |
| Re-issuing `resume` after "no resumable baton" | Each baton is single-shot. Re-issuing won't recover it; you need a fresh `yaah list`. |
| Expecting `resume` to background | It blocks until the next gate or completion. For long resume chains, run inside `tmux` / `screen` or use `nohup`. A future `--detach` flag is on the roadmap. |
| Writing `decision.json` while another `resume` is in flight | Each baton is single-shot; the second `resume` will get "no resumable baton" because the first one already evicted it. Coordinate operators externally. |
| Setting `decisions: {<gate>: "approve"}` (bare string) | The shape is `{"auto": "approve"}` — the wrapper dict matters. `validate_root` catches the bare-string form with did-you-mean. |
| Trying to drive a pipeline with no `state:` configured | `state.type: "memory"` means the baton store dies with the engine process; nothing can be resumed. For real human-gate pipelines, `state.type: "file"` (or richer) is required. The `branch-with-gate` archetype's starter sets this. |

## Cleaning up

- A baton you no longer want delivered: `yaah resume` it with the right
  rejection (form-dependent), OR wait for its TTL to expire (default
  72h, configurable via root `baton_ttl: <minutes>`). There's no `yaah
  cancel <id>`; the TTL sweep is the cleanup path.
- A whole stuck queue: `yaah clear <root>` drops EVERY parked baton.
  Destructive — use only when you've confirmed via `yaah list` that
  nothing in flight matters. Memory note: never blindly clear without
  list-first.

## Related

- [`docs/decision-forms.md`](../../../docs/decision-forms.md) — user reference for the catalog + extension.
- [`docs/decisions/0002-decision-forms.md`](../../../docs/decisions/0002-decision-forms.md) — the ADR.
- [`docs/archetypes.md`](../../../docs/archetypes.md) — `branch-with-gate` is the canonical archetype this skill drives.
- [`docs/shape-grammar.md`](../../../docs/shape-grammar.md) — the one-page CLI verbs reference.
- [`yaah-pipeline-authoring`](../yaah-pipeline-authoring/SKILL.md) — where to send "the gate has no form declared" cases.
