# Tutorial

This picks up where the [Quickstart](quickstart.md) leaves off and walks through
every core idea, one at a time. Each part has a **runnable example** under
`examples/` — run it as you read.

Everything here runs on a **fake** model backend: free, offline, deterministic.

---

## Concepts in 60 seconds

The whole model, before you invest in the rest of the page:

- **Envelope** — one message; a run is a single envelope flowing stage → stage.
- **Node** — a worker: `agent`, `transform`, `human_gate`, `shell`/`shell_check`,
  `render`, `get`/`post`. You pick one per stage.
- **Comms** — the harness routes between nodes; they never call each other.
- **You write two JSON files:** a *pipeline* (nodes + how they're wired with
  `then`/`branch`/`fork`) and a *root config* (model backend + input).
- **The one gotcha:** an agent's reply is a STRING in `payload["raw"]`. A `parse`
  transform turns it into keys. Put a parse between any `agent` and a `render`/`branch`,
  or the render fails (`render_unfilled_placeholders`) pointing at the missing parse.
- **Build your own:** list stages → pick a node per stage → wire them → run on the
  fake backend → swap in a real model. Or copy the nearest example and edit.
- **Run one right now:**
  `cd examples/hello-yaah && python3 -m yaah.runtime starter.local.json`

That's it. The rest of this page shows each piece in a runnable example.

---

## The mental model

Three things, that's it:

- **Envelope** — one message. A run is a single envelope that flows from stage to
  stage. Each stage reads it and hands it on.
- **Node** — a worker. `invoke(input) → output`. An LLM agent is a node; so is a
  shell command, a template renderer, a Python function. You pick a type; you
  rarely write one.
- **Comms** — the harness moves the envelope between nodes. Nodes never call each
  other directly.

