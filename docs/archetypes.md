# Pipeline archetypes

Five shapes. Almost every YAAH pipeline is one of them. When you sit
down to write a new pipeline, the workflow is:

1. Read the **Reach for this when** lines below.
2. Pick the nearest archetype.
3. Open its **Reference example**, copy it, adapt.

If your idea doesn't fit any archetype, that's signal. Usually one of
two things is true: (a) you've found a genuinely new shape worth its
own archetype (rare — open an issue), or (b) you're bending the
problem into a non-archetype because you skipped this page (common —
re-read).

YAAH is deliberately opinionated about this. The engine is small and
domain-free precisely so the *shapes* can carry the meaning. Picking
the closest archetype and adapting is the workflow — not designing
from scratch.

---

## 1. linear

A deterministic sequence of stages. No branches. No human gate.
Output of stage N is input to stage N+1.

**Reach for this when:** you want to chain "transform → call a model
→ parse the reply → render". The pipeline reads top-to-bottom; nobody
needs to wait, nobody needs to approve, nothing forks. Often the
right first sketch even if you later add branches or gates — get the
spine working linearly before complicating it.

**Don't reach for this when:** any step needs a human decision, or
the work can run in parallel, or one branch's outcome should skip
another.

**Reference example:** [`examples/hello-yaah/`](../examples/hello-yaah/).
Smallest real pipeline: `agent → validate+retry → parse → render`.
Read the whole thing in one sitting.

**Variations seen in the wild:**
- Add validation feedback loops (`max_attempts`, `feedback: true`) —
  hello-yaah already does this on the agent stage.
- Swap the final `render` for a `transform` that posts an HTTP webhook
  or writes a file — the shape stays linear.
- Replace `agent` with a `transform` whose `fn:` target hits a non-LLM
  API; you've made a non-AI deterministic pipeline. YAAH is still the
  right runtime for the trace + retry + suspend story.

**Known footguns:**
- An `agent`'s output is a STRING in `payload["raw"]` — nothing merges
  it into the payload top-level until a `transform` does. Every
  `agent → render` edge needs a `parse` transform between them or
  `render` fails with `render_unfilled_placeholders`. See [AGENTS.md
  §"Data-flow contract"](../AGENTS.md).
- Transforms with `call: "envelope"` REPLACE the payload by default.
  Use `return {**envelope.payload, ...new_keys}` to enrich rather than
  overwrite. See [`docs/node-reference.md`](node-reference.md).

---

## 2. branch-with-gate

A stage produces something, a human reviews it, and the decision
routes the run to one of N continuations. Suspend/resume is durable —
the gate may park for hours or days; the engine survives restarts.

**Reach for this when:** there's a judgment call no rule can encode —
"is this draft good enough to send?", "approve / revise / reject?",
"is this auto-rejected report actually a real CVE?". The right answer
is "a human looks for 30 seconds and clicks", not "tune the prompt
until it's never wrong."

**Don't reach for this when:** the decision is deterministic
(branch on a value the previous stage already computed — that's
plain `branch:`, no gate needed) or the run is fully unattended
(use [decisions auto-approve](root-config-reference.md) instead of a
human gate).

**Reference example:**
[`examples/review-pipeline/`](../examples/review-pipeline/). A draft
stage → human gate → branch on the human's decision → finalize OR
loop back to revise.

**Variations seen in the wild:**
- **Decision forms** (ADR-0002): swap a freeform-text gate for a
  structured `form: "approve_or_revise"` so the operator's driver
  (or `yaah baton-schema`) knows the exact decision shape.
- **`concerns_from`** on the producer stage: a soft validator's
  flagged-but-not-blocking concerns ride into the gate's ask so the
  reviewer sees what the previous stage was uncertain about.
- **`escalate: "human"`**: a stage that exhausts `max_attempts`
  escalates to a human gate automatically — the gate becomes the
  fallback for *any* hard failure, not just an explicit step.

**Known footguns:**
- The baton is single-shot: once resumed, gone. Resuming a delivered
  baton raises `KeyError` with the diagnostic "run `yaah list`."
- The gate's parked envelope has a TTL. Abandoned gates get swept;
  the baton ID will resolve to the same error. Set `ttl` realistically
  on the gate node.
