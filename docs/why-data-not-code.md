# Why a YAAH pipeline is data, not code

The first question anyone asks: *if I can already write Python, why describe the
pipeline in a JSON file at all?* The answer is a **boundary**, not a preference:

> **Python writes the work. JSON declares the wiring.**

You never write the *algorithm* in JSON — the actual work (call a model, transform
some data, render a file) is plain Python in a file next to the config. The JSON
only says *which steps run, in what order, and where the flow forks.* So the real
question is narrower: why declare the **wiring** as data instead of as Python
`if`/`else` and function calls?

## Because the runtime can act on a structure it can read

Once the wiring is data the runtime can read, it can **do things to your pipeline
that it could never do to ordinary code**:

- **Pause for a human — for days — and survive a restart.** When a run reaches a
  human gate, YAAH writes the whole half-finished run to disk. The process can
  exit; tomorrow a *different* process loads it and continues from the gate. In a
  plain script, "stop here, wait for a person, then resume exactly where you were
  after a reboot" means hand-rolling state serialization and a resume entry
  point. The declared graph makes it nearly free.
- **Retry one step with feedback**, route on a result, **measure cost per step**,
  run a step on another machine, **swap the model** — by reading or editing the
  structure, none of it touching the work code.
- **See, diff, lint, even draw the flow** — it's just data. (A sibling tool in
  this repo, `config-flow`, literally renders any pipeline's JSON as a diagram —
  possible only because the structure is data.)

## The deeper reason: a structure you can reason about

"Edit data to change the shape" sounds minor until you see what a *constrained
data structure* gives you that *arbitrary code* never will:

- **You can check a change before you trust it (safety).** A pipeline edit can
  only do pipeline-shaped things — name a node, draw an edge, set a value. Code
  enters only through explicit, auditable `fn:` references. So a change to the
  wiring has a blast radius you can verify mechanically. YAAH even ships a
  deny-by-default lint for *AI-proposed* config edits: leaf tweaks pass; anything
  code-equivalent, or that loosens a safety bound, is rejected — at load, before
  anything runs. **You cannot deny-by-default-lint a Python diff** — any line of
  code can do anything. If you want an AI (or a junior, or an untrusted source)
  to change a pipeline *safely*, the shape has to be data.

- **You test the plumbing once, not per pipeline — but the data flow still needs
  checking.** Routing, retries, the durable pause, error handling are the
  harness: tested once and reused, so you never re-earn orchestration tests.
  What stays yours is the **work** (your `fn:` functions) and the **data flow** —
  each stage reads keys that *earlier* stages must have produced, in order. And
  that's the honest catch: the load check catches structural mistakes today
  (dangling edges, unknown node types, one common parse trap), but **not yet**
  the general question *"is this `{{key}}` ever produced upstream?"* — that still
  surfaces at runtime. The right answer there isn't a test per pipeline, it's a
  **verifier** — something that traces which keys each stage *produces* against
  the keys each stage *reads*, and tells you when a stage reads a key nothing
  upstream makes. (Today an AI authoring assistant does this by reading the JSON,
  and running the `.fake` overlay surfaces the rest as specific errors.) That a
  *constrained structure even admits* such a check — that you can trace it just by
  reading the wiring — is the point; you can't trace that through hand-written
  Python control flow.

- **Tools — and models — can build it (construction).** Data is generable and
  transformable by other programs: scaffold a pipeline, draw it, migrate a
  hundred of them mechanically, or have a model *emit* one. That last point is the
  deep one for AI systems — **a model can produce config you can validate against
  a schema; it cannot produce code you can validate the same way.** Verifiable
  generation is only possible because the artifact is constrained data.

The through-line: **a constrained structure is something you can analyze,
constrain, and generate — safely and mechanically. Turing-complete code is not.**
That is the real reason the wiring is a JSON file.

## When *not* to reach for it

The honest flip side: **if you need none of that — a short, linear, unattended
job — a plain Python script is the right answer, and YAAH would be overkill.** The
JSON earns its keep when the work outgrows a script: a human in the loop, retries,
cost tracking, distribution, A/B comparison, or an AI authoring the pipeline.

## Going deeper

- **See it concretely:** the [`arch-drift` walkthrough](../examples/arch-drift/HOW-IT-FITS-TOGETHER.md)
  traces one real pipeline end to end — JSON, Python, model, and the payload that
  ties them together.
- **The full rationale:** [`design.md`](design.md) (workers-not-citizens, the
  three comms modes, suspend/resume) and [`architecture.md`](architecture.md)
  (the layers and how concepts map to code).
