# YAAH roadmap

The engine is feature-complete for its core mission — a domain-free,
file-state, config-driven worker-orchestration kernel with retry/fan-out/
branch/fork-join, human gates with durable suspend/resume, pluggable
prompt/data/mcp/model/transport/store/trace layers, and in-proc → local-bus →
secured-NATS distribution. This is the forward-looking engine work; it carries
no application or deployment-specific context (that lives with the apps built on
yaah).

## Fault tolerance

A 2026-06 fault-tolerance pass landed the engine core: in-proc exceptions converge
to the verdict path (no run-killing tracebacks); a separate `error_retries` budget
retries transient infrastructural faults with backoff without spending
`max_attempts`; `_settle` preserves a parked baton on an infrastructural error
(only a logical `StageFailed` evicts); default ceilings (shell timeout, `_drive`
step limit) end the "hangs forever" class; cheap "why" observability (branch
route/decision attrs, per-attempt retry spans, absent-route marker, structured
shell-timeout); a render `unfilled`-placeholder marker; a wrapped fan-in reduce
(a broken reduce is an observable error span, not a silent hang); and NATS
connection-state callbacks. Forward work from the same analysis:

- **Per-stage input checkpoint** — checkpoint the failing stage's input onto the
  baton so an *interrupted* run RE-RUNS its current stage on resume (the current
  preserve leaves it resumable but not re-drivable). Pairs with a durable
  per-stage cursor (crash-between-stages).
- **RecordingBackend + input snapshot → offline replay** — capture
  `(rendered_prompt, model, opts, completion)` per call + the stage input at
  failure, so any failed stage becomes a `ScriptedProvider` fixture. Blocked on a
  redaction/retention policy (same reason `reasoning` capture is opt-in).
- **Baton CAS + graph fingerprint + delete-tombstone** — optimistic-version save
  (double/concurrent resume is a silent lost-update today), a wiring-hash checked
  on resume (a graph change can resume onto a stale/wrong stage), and a TTL'd
  tombstone so an expired baton is distinguishable from one that never existed.
- **NATS hardening** — split `NoRespondersError` (deploy fault, fail fast) from
  `TimeoutError` (slow node); idempotency-by-default at the `serve` boundary
  (redelivery → double side-effect today); explicit namespaced queue-groups; and
  wire the connection-state tracer through the transport factory (the capability
  exists; the runtime doesn't pass the tracer yet).
- **Build-time deep-validate (opt-in)** — resolve `fn:` transform targets and
  prompt/template file existence at build, so a typo fails at build instead of a
  raw raise mid-run. Opt-in because it imports user modules.
- **`finish_reason` capture** — backends never read it, so a max_tokens truncation
  masquerades as validator-exhaustion; surface it and retry with a higher budget.

## Agents
- **Deterministic pre-emit self-checks** — an agent may run JSON/key validators
  on its own output and re-prompt once BEFORE emitting, never an agent-judge
  (no self-correction against its own critic).
- **`stateRef` handles** — a header convention so a node addresses large working
  memory in the substrate by handle instead of carrying it in the envelope.

## Observability
- **Tracer-as-UX** — a live consumer of the span stream (stage waterfall,
  cost/time-so-far at gates, run replay).
- **Reasoning capture** (compliance-driven) — an opt-in `reasoning` contributor
  with access-controlled storage, for auditable decision trails.
- **Buffered / async-drain trace sink** — when throughput makes the hot-path
  publish + per-record file open cost bite.
- **OTLP exporter sink** — spans are OTel-aligned; one OTLP sink reaches
  Jaeger/Grafana/Langfuse for free.
- **Langfuse OTLP-native sink** — `LangfuseTraceSink` now maps onto both the v2
  manual client and the v4 OTEL client (`.start_observation`), detected at
  runtime. The v4 path attaches observations to the trace flat (by `corr` as the
  OTel trace id) without reconstructing parent/child nesting. The version-proof
  end state is a generic OTLP sink (above) that reaches Langfuse over OTLP rather
  than chasing the bespoke client across majors; at that point the per-version
  branching here can retire.

## Configuration & control plane
- **Authorized live config push** — an authorized principal publishes a
  leaf-config change over the bus, gated by the same capability layer as
  resume/clear; the mutable surface is the one allow/deny table (leaf,
  non-code-equivalent) shared with the overlay lint and the per-call re-read.
- **Profile / typed-template layers** — a named-profile DSL and a typed "recipe"
  layer ABOVE the validated foundation; build only if the foundation + the
  config-authoring assistant prove insufficient.

## Durability & distribution
- **Durable backends** — a JetStream/KV store extender, a KV-backed `mem:`
  source/sink, authorization-scoped state access for multi-user deployments.
- **Cloud deployer** — root config → serverless/containers over NATS,
  placement-driven (the host declares WHERE it runs; the deployer assigns roles).
- **Polyglot nodes** — document the JSON-envelope + subject wire contract so a
  worker can be written in any language; optional thin per-language clients.
- **Dead-letter + restore** for unreachable nodes (durable retention; the baton
  is the resume cursor).
- **Bounded durable job scheduler** — a generic admission-controlled queue with
  per-key concurrency and retries (the in-proc Semaphore is the degenerate case).

## Borrowings under evaluation
From Temporal/DBOS/Restate (durable replay journal + an enforced determinism
invariant), LangGraph (richer gate inspect + typed decisions, graph
visualization), OpenAI Agents SDK (concurrent guardrails with early-abort), and
the eval-as-a-harness-run pattern. Each adopted only when a concrete need lands.

## Onboarding & adoption (evaluation-time friction)

From a first-look review — the friction that decides "should I try / trust this?"
before any code is read:

- **Parse-gap lint at config-load** (runtime guard → load-time guard). The
  most-documented footgun — an `agent` stage feeding a `render`/`branch` with no
  `parse` transform between — now *fails at render-time* (`render_unfilled_placeholders`)
  instead of shipping a literal `{{placeholder}}` at exit 0. A *load-time* warning
  would catch it even earlier, before the run starts. Add to `validate_pipeline`:
  walk the graph; for
  each `agent` stage whose direct successor (`then` / a `branch` route / a `fork`
  target) is a `render` node — or which `branch`es on a key the agent doesn't set —
  emit "warning: agent stage X feeds Y with no parse between them". Warn, don't fail
  (a few legitimate shapes exist). This is the highest-leverage DX fix.
- **PyPI release (0.1).** `pip install yaah` failing is the first silent rejection
  at evaluation time — most people never get to the docs. Publish (Phase 3): claim
  the name, ship the wheel, pin a version. The metadata is ready (`pyproject.toml`
  extras + `pixi.lock` story); this is a release action.
- **Trust signals** (mostly organic, but the honest ones are cheap): surface the
  provenance the engine has earned (proven driving a real multi-stage code-change
  app over ~hundreds of tasks), a visible CI-green badge, `LICENSE` + a short
  `CONTRIBUTING`. Multi-author history and stars accrue with use; don't fake them.

## Documentation
- A distribution & security operator guide (serve subsets, queue-group pools,
  NATS auth/TLS/subject-scoping). Config references already shipped:
  [`node-reference.md`](node-reference.md), [`root-config-reference.md`](root-config-reference.md).
