# YAAH roadmap

The engine is feature-complete for its core mission — a domain-free,
file-state, config-driven worker-orchestration kernel with retry/fan-out/
branch/fork-join, human gates with durable suspend/resume, pluggable
prompt/data/mcp/model/transport/store/trace layers, and in-proc → local-bus →
secured-NATS distribution. This is the forward-looking engine work; it carries
no application or deployment-specific context (that lives with the apps built on
yaah).

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

## Documentation
- A distribution & security operator guide (serve subsets, queue-group pools,
  NATS auth/TLS/subject-scoping). Config references already shipped:
  [`node-reference.md`](node-reference.md), [`root-config-reference.md`](root-config-reference.md).
