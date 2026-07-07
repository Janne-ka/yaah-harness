# Model cascade — cheap model first, escalate to expensive only when needed

Copy this pattern when a task is **easy for most inputs and hard for a few**, and you
want to pay the cheap-model price on the easy majority and the expensive-model price
only on the hard tail. There are **two** shapes, and picking the right one is the
whole decision:

| | **A. Built-in ladder** (`escalate_model`) | **B. Branch cascade** (composition) |
|---|---|---|
| Escalates when… | the cheap model **admits** it's blocked (`help`) | an **external validator** rejects the cheap output |
| Who judges the cheap output | the cheap model, itself | a transform / validator / second model |
| Wiring | one config field on one agent node | two agent nodes + a validator + a branch |
| Cost worst-case | 2 model calls (× `max_attempts`) | 2 model calls (+ the validator) |
| Reach for it when | the cheap model can recognize its own limits | the cheap model is *confidently wrong* and can't self-assess |

**Reference implementation (shape A):**
[`examples/model-cascade/`](../../examples/model-cascade/)

---

## Shape A — the built-in one-rung ladder (`escalate_model`)

The engine has this cascade built in. A model that judges it *cannot* do the job
replies `{"answer": "", "help": "<why>"}` instead of guessing (the **blocked-agent
convention**). A truthy top-level `help` key triggers exactly **one** re-call of the
same prompt with the stronger model; the engine returns that reply. No `help` → the
cheap answer is returned and the ladder never fires.

```
extract (agent, cheap model, escalate_model=expensive)
   ├─ cheap reply has truthy help  → re-call ONCE with expensive → return that
   └─ cheap reply has empty  help  → return the cheap reply (expensive never called)
```

```json
"role:extract": {
  "type": "agent",
  "prompt": "file:extract",
  "model": "claude:claude-haiku-4-5",
  "escalate_model": "claude:claude-opus-4-6",
  "output_schema": {
    "properties": {"answer": {"type": "string"}, "help": {"type": "string"}},
    "required": ["answer"]
  }
}
```

The prompt must teach the cheap model the convention: *answer confidently, or set
`help` to the reason and leave `answer` empty — do not guess.*

**Three facts before you wire it on** (from
[node-reference.md](../node-reference.md)):

1. **A help reply must still PASS `output_schema`.** Declare `help` as an *additive
   optional* string and let `answer` (required) be empty on the blocked path. If you
   make `help` required, a confident cheap reply that omits it fails the schema; if
   `answer` isn't allowed to be empty, the blocked reply fails the schema and you get
   the retry+feedback path instead of the ladder.
2. **Cost multiplies with retries.** Each stage attempt may ladder once, so
   `max_attempts: 3` is up to **6** model calls worst-case.
3. **`parse: false` + `escalate_model` is rejected at validation.** The ladder needs
   the parsed reply to inspect `help`, so it's parse-path only.

The escalation is visible in the trace as a second `model_call` span carrying
`ladder_from` and `ladder_trigger: "help"`; `yaah trace <t> --counts` shows it as a
`… (ladder)` row. That's your proof the ladder fired (or, on the short-circuit path,
that it didn't).

---

## Shape B — the branch cascade (when an EXTERNAL check gates escalation)

The built-in ladder escalates on the cheap model's **self-report**. It does **not**
help when the cheap model is *confidently wrong* — nothing external is judging its
answer. When escalation must be gated by a check the model can't run on itself (a
schema/enum validator, a `shell_check`, a golden comparison, a second-opinion judge),
compose it by hand:

```
cheap  (agent, cheap model, carry the inputs the expensive stage needs)
   ↓
check  (transform or validator: judge the cheap output → quality = "pass" | "fail")
   ↓ branch on quality
   ├─ "pass"        → emit cheap result
   └─ "fail"/default → expensive (agent, expensive model) → emit expensive result
```