- The operator UX depends on `yaah list` + `yaah baton-schema <id>`
  surfacing the right decision shape. Always declare `form:` on
  human_gate nodes; otherwise `yaah baton-schema` exits with
  "no form declared." See [`docs/decision-forms.md`](decision-forms.md).

---

## 3. fork-fanin

Fan out to N parallel branches doing independent work, then reduce
their outputs into one combined result.

**Reach for this when:** you want **multiple perspectives on the same
input** (different lenses, different models, different prompts) and a
single downstream artifact that combines them. The branches don't
depend on each other; the reducer owns the merge logic.

**Don't reach for this when:** the branches need to coordinate
mid-flight (they don't — the engine doesn't support cross-branch
messaging), or one branch's output is the next branch's input (that's
linear, not fork).

**Reference example:** [`examples/fork-join/`](../examples/fork-join/).
Three review lenses run in parallel; a `reduce` function merges their
findings into one report.

**Variations seen in the wild:**
- **A/B model comparison**: branches differ only in `model:` —
  arch-drift's A/B variant pairs sonnet (loose prompt) against haiku
  (tight prompt) to demonstrate the per-model-prompt asymmetry. See
  [`docs/case-study/prompt-tuning/`](case-study/prompt-tuning/).
- **Mixed-tier ensembles**: the cheap model runs on every branch; the
  expensive model only runs on the branches the cheap model couldn't
  refute. Wire as two forks in sequence with a filter between.
- **Reducer chooses, not merges**: the `reduce: fn:...` function can
  return one winner instead of a combined report. The shape doesn't
  care; the function does.

**Known footguns:**
- `fanin.expect` lists **fork branch names**, not the names of the
  last stage in each branch. Easy mistake; the error is good when
  you hit it.
- Gates inside a fork are not supported — a gate suspends a *baton*,
  and there's one baton per run, not per branch. If you need
  per-branch suspension, you want sequential runs, not a fork.

---

## 4. instrumented

A pipeline that measures itself — token cost per stage, model
choice, A/B-able by config, optional human gate, optional
auto-approve overlay. The shape you graduate to once a pipeline is
running on real money and real maintainers.

**Reach for this when:** a pipeline is past the prototype stage and
you (or a customer) needs to **answer real questions about it**: how
much did this run cost, which model was used, why did it choose that
branch, how does haiku compare to sonnet on the same input. The
instrumentation isn't decoration — it's load-bearing for the
operator who's paying the bill or signing off on the artifact.

**Don't reach for this when:** you're prototyping the pipeline shape
itself. Start linear; promote to instrumented later. Adding the
attacher + A/B + auto-approve overlay during prototyping confuses
"is the pipeline shape wrong?" with "is the instrumentation noisy?"

**Reference example:** [`examples/arch-drift/`](../examples/arch-drift/).
Multi-stage pipeline (snapshot → extract → render → diff → gate →
land) with the attacher port (ADR-0003) surfacing token cost on
every model call. Ships A-only and A/B variants, plus an offline
fake-provider overlay AND an unattended auto-approve overlay.

**Variations seen in the wild:**
- **A-only** (single-model production shape): one agent stage with
  `attach: ["fn:transforms:UsageAttacher"]`, cost-per-run lands in
  the final artifact.
- **A/B** (fork into two models, same prompt or per-model prompts,
  reduce into a side-by-side report). The honest comparison shape —
  see [`docs/case-study/prompt-tuning/`](case-study/prompt-tuning/)
  for why per-model prompts matter.
- **Auto-approve overlay**: a `.dogfood.json` config extends the
  production root and adds `decisions: {<gate>: {auto: "approve"}}`
  so the gate never blocks. Use for CI / cron / unattended runs.
- **Real / fake overlays**: a `.local.json` swaps real providers for
  `fake_scripted` so the whole pipeline runs offline (deterministic,
  free, no API key) for tests and demos.

**Known footguns:**
- Attachers ship in CONSUMER code, never in `src/yaah/` — the engine
  ships ZERO built-in attachers (ADR-0003). Copy from
  [`docs/cookbook/attachers/usage.py`](cookbook/attachers/usage.py),
  not import.
