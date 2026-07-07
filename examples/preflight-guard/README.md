# preflight-guard — validate input before you pay for a model call (composition pattern)

A **deterministic** transform validates and normalizes the request **before** any
model call. Missing a required field, empty, or over a size limit → **reject early**
with a clear error, having spent **zero** tokens and zero latency on a model. Only
clean input reaches the (paid) `summarize` agent. It's pure wiring — a `transform`,
a `branch`, and a route map — no engine code.

## The one idea that makes it work
The guard is a `transform`, not an agent. It runs FIRST and writes a plain branch
key `input_ok` = `"yes"` / `"no"`. The `branch` sends `"yes"` to the model and
everything else to `reject`. **Validation and normalization are the same
deterministic step**, done once, for free, before the expensive stage — the guard
also strips whitespace so the model sees clean input.

**Fail-closed:** any input the guard can't positively clear routes to `reject`, and
the branch's `default` is *also* `reject`. A guard that returned an unexpected
`input_ok` value could never fall through to the model.

## Shape
```
guard (transform: required-field + size check, normalize → input_ok)
  → branch on input_ok: yes     → summarize (agent)   ← the only stage that spends
                        no/default → reject (transform: clear error, NO model call)
```

## Run it
```bash
# offline, deterministic. DEFAULT input is oversized → REJECT, no model call:
PYTHONPATH=src python3 -m yaah.runtime run examples/preflight-guard/preflight.local.json

# the PASS path: a valid input clears the guard and reaches the summarize agent:
PYTHONPATH=src python3 -m yaah.runtime run examples/preflight-guard/preflight-valid.local.json
```

The reject run's trace shows only two stages — `guard` then `reject`. The
`summarize` stage never runs, so the fake provider's script is never even consumed:

```
preflight: REJECT (no model call) — text too long: 284 chars > limit 280
[trace] stage guard ok (2ms)
[trace] stage reject ok (0ms)
RESULT: Done(... payload={'status': 'rejected', 'error': 'text too long: 284 chars > limit 280'} ...)
```

The valid run adds a third stage and the model's summary appears in the output:

```
preflight: OK — input cleared, proceeding to model
[trace] stage guard ok (1ms)
[trace] stage summarize ok (1ms)
RESULT: Done(... payload={'raw': '{"summary": "..."}', 'summary': 'A 14:02 UTC config push dropped the cache tier ...'} ...)
```

Swap `input` to `fixtures/missing-field.json` to see the missing-required-field
rejection instead of the size one.

## The data-flow bite (read before you adapt this)
An agent's reply **replaces** the payload. That doesn't hurt *this* pipeline because
both branches are terminal — nothing reads a pre-model key after the agent. But the
guard is a `transform` with `call: "envelope"`, whose return value **also** replaces
the payload, so it returns `{**envelope.payload, ...}` to keep `title`/`text` alive
for the summarize agent to read. Drop the `**payload` splat and the agent's
`{{title}}`/`{{text}}` placeholders would render empty. The guard node declares
`provides: ["input_ok", "error", "title", "text"]` so the author-time lint knows
those keys exist downstream.

## Make it your own
`summarize` is a stand-in for whatever expensive stage you're protecting (an agent,
an `agent_loop`, a fork of lenses). `reject` is a stand-in — in real use it becomes
an HTTP 400 back to the caller, or a `human_gate` that queues malformed input for a
person. Tune the guard's `config`: `required` (which keys must be present) and
`max_chars` (the size limit). Because the whole guard is config, prepending it to an
existing pipeline is a one-stage edit.

## Limits — a cost/shape gate, NOT a semantic one
The guard checks **shape** (present, non-empty, within size), not **meaning**. It
won't catch input that is well-formed but nonsense, off-topic, or adversarial prose —
that judgement needs a model (see [`tool-safety-gate/`](../tool-safety-gate/) for the
cheap-model *pattern check*, or a full agent for real semantics). Use preflight-guard
to kill the obvious-garbage class cheaply and deterministically; layer a model check
on top when you need one.