```json
"graph": {
  "stages": {
    "cheap":     {"node": "role:cheap", "then": "check"},
    "check":     {"node": "role:check",
                  "branch": {"on": "quality", "routes": {"pass": "emit"}, "default": "expensive"}},
    "expensive": {"node": "role:expensive", "then": "emit"},
    "emit":      {"node": "role:emit", "then": null, "final": true}
  }
}
```

The `check` stage is where the *external* verdict lives — it can be a deterministic
transform (enum/shape/length), a real validator node, or another (cheap) model acting
as a judge. Route fail-closed: name the `pass` route, make `default` the escalate side,
so an unexpected `quality` value escalates rather than silently shipping the cheap
answer.

**The data-flow bite:** the cheap agent's reply **replaces** the payload. Anything the
`expensive` agent's prompt reads (the original question/input) must be listed in the
cheap node's `carry:` so it survives the hop, and the `check` transform must return
`{**envelope.payload, ...}` to keep it alive. Miss this and the expensive stage renders
its prompt against an empty payload. (See [AGENTS.md](../../AGENTS.md) "Authoring a
pipeline"; the reference example avoids it because Shape A is a single stage.)

---

## Choosing A or B

- The cheap model can tell when it's out of its depth (unfamiliar input, missing
  context, low confidence it can *express*) → **A**. One field, no graph. This is the
  common case and the reference example.
- The cheap model can't judge itself and you have an **objective** gate (valid enum
  label? passes a schema? matches a golden? a judge scores it ≥ threshold?) → **B**.
- You want *both* — self-report **and** an external gate on the escalated answer →
  wrap Shape A's stage in Shape B's `check` + branch (validate the top rung too; A
  alone trusts whatever the expensive model returns).

---

## Honest limits

**Shape A trusts the top rung.** The escalated (expensive) reply is returned whatever
it is — the ladder validates it against the same `output_schema`, but there is no
third rung and no external judge. A repeated `help` from the expensive model flows out
to the normal concern/gate path rather than burning a third call. If the expensive
answer also needs checking, use Shape B around it.

**`help` is a prompt-engineering contract, not a guarantee.** The cheap model only
escalates if it *emits* `help` — a model that doesn't know it's wrong won't ask for
help. Calibrate the prompt (what counts as "unsure"), and measure the escalation rate
on real inputs: too low means confident-wrong answers slip through (consider Shape B);
too high means you're paying the expensive price too often (the ladder isn't saving
you money — the cheap rung is miscast).

**Offline testing: keep scripted fake replies OUT of the prompt verbatim.** The
`fake_scripted` provider advances a content cursor for cross-process resume
durability — it treats a scripted reply as *already emitted* if that exact string
appears in the prompt, then returns its empty exhaustion default (a silent
`not_json`). If your prompt shows an example like `{"answer": "4.50", "help": ""}`,
don't also script that exact string as a reply. Paraphrase prompt examples, or script
values the prompt never contains. (This one bit the reference example during
authoring.)

**Mixed-capability ladders + tools degrade silently.** With `tools`, the tool manifest
is rendered for the *primary* model's capability. Keep both rungs tool-capable, or keep
the agent tool-free.

---

## Checklist for adapting this recipe

1. Decide A or B from the table up top — *who* judges the cheap output.
2. **Shape A:** set `escalate_model`; declare `help` as optional and let the
   required answer key be empty on the blocked path; teach the convention in the
   prompt. Verify both paths with a fake `by_model` script (a `help` reply → ladders;
   an empty-`help` reply → short-circuits) and confirm the `(ladder)` row appears only
   on the escalate run (`yaah trace --counts`).
3. **Shape B:** `carry` the expensive stage's inputs across the cheap hop; return
   `{**payload, ...}` from `check`; wire the branch fail-closed (`default` = escalate).
4. Either way, script the fake so replies don't appear verbatim in the prompt.
5. Measure the real-input escalation rate — the cascade only saves money if the cheap
   rung handles the majority.
