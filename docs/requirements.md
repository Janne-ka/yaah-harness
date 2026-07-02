# YAAH — Requirements & competitive evaluation

What an agentic harness for **this** workload must do, *why*, how YAAH meets each
requirement today, and how the off-the-shelf frameworks compare — with references.
This doc is the artifact behind the build-vs-adopt decision: we evaluated the field,
and it is the requirements below that the existing frameworks failed, which is why
YAAH exists.

Status tags: ✅ met · 🟡 partial / designed · ⛔ not yet · ↘ **behind the field — borrow.**
Requirement IDs are `RQ-*` (distinct from the `R*` build-items in [TODO.md](TODO.md)).

**Layer tags — generalized (engine) vs specialized (app).** the example app is the first *app*
on YAAH, not part of it; a requirement must say which part is generic and which is
app-specific, and we **generalize the component as far as possible**, pushing only
irreducibly-domain bits to the app. **Generalize by COMPOSING existing core** —
`Node`/`Envelope`/`Comms`/`StoreBackend`/baton/gates/`run_pool` — before adding anything new;
the engine gains a concept only when composition genuinely can't express it (keep it
simple). Boundary test (design.md §10): harness vocabulary
(node, envelope, comms, run, job, queue, slot, baton, gate, verdict) = **🔷 ENGINE**;
domain vocabulary (requirement, spec, RED/GREEN, review-lens, data-audit, findings) =
**🔶 APP**. Many requirements are **🔷+🔶**: a generic engine capability that the app
*configures or specializes* (e.g. per-stage model routing is engine; choosing Haiku for
the sceptic is app). Each requirement below is tagged; if a domain word would have to
enter the engine to satisfy it, the abstraction isn't generic yet.

---

## 0. The prime requirement: a deterministic, token-free control plane

**The point.** The orchestration layer is **deterministic and spends zero tokens.**
Routing, retries, fan-out, branching, and gates are plain code in the harness; a model
is called **only inside a worker node, and only when the graph routes to one.** The
control plane is predictable, replayable, and free; the *only* token spend is the
deliberate, bounded work a stage was configured to do.

**Why this is the spine.** Every general agent framework we evaluated (CrewAI,
AutoGen/AG2, OpenAI Agents SDK, Google ADK, and to a lesser extent LangGraph) makes
**agents first-class citizens**: the control flow itself is an autonomous model loop —
the agent decides routing, converses, and loops until it declares done. That puts a
**nondeterministic, token-spending model in the control plane**: behavior you can't
predict or replay, and spend you can't bound. YAAH puts deterministic code there
instead.

> **RQ-PRIME — Agents are constrained workers; the control plane is deterministic code.** ✅
> The harness owns routing/retry/fork/gate (deterministic, token-free); a worker does
> one bounded job and is replaceable. Autonomy — and therefore nondeterminism — is kept
> out of the hot path.
> *YAAH:* "workers, not citizens" (design.md §1); the harness routes, nodes work.
> *Field:* CrewAI / AutoGen / OpenAI Agents SDK / Google ADK are built on the opposite
> stance (model-driven control flow); adopting one means fighting it the whole way.
> *Closest ally:* Anthropic's **"Building effective agents"** — *prefer composable,
> predictable patterns over autonomous frameworks.* YAAH is those patterns
> (prompt-chaining = `then`; routing = `branch`; parallelization = `fanout`;
> orchestrator-workers = the whole model; evaluator-optimizer = the validator loop)
> made into a small distributed runtime.
> Ref: <https://www.anthropic.com/research/building-effective-agents>

**Cost is a settled consequence, not the topic.** Because the control plane is
deterministic and token-free, per-task spend is predictable and already addressed
(per-step model routing, cheap/deterministic judging, context minimization — §1). This
doc records *how* that holds; it does not re-litigate cost. The open frontier is
**operational** (§3), not economic.

Everything below is a corollary of RQ-PRIME or an operational necessity around it.

---

