# The Envelope, by example

Everything in YAAH is one message shape — the **Envelope** — and a run is that
message flowing stage to stage, each stage's output becoming the next stage's
input. If you're writing a node (or an AI is authoring one), this is the contract
you read and return. Every example below is a **real envelope captured from a run**
of `judge-gate.run.fake.json`, not invented.

## The shape

```jsonc
{
  "id":      "d01667de…",          // unique per envelope (auto)
  "kind":    "result",             // task | result | verdict | await | resume | error | event | handoff
  "payload": { … },                // your DOMAIN DATA — what a node reads and writes
  "headers": {                     // METADATA — the harness manages these
    "correlation_id": "7cb58c…",   // the RUN id (stable for the whole run; = the first envelope's id)
    "causation_id":   "7cb58c…",   // the id of the envelope that caused this one
    "clear_id":       "…",         // (forks only) the gate address a branch carries to its fan-in
    "sender":         "…"          // (optional) set on handover
  }
}
```

You almost always touch only `payload`. The harness owns `headers` — when a node
calls `input.reply(...)`, correlation/causation are chained for you.

## A run, hop by hop

### 1. The start — a `task` envelope
The run begins with one `Kind.TASK` envelope; its `payload` is the root's `input`.
Empty headers — the harness fills them as it goes.
```json
{ "id": "7cb58c…", "kind": "task",
  "payload": { "task": "Explain how to reverse a list" },
  "headers": {} }
```

### 2. After an `agent` stage — the model's text lands in `payload.raw`
**The contract that bites everyone:** an agent writes its raw output to
`payload["raw"]` as a **string**. It does *not* merge structured fields. Note the
`task` key survived — it was on the node's `carry` list.
```json
{ "id": "d01667…", "kind": "result",
  "payload": { "raw": "Draft v1: a quick answer.",
               "task": "Explain how to reverse a list" },
  "headers": { "correlation_id": "7cb58c…", "causation_id": "7cb58c…" } }
```

### 3. The judge agent — `raw` is now a JSON string (still a string!)
A validator or downstream node still sees `raw` as text. Nothing has parsed it yet.
```json
{ "kind": "result",
  "payload": { "raw": "{\"decision\": \"rework\", \"concerns\": [\"misses the empty-input edge case\"]}",
               "task": "Explain how to reverse a list" },
  "headers": { "correlation_id": "7cb58c…", "causation_id": "d01667…" } }
```

### 4. A `transform` stage parses `raw` into real payload keys
This is what a parse node is *for*: turn `raw` (string) into keys the graph can
branch on. Here `judge_route` produced the branch key `route`, bumped the loop
counter `judge_attempts`, and turned the judge's concerns into the standard
`feedback` block (which the next agent's prompt appends automatically). Note `raw`
is gone — it was consumed.
```json
{ "kind": "result",
  "payload": { "task": "Explain how to reverse a list",
               "route": "rework",
               "judge_attempts": 1,
               "feedback": [ { "code": "judge",
                               "message": "misses the empty-input edge case",
                               "fix_hint": "" } ] },
  "headers": { "correlation_id": "7cb58c…", "causation_id": "4748a2…" } }
```
The `branch` then routes on `payload["route"]` — `"rework"` sends this envelope
*backward* to the rework agent.

### 5. The rework agent — got the feedback, produced a new `raw`
The agent's prompt automatically received the `feedback`; its fresh output is again
a string in `raw`. `judge_attempts` rode along (it was carried), so the loop bound
holds.
```json
{ "kind": "result",
  "payload": { "raw": "Draft v2: covers the empty-input edge case.",
               "task": "Explain how to reverse a list",
               "judge_attempts": 1 },
  "headers": { "correlation_id": "7cb58c…", "causation_id": "659df0…" } }
```

## Other kinds you'll author against

- **`verdict`** — a validator's output. `payload` carries `{ "status": "pass"|"fail",
  "severity": "hard"|"soft", "failures": [ { "code", "message", "fix_hint" } ] }`.
  Build it with `Verdict.passed()` / `Verdict.failed(...)`, not by hand.
- **`await`** — a node asking to suspend (a human gate). `payload` carries the
  rendered `ask` and `awaiting`; the harness parks the baton and merges the human's
  decision onto this artifact on resume.
- **`error`** — a failure. Over NATS, a worker that raised replies with this kind;
  the harness treats it as a stage failure (it is *not* a silently-passed result).

## The rules a node author (human or AI) must keep

1. **Read and write `payload`; leave `headers` to the harness.** Return via
   `input.reply(kind, **fields)` or `input.reply_with(kind, payload_dict)` so
   causation chains correctly.
2. **An agent's output is `payload["raw"]`, a string.** If a later stage needs to
   `branch` on a field or a template needs `{{field}}`, a **parse `transform` must
   run first** to lift `raw` into real keys. An agent→branch or agent→render edge
   with no parse between them is the #1 authoring bug.
3. **Only `carry`-listed keys survive an agent stage.** A payload-replacing
   transform must explicitly re-carry what downstream needs (the factory uses a
   `_carry` helper). A dropped key surfaces as an empty `{{placeholder}}` far later.
4. **`feedback` is the standard retry channel.** Put `[{code, message, fix_hint}]`
   on `payload["feedback"]` and the next agent's prompt appends it automatically —
   that's how "return to sender with the judge's concerns" works without a custom
   prompt.

See [`agent-tools.md`](agent-tools.md) for the tool/MCP payload shapes and
[`architecture.md`](architecture.md) for where each node type sits.
