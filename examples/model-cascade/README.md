# model-cascade — cheap model first, escalate to expensive only when needed

Try a **cheap** model (haiku); if it can't meet the contract it asks for **help**,
and the engine ladders **once** to an **expensive** model (opus) with the same
prompt. When the cheap model answers confidently, the expensive model is **never
called** — you pay the cheap price on the easy majority and the expensive price only
on the hard tail. This is the engine's built-in **one-rung model ladder**
(`escalate_model`): a single agent stage plus one config field, no graph to wire.

## The one idea that makes it work
`escalate_model` fires on the **blocked-agent convention**: a model that judges it
*cannot* do the job replies `{"answer": "", "help": "<why>"}` instead of guessing. A
truthy top-level `help` key triggers exactly **one** re-call with the stronger model;
the engine returns that reply. No `help` (empty/absent) → the cheap answer is
returned as-is and the ladder never fires.

```json
"role:extract": {
  "type": "agent",
  "model": "claude:claude-haiku-4-5",
  "escalate_model": "claude:claude-opus-4-6",
  "output_schema": {"properties": {"answer": {"type": "string"}, "help": {"type": "string"}},
                    "required": ["answer"]}
}
```

Two facts that bite: the help reply **must still pass `output_schema`**, so `help`
is an *additive optional* string and `answer` (required) is allowed to be empty on
the blocked path — never make `help` required. And the trigger is `help`, a
**self-report**, not an external validator verdict (see "Honest scope" below).

## Shape
```
extract (agent, cheap model, escalate_model=expensive)
   ├─ cheap reply has truthy help  → re-call ONCE with expensive → return that
   └─ cheap reply has empty  help  → return the cheap reply (expensive never called)
```

## Run it — both paths, offline & deterministic
```bash
# ESCALATION: the ambiguous line makes the cheap model ask for help → ladders to opus:
PYTHONPATH=src python3 -m yaah.runtime run examples/model-cascade/cascade-escalate.local.json

# SHORT-CIRCUIT: the simple line is answered by the cheap model → opus never called:
PYTHONPATH=src python3 -m yaah.runtime run examples/model-cascade/cascade-short-circuit.local.json
```

The two `*.local.json` roots use the **same** pipeline; only the input and the fake
provider's `by_model` scripts differ. The escalate run's final `answer` (`47.90`)
comes from **opus**; the short-circuit run's (`4.50`) comes from **haiku**.

**Proof the ladder actually fired** (add a file trace sink and read the spans):
```
$ yaah trace <trace.jsonl> --counts     # escalate run
stage    model                            calls
extract  claude:claude-haiku-4-5              1
extract  claude:claude-opus-4-6 (ladder)      1   ← the second, escalated call

$ yaah trace <trace.jsonl> --counts     # short-circuit run
stage    model                    calls
extract  claude:claude-haiku-4-5      1           ← only the cheap model; no ladder row
```
The escalated `model_call` span carries `ladder_from: "claude:claude-haiku-4-5"` and
`ladder_trigger: "help"`; the short-circuit run has neither. The expensive model's
script entry in `cascade-short-circuit.local.json` is left deliberately unconsumed —
proof it was never called.

## The offline-testing footgun that bit this example (read before scripting fakes)
The `fake_scripted` provider advances a **content cursor** for cross-process resume
durability: it treats a scripted reply as *already emitted* if that exact string
appears **verbatim in the prompt**. An early draft of this example put
`Example: {"answer": "4.50", "help": ""}` in the prompt — identical to the scripted
cheap reply — so the provider skipped it and returned its empty exhaustion default,
failing the stage with `not_json`. **Keep scripted fake replies out of the prompt
text verbatim** (paraphrase examples, or use values the script never returns).

## Honest scope — `help` is a self-report, not an external validator
This ladder escalates when the cheap model **admits** it's blocked. That's the right
tool when the cheap model can recognize its own limits (out-of-distribution input,
missing context, low confidence it can express). It does **not** escalate when the
cheap model is *confidently wrong* — nothing external is judging its answer here.

If you need "escalate when the cheap output **fails a check the model can't run on
itself**" (a schema/enum/shell validator, a golden comparison, a second-opinion
judge), that is a different shape: a **branch cascade** — `cheap agent → validator
transform → branch: pass → done / fail → expensive agent`. The
[cookbook entry](../../docs/cookbook/model-cascade.md) gives that skeleton and a
table for choosing between the two. Use the built-in ladder (this example) when the
cheap model can self-assess; reach for the branch cascade when an *external* verdict
must gate the escalation.