## 1. Token discipline (settled — recorded here, not a to-do)

Given the token-free control plane (§0), the *worker* spend is then kept lean by the
items below. These are largely in place; they're documented as the *how*, not an open
agenda — don't re-fixate on cost.

**RQ-C1 — Right-size the model per step.** ✅
Each stage picks the cheapest model that passes its bar; cheap models do triage/judge
work, expensive models only the hard synthesis.
- *YAAH:* `provider:model` routing per node + `effort` per `NodeConfig`; pure config,
  no code (`RoutingProvider`).
- *Field:* most agent frameworks bind one model per agent; per-step model choice is
  possible in LangGraph but is wiring, not a first-class knob. CrewAI/AutoGen lean to
  one strong model per agent → costlier by default.

**RQ-C2 — Judgment by cheap/deterministic gates, not expensive self-critique.** ✅
Replace agent self-reflection loops with (a) deterministic validators and (b) a
*separate, cheap* judge — never the worker grading itself.
- *YAAH:* deterministic validators (`json_object`, `expect_field`, `shell_check`);
  **validator retry-with-feedback** (evaluator-optimizer, bounded, escalates to human);
  counterfactual sceptics on Haiku that cold-read output. No self-grading loop.
- *Field:* Reflexion-style self-critique (common in autonomous frameworks) re-runs the
  *expensive* model to critique itself. YAAH pushes that work to Haiku / to free
  deterministic checks. This is a direct token saving and a quality win (independence).

**RQ-C3 — Minimize context; never re-send what the model doesn't need.** 🟡
Token cost is dominated by input context. The model must see the lean slice it needs,
not whole transcripts/envelopes.
- *YAAH:* lean default projection (payload rendered to the prompt; **headers/trace
  never shown**); `get`/`post` + `stateRef` to address data by handle; `tail_only`
  payload trimming; `GitDiffSource` ±N context ("smart get"). **Designed, not built:**
  `envelope_get` tool + pluggable `Filter`s + a Haiku **context broker** that returns
  only the *relevant* slice (TODO R9–R12).
- *Field:* conversation-history frameworks (AutoGen, CrewAI) accumulate and re-send the
  running transcript → roughly quadratic token growth over a multi-turn task. This is
  the single biggest structural cost difference and the core of the eval finding.

**RQ-C4 — Isolation removes re-computation cost.** ✅
A fresh worker per stage, talking via files/envelopes, never re-derives a prior agent's
reasoning; it consumes artifacts, not transcripts.
- *YAAH:* agent isolation is a hard rule; stage output → next stage input; prompt
  caching planned (TODO durability/memory).
- *Field:* shared-memory multi-agent systems re-feed accumulated state; "more agents"
  multiplies token spend. Isolation also dodges the *reasoning-momentum* quality tax.

**RQ-C5 — Measure cost to optimize it (and prove cheaper models suffice).** 🟡 ↘
You cannot control what you don't measure: per-stage tokens/$, model mix, retry rate;
plus an experiment to swap in a cheaper model and score recall vs a baseline.
- *YAAH:* the **Tracer** design captures tokens per `model_call`, an aggregator applies
  a config **price-map** (tokens→$), and the **A/B + recall** apparatus runs a stage on
  a cheaper model and scores it against the canonical run (TODO Observability R4/R8 +
  Orchestration "A/B + recall"). **Designed/agreed, not built.**
- *Field:* **LangSmith** and **Langfuse** ship mature per-run cost/token tracing today;
  this is a place YAAH is **behind — borrow, don't rebuild**: emit OpenTelemetry spans
  (the Tracer design already mirrors OTel) and export to Langfuse/Jaeger/Grafana.
  Refs: <https://docs.smith.langchain.com/> · <https://langfuse.com/docs/opentelemetry/get-started>
  · <https://opentelemetry.io/docs/collector/configuration/>

---

## 2. Quality requirements (cheap quality, not expensive quality)