You write two JSON files: a **pipeline** (the nodes and how they're wired) and a
**root config** (how to run it — which model backend, which input). That's the
whole job.

---

## Build a pipeline for your own task

Have something in mind ("draft → review → publish", "fix a bug test-first",
"summarize each ticket")? Five steps:

1. **List your steps** as stages, in order.
2. **Pick a node per step:** `agent` (think), `transform` (deterministic code),
   `human_gate` (a person decides), `shell` / `shell_check` (run a command),
   `render` (write a file) — and a `parse` transform after every agent.
3. **Wire them** in the graph with `then` (next), `branch` (decide), or
   `fork` + `fanin` (parallel).
4. **Copy a `*.local.json` root**, point it at your pipeline and a `fake` provider
   so it runs offline.
5. **Run → fix → go real:** `python3 -m yaah.runtime your.local.json`, iterate until
   green, then swap `fake` for `claude`/`litellm`.

**Fastest start:** copy the example closest to your shape — `hello-yaah` (linear),
`review-pipeline` (a human decision), `fork-join` (parallel) — and edit it. The rest
of this tutorial explains each step; or hand the whole thing to an AI assistant
([Part 7](#part-7--let-an-ai-build-it-for-you)).

---

## Part 1 — Follow the envelope

Run the smallest pipeline and watch one envelope move through it:

```bash
cd examples/hello-yaah
python3 -m yaah.runtime starter.local.json
```

It has four steps: **summarize** (an agent) → **check** (a validator) → **parse**
(a transform) → **render**. Here is the envelope's `payload` at each step — this is
the whole lesson:

```
1. the input                {"text": "YAAH is a domain-free harness."}

2. after `summarize`        {"raw": "{\"summary\": \"hello\"}"}
   the agent answered — and its answer is a plain STRING under "raw".
   Nothing has read it yet.

3. after `check`            {"raw": "{\"summary\": \"hello\"}"}
   the validator only LOOKED: "is raw valid JSON with a summary key?" Yes.
   It changed nothing.

4. after `parse`            {"summary": "hello"}
   the transform turned that string into a real key.

5. after `render`           {"summary": "hello",
                             "output": "<h1>hello</h1>",
                             "path": "summary.html"}
   the template <h1>{{summary}}</h1> could finally see `summary`.
```

**The one thing to remember:** an agent gives you a *string* in `raw`. A validator
checks it but does not unpack it. Until a **parse** step turns that string into
keys, nothing downstream can use it — a `render` fails with
`render_unfilled_placeholders`, telling you a parse step is missing (rather than
shipping a broken `{{summary}}` at exit 0). Set `allow_unfilled: true` on the
render only when a field is intentionally optional.

So the rule is simple: **between an agent and any `render` or `branch`, put a parse
step.** You'll see it in every example.

<details>
<summary>The four files (open <code>examples/hello-yaah/</code>)</summary>

- `starter.json` — the pipeline (nodes + graph)
- `starter.local.json` — the root config (fake backend, which pipeline, which input)
- `hello_transforms.py` — the parse function
- `prompts/summarize.md`, `fixtures/input.json`, `templates/output.html`
</details>

---

## Part 2 — Retry when the model misbehaves

Models return junk sometimes. The `summarize` step already handles it:

```jsonc
"summarize": {
  "node": "role:summarize",
  "validators": ["role:check"],   // must be JSON with a "summary" key
  "max_attempts": 3,              // try up to 3 times
  "feedback": true                // tell the model what was wrong, and retry
}
```

To see it kick in, script a bad answer followed by a good one — edit the fake
backend in `starter.local.json`:

```jsonc
"by_model": {"summarize": ["not json at all", "{\"summary\":\"hello\"}"]}
```

Re-run. The first attempt fails the validator; the harness re-prompts the same
agent with the error, and the second attempt passes. One stage, two attempts, no
extra code.

(A validator can **block** the run, or be **soft** — record a note and continue.
See [`node-reference.md`](node-reference.md).)

---

## Part 3 — Pause for a human

→ `examples/review-pipeline/`: `draft → parse → approve → publish`, where
**approve** waits for a person.

```jsonc
"approve": {
  "node": "role:approve",                     // a human_gate node
  "branch": {"on": "decision",                // route on what the human said
             "routes": {"revise": "draft"},   // "revise" → loop back
             "default": "publish"}            // anything else → publish
}
```

Run it. It stops at the gate and saves its place (a *baton*) to disk:

```bash
cd examples/review-pipeline

python3 -m yaah.runtime review.local.json
# → parks, prints:  GATE baton_id=<id> awaiting=review

python3 -m yaah.runtime review.local.json --list
# → shows the parked gate and the question it's asking

python3 -m yaah.runtime review.local.json --resume <id> decision.json
# → delivers {"decision": "approve"} and finishes (publishes)
```

That's the whole human-in-the-loop story: **stop → look → resume**. The baton is on
disk, so the run survives closing your laptop. Change `decision.json` to
`{"decision": "revise"}` and it loops back to `draft` instead.

> For unattended runs, answer in the root config instead and it never stops:
> `"decisions": {"review": {"decision": "approve"}}`.

---

## Part 4 — Do work in parallel

→ `examples/fork-join/`: review one input through three lenses at once, then merge.

```jsonc
"spread": {"fork": ["security", "perf", "style"], "then": "report"},
"join":   {"fanin": {"expect": ["security", "perf", "style"],
                     "wait": "all",                 // or "any", or a number
                     "reduce": "fn:lenses:merge"}}
```

```bash
cd examples/fork-join
python3 -m yaah.runtime review.local.json     # → review.html
```

`fork` sends the same envelope to all three lenses; they run at the same time.
`fanin` waits for them, then hands the collected results to your `reduce` function,
which combines them however you like (the engine never looks inside the data). The
merged report flows on to `report`.

This is how you scatter-gather: multi-lens review, A/B drafts, parallel fetches —
no threads to manage.

---

## Part 5 — Use a real model

Every example used a fake backend. To use a real one, change two lines — the
pipeline stays the same:

```jsonc
// in the root config
"providers": {"claude": {"type": "claude_cli"}},
"default_provider": "claude",

// on the agent node
"model": "claude:claude-sonnet-4-6"
```

`claude_cli` calls your authenticated `claude` CLI. Prefer an API key? Install the
LiteLLM adapter (`pip install -e ".[litellm]"`) and use a `litellm` provider
instead. Keep the fake `.fake.json` overlay around so CI still runs for free.

---

## Part 6 — See what happened

The `[trace]` lines are on by default. For a saved record, add a file sink to the
root config:

```jsonc
"trace": {"mode": "tracer", "capture": ["phase", "cost"],
          "sinks": [{"type": "file", "path": "trace.jsonl"}]}
```

Now each stage records its status, cost, **which branch it took** (so you can see
*why* a run parked or looped), and **a line per retry**. And before a run,
`--explain` prints the final, validated config with defaults filled in:

```bash
python3 -m yaah.runtime <root>.json --explain
```

---

## Part 7 — Let an AI build it for you

This repo ships helpers so an AI assistant can write these pipelines for you —
see **[AGENTS.md](../AGENTS.md)**. Point your tool at it and ask in plain English:

> "A spec → code → review pipeline that pauses for human approval before merge."

It knows the node types, the parse rule from Part 1, and the guardrails, and will
draft the JSON. Run the `.fake.json` overlay to check it — then go real.

---

## Where to go next

| You want to… | Read |
|---|---|
| know every node type and its options | [`node-reference.md`](node-reference.md) |
| know every root-config key | [`root-config-reference.md`](root-config-reference.md) |
| give an agent tools / MCP | [`agent-tools.md`](agent-tools.md) |
| understand batons, idempotency, memory | [`durable-state.md`](durable-state.md) |
| run across machines (NATS) | [`design.md`](design.md) |
| see the real envelope in detail | [`envelope-by-example.md`](envelope-by-example.md) |