- Attacher captures must be declared in `trace.capture` at the root.
  The builder rejects at LOAD time if a missing capture would make
  the attacher silently return `{}` — the error message names the
  capture to add.
- `_extends` chains (RFC 7396 merge-patch) use `null` to DELETE a
  key inherited from the parent. Otherwise typed-block overrides
  merge with the parent and surprise you. See
  [`docs/root-config-reference.md`](root-config-reference.md).
- `MERMAID_RENDERER=:canned` returns a FIXED placeholder SVG, not a
  real render — useful for offline overlays, misleading if you forget
  you set it. The transform prints a stderr warning when canned;
  silence with `YAAH_CANNED_QUIET=1`.

---

## 5. meta-tool

A pipeline whose input is **another YAAH config**. The shape we use
to build tools that operate on YAAH configs themselves — visualizers,
linters, transformers, migrators.

**Reach for this when:** you're building developer tooling for the
YAAH ecosystem itself. "Show me a diagram of this config." "Check
that all `fn:` targets in this config resolve." "Upgrade this config
from v1 to v2 conventions." The pipeline is short and the *input
parsing* is the bulk of the work.

**Don't reach for this when:** the tool doesn't actually need to be a
pipeline — sometimes a 30-line `argparse` script is the right answer.
The meta-tool shape earns its keep when the work benefits from
agent + trace + cost-tracking + the rest of YAAH's runtime. If you'd
write the tool the same way in plain Python, do that.

**Reference example:** [`examples/config-flow/`](../examples/config-flow/).
Visualizes any YAAH root config as an SVG (extends chain, pipeline
graph, fixture, registries). Default haiku-strict; A/B variant for
sonnet vs haiku comparison.

**Variations seen in the wild:**
- **Visualizer**: render some property of the input config (the only
  variation in the wild today — config-flow itself). Future
  candidates: trace JSONL → flame graph; baton store → mailbox HTML.
- **Linter / validator**: walk the input config and produce a report
  on what's missing or surprising. Likely future direction.
- **Transformer / migrator**: input config in, modified config out.
  Useful when conventions shift and existing pipelines need
  mechanical upgrades.

**Known footguns:**
- The input config probably has its own `_extends` chain. Walk it
  fully before processing (use `_deep_merge` in your transforms —
  see config-flow's implementation) or you'll process the wrong
  inherited values.
- Beware writing a tool whose output is another YAAH config and
  whose input is a YAAH config and whose pipeline IS a YAAH config.
  The dog-food cube is fine, but document it clearly — readers get
  lost. config-flow's README does this with an "inheritance map"
  table.
- A meta-tool should generally NOT need a human gate — its work is
  regenerable. config-flow has no gate by design; the user re-runs
  whenever they want a fresh artifact.

---

## When archetypes don't fit

If your real pipeline doesn't fit any of the five:

1. **First**: re-read above. The five names are intentionally narrow;
   match on the *shape*, not the *domain*. A "summarize meeting notes"
   pipeline is `linear`; a "summarize meeting notes with human
   review" is `branch-with-gate`; "summarize from 3 angles and merge"
   is `fork-fanin`.
2. **If still no fit**: write the pipeline as if it were the
   nearest archetype, even if it feels a little forced. Often the
   "doesn't fit" is the pipeline doing too much — splitting it into
   two simpler pipelines (each of a known archetype) is the answer.
3. **If after both above you're sure it's a new shape**: open an
   issue describing the shape (not the domain). A genuinely new
   archetype gets its own section here. Don't expect that often.

## Cross-references

- [`AGENTS.md`](../AGENTS.md) — start here for the engine's mental
  model and authoring rules.
- [`docs/quickstart.md`](quickstart.md) — 5-minute first run.
- [`docs/cookbook/`](cookbook/) — copy-paste reference patterns
  (currently: the canonical attacher).
- [`docs/case-study/prompt-tuning/`](case-study/prompt-tuning/) —
  worked example of A/B comparison driving prompt strategy
  (variation of the `instrumented` archetype).
- [`docs/node-reference.md`](node-reference.md) — every node type
  and its config.
- [`docs/root-config-reference.md`](root-config-reference.md) —
  every root / deployment-config key.
- [`docs/decisions/`](decisions/) — ADRs explaining *why* the shape
  is what it is.