**RQ-Q1 — Independent verification (the canonical + cold-sceptic pattern).** ✅
The agent that produces work never grades it; a separate, context-cold pass emits
concerns. Cheap, and structurally better than self-review.
- *YAAH:* counterfactual sceptics (cold-read, Haiku) + cross-lens review fan-out.
- *Field:* OpenAI Agents SDK has **guardrails** (closest analogue) — borrow the idea of
  running cheap guardrails *concurrently* with early-abort. Refs:
  <https://openai.github.io/openai-agents-python/guardrails/>

**RQ-Q2 — Evaluator-optimizer loop, bounded, human-escalating.** ✅
Retry with the verdict folded back in, capped, then suspend to a human — not an
open-ended autonomous loop.
- *YAAH:* `validators` + `max_attempts` + `feedback` + `escalate: human`.
- *Field:* the Anthropic evaluator-optimizer pattern; LangGraph can express it but
  YAAH makes it a declarative stage field.

**RQ-Q3 — Grounding before reasoning (anti reasoning-momentum).** ⛔
For brownfield work, gather evidence (grep/read/tests) *before* the high-effort synthesis;
label claims EVIDENCE/INFERENCE/ASSUMPTION and hard-fail an unverified assumption.
- *YAAH:* designed only (TODO Discovery-before-Synthesis: a Discovery stage, the EIA
  protocol validator, a diff-budget validator). Both a quality and a **cost** lever
  (stop paying for high `effort` where grounding would do).

**RQ-Q4 — RED/GREEN contract for code work.** ✅ (in the example app layer)
Tests fail before code, pass after — a deterministic, free quality gate.
- *YAAH:* `shell_check`/`expect_field` RED gate + GREEN refix loop, worktree-isolated.

---

## 3. Operational requirements (batch scale, production)

**RQ-O1 — Durable execution / resume after crash.** 🟡 ↘
A parked or crashed run must resume without re-spending tokens on completed stages.
- *YAAH:* durable **baton store** (FileBackend), gate **suspend/resume**, **idempotency**
  once-node — **Level 1 done** (cross-process resume proven). **Missing:** a per-run
  **step journal** so a resumed/crashed run *skips already-completed stages* (replay),
  which is the real token-saver (TODO durability "Level 2").
- *Field:* **Temporal**, **DBOS**, **Restate** are the gold standard — deterministic
  replay that skips completed activities. Don't match their guarantees; **borrow the
  journal mechanism** and study **DBOS** (durable execution as a library, no server).
  Refs: <https://docs.temporal.io/> · <https://docs.dbos.dev/> · <https://restate.dev/>

**RQ-O2 — Observability / tracing.** 🟡 ↘  (see RQ-C5 — same Tracer, cost is one capture)
Per-step timing/tokens/tool-calls assembled by correlation id, exportable to standard
tooling. *YAAH:* Tracer design agreed (OTel-shaped), not built. *Field:* LangSmith /
Langfuse / OTel — **behind; borrow** the OTLP exporter.

**RQ-O3 — Human-in-the-loop gates.** ✅
Pause for a decision durably (minutes→days), inspect parked runs, resume with a typed
decision.
- *YAAH:* `human_gate` (await) → suspend → BatonStore → `drive()` / `--list` / `--resume`;
  soft concerns surface at the gate.
- *Field:* **LangGraph** `interrupt()`/`Command` and **Temporal signals** are comparable
  (LangGraph's mid-node interrupt is richer — deliberately *not* adopted, it breaks the
  clean stage boundary). Borrow only: validate the resume decision against a schema.
  Ref: <https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/>

**RQ-O4 — Distribution, placement, multi-tenant security.** ✅ (notably ahead)
Run local, cloud, or split; pin resource-bound work; isolate tenants on a shared bus.
- *YAAH:* NATS with **auth + TLS + subject-scoping + queue-group shared pools**;
  `placement` tags resolve a host's role set; capability-based state authorization
  designed (TODO). The on-the-wire security posture is **ahead of most agent frameworks**.
- *Field:* **Dapr / Dapr Agents** is the closest building-block model but far heavier
  (sidecars). Refs: <https://docs.dapr.io/> · <https://nats.io/>

**RQ-O5 — Bounded durable job scheduler (🔷 ENGINE) + requirement backlog (🔶 APP).** ⛔
Split deliberately so the engine stays domain-free and **maximally reuses existing core**:
- 🔷 **ENGINE — a bounded durable job scheduler over opaque `(pipeline, input)` jobs.**
  Build it from EXISTING components, no new concepts (keep it simple): the **`StoreBackend`** for
  the backlog (a new namespace, not a new store type); the **baton AS the run record**
  (running/parked/done is already baton status + durable + resumable — no parallel state
  model); the existing **suspend/resume + `--list`/`--resume` mailbox** for gate-wait. The
  only new code: a small **`run_pool`** (admission via `asyncio.Semaphore`+`gather` over
  `harness.run`/`drive`) + a thin **admit loop** pulling pending jobs from the namespace.
  A queued job → admitted → becomes a baton; that's the whole lifecycle.
- 🔶 **APP (the example app) — the requirement backlog.** The domain layer the engine scheduler
  serves: the `requirement` shape/lifecycle, human curation/prioritization, the
  requirement→`(factory-pipeline, input)` mapping, factory gate semantics (spec-review,
  data-audit), and any requirement-review UI. Stays out of the engine entirely.
- *Field:* **Inngest** / Temporal / a job queue do the engine part as products; here it's
  cheap because we compose existing YAAH core rather than adopt a runtime. Ref:
  <https://www.inngest.com/docs>
- *Status:* engine scheduler not built (small, planned); app backlog is downstream of it.

---

## 4. Platform & DX requirements

**RQ-P1 — Simplicity / comprehensibility.** ✅ (the decisive win)
Three concepts; the whole system fits in your head; stdlib-only core, lazy heavy deps.
*Field:* LangGraph/Temporal are far heavier; only OpenAI Swarm / Pydantic AI / LlamaIndex
Workflows are in the same weight class, and they stay single-process.

**RQ-P2 — No lock-in / polyglot / swappable ends.** ✅
A different app on top and a different transport below each swap without touching the
core (it depends only on `Node`/`Comms` interfaces). JSON `Envelope` wire contract →
nodes in any language over NATS.
- *Field:* frameworks impose their own runtime/lock-in. YAAH's engine/adapters split +
  wire contract is a genuine differentiator.

**RQ-P3 — Config ergonomics under maximal composability.** 🟡
"Hug the world" composability (modes × captures × filters × sinks × …) risks a monster
config; the surface must stay ergonomic.
- *YAAH:* layered defaults + `explain`, ready-made **base configs** + include/drop,
  **schema validation**, and (strategic) an **AI config-generator** skill (TODO R13–R17),
  explicitly modeled on the best-loved base-config+override systems.
- *Field / refs (study their config UX):* Spring Boot auto-config + `--debug`; Terraform
  `optional()`/`validation`; **Kustomize** base+overlay (plain-data, *not* Helm
  templating); **Crossplane** Compositions (typed recipes); **OTel Collector** &
  **Kafka Connect** (the closest structural analogues). Refs in
  [TODO §"Config / DX"](TODO.md).

**RQ-P4 — Typed contracts at stage boundaries.** 🟡 ↘
Payloads/outputs validated against a schema so contract drift fails fast (and prompt
stubs can't silently produce the wrong shape).
- *YAAH:* `json_object`/`expect_field` today; a generic **`json_schema` validator** is
  the cheap next step (a Node, fits the validator slot).
- *Field:* **Pydantic AI** / LangGraph typed state are stronger here — borrow the
  *idea* (schema-validated I/O) without their type systems.
  Ref: <https://ai.pydantic.dev/>

---

## 5. Scorecard

| Requirement | YAAH | Best-in-class alt | Verdict |
|---|---|---|---|
| RQ-PRIME deterministic, token-free control plane | ✅ | Anthropic patterns (ally); agent-frameworks put a model in the control loop | **YAAH ahead for this workload** |
| RQ-C1 per-step model | ✅ | LangGraph (wired) | ahead |
| RQ-C2 cheap/deterministic judging | ✅ | — | ahead |
| RQ-C3 context minimization | 🟡 (broker designed) | — | ahead in design; finish R9–R12 |
| RQ-C4 isolation | ✅ | — | ahead |
| RQ-C5 cost measurement | 🟡 ↘ | LangSmith / Langfuse | behind — borrow OTel |
| RQ-Q1 independent verification | ✅ | OpenAI guardrails | ahead |
| RQ-Q2 evaluator-optimizer | ✅ | LangGraph | on par |
| RQ-Q3 grounding-first | ⛔ | — | gap (designed) |
| RQ-O1 durable replay | 🟡 ↘ | Temporal / DBOS / Restate | behind — borrow journal |
| RQ-O2 observability | 🟡 ↘ | LangSmith / Langfuse / OTel | behind — borrow |
| RQ-O3 human gates | ✅ | LangGraph / Temporal | on par |
| RQ-O4 distribution + security | ✅ | Dapr (heavier) | ahead vs agent-frameworks |
| RQ-O5 backlog scheduler | ⛔ ↘ | Inngest / Temporal | gap — consider adopt |
| RQ-P1 simplicity | ✅ | Swarm / Pydantic AI | ahead |
| RQ-P2 no lock-in / polyglot | ✅ | — | ahead |
| RQ-P3 config ergonomics | 🟡 | Terraform / Crossplane / OTel | designed, well-researched |
| RQ-P4 typed contracts | 🟡 ↘ | Pydantic AI | behind — cheap to close |

---

## 6. Reading of the scorecard (honest)

- **Where YAAH genuinely wins is the deterministic, token-free control plane**
  (RQ-PRIME): predictable, replayable orchestration with model calls only where the
  graph deliberately routes to one. The field puts a nondeterministic model loop in the
  control plane; YAAH puts code there. Token discipline (RQ-C*) falls out of this and
  is settled — not the headline.
- **Where YAAH is behind is operational, and the fixes are borrows, not inventions**
  (RQ-O1 / RQ-O2 / RQ-P4, plus RQ-C5 for measurement): trace/observability (OTel/
  Langfuse), durable replay (Temporal/DBOS journal), typed I/O (Pydantic-style schema).
  Each lands as a port/adapter/node — see the build order in [TODO.md](TODO.md)
  (Tracer R1–R8, durability, schema validator).
- **The one place to seriously consider adopting over building** is RQ-O5 (backlog
  scheduler) — a job queue / Inngest / Temporal is a solved problem the factory needs
  and the harness doesn't address.
- **Biggest risk is not in this table:** the design is sound but **unproven at the
  250-task batch scale** — the proven factory still runs on bash; the YAAH port isn't
  fully e2e. The requirements above are the bar; finishing the e2e port is what turns
  "deterministic control plane" from a claim into a demonstrated property.

---

## 7. Appendix: LangGraph — the one serious "adopt instead of build" candidate

LangGraph spans a **spectrum** — LangChain's own docs frame it as *"workflows and
agents"* (echoing Anthropic): a **workflow** end (deterministic edges, token-free
control plane — like YAAH) and an **agent** end (`create_react_agent`, supervisor/
swarm, tool loops — where the **model decides routing and when to stop**, which is a
first-class agent in the control loop). So unlike CrewAI / AutoGen (agent-only),
LangGraph *can* satisfy RQ-PRIME — but it doesn't *enforce* it, and its center of
gravity is the agent end. That makes it the only framework where adopting is a real
question, so it deserves a direct verdict.

**The key distinction:** *LangGraph **lets** you be deterministic and isolated; YAAH
**makes** you.* In LangGraph the cost/predictability guarantee is **by convention** —
reach for the flagship `create_react_agent`, or write a conditional edge that routes on
a field the LLM just produced, and the model is back in the control loop (token spend +
the classic routing failures: wrong branch, loops, won't-stop). In YAAH it's
**structural**: there is no first-class agent to reach for, the only control plane is
deterministic code, and isolation is the default because workers exchange envelopes, not
shared state. You can't *accidentally* make an agent first-class. For a batch factory
that wants the guarantee, "a framework you can't misuse into expensive agentic routing"
is itself the value.

**Where LangGraph fits us (the uncomfortable part):**
- **It *can* run the same deterministic stance** (workflow-mode edges, model only inside
  a node) — so determinism isn't "LangGraph can't"; it's "LangGraph won't stop you from
  abandoning it." The differentiator is *enforcement*, not capability.
- **It already ships the three things we're behind on.** Durable **checkpointers**
  (Memory/SQLite/Postgres) give resume + time-travel (RQ-O1); **`interrupt()` /
  `Command(resume=)`** are mature human-in-the-loop (RQ-O3); **LangSmith** is mature
  cost/trace observability (RQ-C5/O2); plus token **streaming**. Adopting would get
  these for free instead of us building Tracer R1–R8 and the durability journal.
- **Deployment/scale exists** via LangGraph Platform/Server (queues, cron, horizontal
  scale) — our missing RQ-O5.
- Large ecosystem, maintained, and people already know it.

**Where it doesn't fit us (why YAAH still earns its place):**
- **Shared state vs message-passing isolation — the deciding mismatch.** LangGraph nodes
  read/write a shared state object (channels + reducers). YAAH's hard isolation — a fresh
  worker per stage that sees *artifacts, not transcripts*, so the reviewer never inherits
  the coder's reasoning — is the spine of the example app quality story (counterfactual
  sceptics, agent isolation). In LangGraph you'd impose that discipline *against* the
  framework's grain and police it forever. The grains run opposite.
- **Distribution & polyglot.** OSS LangGraph runs a graph in **one Python process**;
  real cross-machine cloud-burst (our parallel review fan-out) and **nodes-in-any-
  language** mean either LangGraph Platform (commercial, heavier, lock-in) or building
  distribution yourself anyway. YAAH is **NATS-native** (auth/TLS/subject-scoping/queue
  groups) with a **JSON-envelope wire contract** — a genuine fit-for-purpose edge.
- **Config/markdown-as-data ethos.** YAAH pipelines are JSON config + markdown prompts,
  editable with no Python; LangGraph graphs are Python code. For a factory whose agents
  *are* markdown skills, that's a real ethos clash.
- **Weight & lock-in.** `langchain-core` dependency surface, the commercial pull of
  LangSmith/Platform, and a larger conceptual API (typed state, channels, reducers,
  checkpointers) vs YAAH's stdlib core that fits in your head (RQ-P1/P2).
- **You build the same domain nodes either way.** Worktree isolation, RED/GREEN gates,
  shell gates, the counterfactual pattern — LangGraph gives none of these; that work is
  identical under both.

**Verdict.** LangGraph is the right tool if **durability + observability + HITL +
deployment dominate** and isolation can be enforced by convention — it would save
months of operational building. YAAH is the right tool if **isolation, NATS-native
polyglot distribution, config/markdown-as-data, and zero lock-in** are load-bearing —
which, for the example app, they are. The honest cost of choosing YAAH is **re-implementing
the operational layer LangGraph already has** (checkpointing/replay, tracing, deploy).
That trade is only worth it if the four differentiators are real to you; they are, but
it means the borrow list (§3 / TODO) isn't optional polish — it's paying down the gap to
the thing we declined to adopt. A defensible middle path: keep YAAH's model and
**borrow LangGraph's checkpointer idea** (the durable replay journal) rather than its
runtime.
