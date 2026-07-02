# YAAH — Yet Another Agentic Harness — Design Notes

**Status:** implemented and proven, pre-1.0, on branch `harness-redesign`. Living document — updated as decisions firm up. The Python implementation lives under `src/yaah/`; this document is the architecture it realizes. Error handling is not yet hardened (see `early_review.md`). Current build state and roadmap: `TODO.md`.

YAAH is a generic, distributed runtime for orchestrating agentic workers. It is small and knows nothing about any particular application. Its job is to move work between interchangeable workers, reliably, whether they run on a laptop or in the cloud.

`the example app` (a code-change pipeline) is the first application built on YAAH. It is a production-proven workload (~250 shipped tasks) and a useful test of the design. It is a consumer of YAAH, not part of it. See §10.

---

## 1. The stance: workers, not citizens

Most agent frameworks treat agents as first-class citizens: autonomous, primary, deciding whom to call and routing themselves. YAAH treats agents as workers. The harness is primary. A worker does one job and knows nothing about the rest of the system. The harness routes; the worker works.

This constrains agents rather than empowering them, which is the intent: constrained workers produce output that is easier to validate and review. (In our harness, agents are first-class citizens only in their dreams, after they clock off.)

The rest of this document follows from this choice: workers are opaque `Node`s, they do not address each other, routing lives in the harness and its config, and any worker is replaceable.

---

## 2. Principles

In priority order:

1. **Simplicity.** Few concepts. The whole system fits in your head.
2. **Similar building blocks.** Everything is the same shape — one kind of node, one kind of message.
3. **Easy to understand.** This document plus the interfaces should be enough.
4. **Replaceable parts.** Depend on interfaces, not implementations. Any part swaps out without touching the others.
5. **Separation of concerns** — in particular comms ↔ harness, and harness ↔ application (§10).

Litmus test: if a feature is not a `Node`, an `Envelope`, or a `Comms` backend, question whether it belongs.

Corollary: add a gate or feature only after seeing the failure it prevents. Default to leaving things out.

### Package layout: engine vs adapters (realizes principle #4)

The folder structure makes the swap layer visible at a glance. Two tiers:

- **Engine** (`core/`, `comms/`, `harness/`, `agents/`, `nodes/`, `build/`, `runtime.py`, plus the port-home dirs `prompts/`, `data/`, `mcp/`, `store/`) — the invariant machinery: the contract (`Envelope`/`Node`/`NodeConfig`/`Verdict`), the line (`Harness` + graph/baton/baton_store/gate_driver), the generic worker library (`nodes/` — agent, get, post, transform, shell, shell_check, render, worktree, once, validators; agent backends + tool-loop under `agents/`), and the assembly layer (`build/` + `runtime.py`). Each port (a `Protocol`: `Comms`, `ApiProvider`, `PromptSource`, `DataSource`/`Sink`, `McpSource`, `StoreBackend`) lives in its dir next to its **zero-config default** — the in-memory / static reference that lets the harness run with no external systems (`InProcessComms`, `MemoryBackend`, `FakeProvider`/`ScriptedProvider`, `StaticPromptSource`, the `Routing*` composers).

- **Adapters** (`adapters/{transports,backends,prompts,data,mcp,stores}/`) — **the specialization layer**: every implementation that binds yaah to an outside system (filesystem, network, a SaaS, a subprocess, a third-party lib) — `LocalBus`/`NatsComms`, `ClaudeCliProvider`/`LiteLLMProvider`, `File`/`Http`/`Langfuse` prompt sources, `File`/`GitDiff` data, `FileMcpSource`, `FileBackend` (+ deferred `nats_kv`/blob/sqlite). To swap or add a provider, you touch only `adapters/` + one runtime factory map; the engine and the pipeline graphs don't move.

The boundary is enforced by **dependency direction**: adapters import the engine's ports + `core`, *never the reverse*. Nothing in `core/comms/harness/agents/nodes` imports from `adapters/`; only `build/` and `runtime.py` (assembly) and the public `yaah/__init__.py` (facade) wire adapters in, selected by config. So "if it's under `adapters/` it's swappable; if it isn't, it's the engine and you shouldn't be forking it." This is the same `app → yaah` one-way dependency (§10), applied one level down inside yaah.

---

## 3. The core: three concepts, two interfaces

The system is three nouns:

```
Envelope   — one message shape, used everywhere
Node       — invoke(input: Envelope, config): Envelope     ← every node, agents included
Comms      — send(target, Envelope) / subscribe(topic, handler)
```

- An agent is a Node whose body calls a model. A validator, a renderer, a human-gate are also Nodes.
- All messages are Envelopes. One format.
- The harness depends only on `Comms` and `Node`. It does not import a transport or a concrete node. That is the comms↔harness separation, and it is what makes both sides replaceable.

Node config carries the per-node settings:

```
NodeConfig = {
  model?: string                       // which model (provider-agnostic — see §7)
  effort?: 'low' | 'med' | 'high' | 'max'
  temperature?: number
  timeout, retries, idempotencyKey     // remote semantics — always present (see §5)
  [k: string]: unknown                 // node-specific settings
}
```

Extensibility is subclassing. Several node classes share one API:

```
abstract class Agent implements Node { /* shared: build prompt, call model, trace */ }
class WorkerA extends Agent { ... }
class WorkerB extends Agent { ... }
```

The orchestrator and the comms layer depend only on `Node`. Adding a node type means adding a subclass and registering it; the orchestrator does not change.

---

## 4. Three comms modes

Three uses of the same `Comms`, not three transports.

| Mode | Ownership | Acknowledged? | Use |
|---|---|---|---|
| **Event** (`publish`) | none — fire-and-forget | no | metrics, logs, broadcast (fan-out) |
| **Call** (`request`) | caller keeps it, gets a result | the reply is the ack | validator verdict, a tool |
| **Handover** (`handoff`) | transfers A→B, A then exits | yes, explicit | stage→stage baton, triage→specialist |

Event / Call / Handover is the complete set.

### Handover details

A handover differs from a plain message: (1) ownership moves; (2) it is acknowledged, so the baton is not dropped (B accepts, or A keeps it / escalates); (3) context travels with it (a handover report of artifacts, not the predecessor's reasoning); (4) exactly one owner at a time.

Nodes do not command each other. A node emits a handoff intent naming a role/capability; the harness resolves it to a concrete node, delivers, and confirms acceptance:

```
Envelope (handoff) = {
  intent: 'handoff',
  to:     'role:<capability>',   // a role, not a concrete node
  baton:  '<task-id>',           // ownership token; harness tracks the holder
  context: { ...artifacts },     // artifacts, not the predecessor's reasoning
  from:   'role:<capability>'
}
```

Protocol, implemented once in the harness:

```
A emits handoff intent → harness resolves role → offers baton to B
  → B accepts (ack)        → harness records B as owner, releases A
  → B unavailable/rejects  → A keeps baton, or harness escalates
```

Composition: mediated calls and loops within an ownership scope (§5); handover between scopes. Default to mediated; use handover only when a node owns the routing decision. Bound the hops; keep a baton audit trail.

---

## 5. Validators and the retry loop

Rule: nodes do not command each other; the orchestrator mediates the loop.

A validator is a Node whose output is a `Verdict`:

```
Verdict = {
  status: 'pass' | 'fail',
  failures: [{ code: 'not_json', message: '...', fixHint: '...' }],
  severity: 'hard' | 'soft'
}
```

A worker is not told "you may not finish." The orchestrator declines to accept the output and re-invokes the worker with the verdict as input. A Node returning from `invoke` is not the same as the work being done; that judgment belongs to the graph.

```
let attempt = 0, input = task
while (true) {
  const out     = await comms.invoke(workerNode, input)
  const verdict = await comms.invoke(validatorNode, out)        // a separate node
  if (verdict.status === 'pass') return out
  if (++attempt >= maxAttempts) return escalate(out, verdict)   // bounded → human gate
  input = { ...task, priorAttempt: out, feedback: verdict.failures }
}
```

- The worker does not know about the validator. Its only contract change is an optional input field (`priorAttempt` + `feedback`).
- The loop is bounded and escalates to a human gate. No infinite loops.
- Two tiers, cheap first: deterministic validators (valid JSON? matches schema?) run before model-based judges. Both return the same `Verdict` shape.
- The loop lives in config, not in any node:

  ```yaml
  <stage>:
    validators: [json-schema, ...]
    onFail: { retry: 3, feedback: true }
    thenEscalate: human
  ```

- A worker may self-check cheaply (a library call, not a node) as an optimization, but the binding gate is the independent validator node.

YAAH may ship a small standard library of generic validators (e.g. `is-valid-json`). Domain validators live in the application.

### Human gates: suspend / await-external / resume

When the loop escalates to a human — or to any external decision — the harness does not block a worker or hold a process open. It suspends:

- A gate is a Node that returns an Envelope of kind `await`, naming what it waits for (e.g. `await: human:spec_review`) and where to resume.
- The harness records the await against the baton, persists state (the baton is the resume cursor, §6), and stops. Nothing polls.
- An external event — `resume(baton, payload)` — delivers the decision; the harness re-enters the graph at the resume target with the payload as input.
- Suspends are durable: a run can be parked for minutes or days and survive a restart, because the baton and state are persisted. This is the same mechanism as resume-after-crash.

This is the one primitive the example app conformance pass found missing (the app's docs). All human gates (spec_review, options, cucumber_required, refix, findings) and the interactive discussion/grill node build on it: the interactive node is a worker that suspends after each question and resumes on the user's answer.

### The UI as a node

The UI is an ordinary Node, reached through all three comms modes:

- **push** (event) — progress, logs, notifications to display; no reply expected.
- **call** (request) — ask the human something and get an answer.
- **handover** — hand the task to the human at a gate; the UI holds the baton until the human acts, then hands it back (resume).

The UI is long-lived and stateful: it keeps a mailbox of messages and forwards them later (buffered pushes for display, pending questions, collected responses). To stay within §6, the mailbox is externalized state addressed by a handle (a session/conversation key), not hidden in one process — any UI instance reattaches to the same mailbox. The UI therefore needs no new kernel primitive: it is Node + Comms (three modes) + a state handle (§6) + suspend/resume (above).

A durable inbox (messages retained until consumed, even if the UI is briefly down) is a substrate feature, not a kernel guarantee. The in-process backend delivers pushes to current subscribers only; a NATS/Dapr backend can provide a durable queue. Treat durable delivery as a substrate upgrade, deferred until needed.

---

## 6. Worker memory under statelessness

Problem: when the orchestrator re-invokes a worker (a retry, §5), a stateless worker has lost its working memory — what it read, its discovery, its reasoning. It recovers its outputs from artifacts but redoes the rest, which costs time and money and may diverge.

Principle: state lives in the substrate, addressed by a handle, not inside a node. A node stays stateless in identity (any instance can serve the next call) but may reattach to externalized, keyed state. An in-node cache that requires the next call to hit the same instance is not allowed, because it breaks replaceability and distribution.

Three mechanisms, cheapest first:

1. **Scratchpad artifact** (near free). The worker persists its working notes, not just final output; the retry re-reads them.
2. **Prompt caching.** The orchestrator re-sends full context; the provider caches the prefix (~5-min TTL, extendable). The retry is cheap and the node stays stateless.
3. **State handle / session** (only on measured need). The harness gives the node a `stateRef` to reattach to. Cost: session affinity and a state-store dependency.

The baton carries the state handle: `baton = ownership token + state pointer`. Whoever picks up the task (a retry, a node after handover, a worker after a crash) reattaches via the handle. The durability cursor and the memory cursor are the same thing.

---

## 7. Provider-agnostic workers

"A worker with swappable implementations behind one API" is two separate axes:

| Axis | What swaps | Approach | Where it lives |
|---|---|---|---|
| **Model provider** | OpenAI ↔ Claude ↔ Gemini ↔ … | a gateway (LiteLLM or similar): one API to many providers, with cost/latency telemetry | inside the `Agent` base class; chosen by `NodeConfig.model` |
| **Worker implementation** | a subprocess agent ↔ an SDK agent ↔ a remote agent | the `Node` interface | the contract |

Do not hand-roll provider adapters. The `Agent` base calls a gateway, so `model: 'claude' | 'gpt' | 'gemini'` is configuration. This also keeps cost/latency telemetry in one place. The model boundary is the part that changes most often, so it is kept the softest.

---

## 8. What runs where (distribution)

Parts of YAAH may run on a developer's machine, parts in the cloud, and the split may vary. Because the harness depends only on `Comms`, this is a deployment choice, not a code change.

Placement follows resource binding, not identity:

- **Resource-bound** workers (need a working tree or local resource) are pinned local (or to a cloud worktree).
- **Artifact-bound** workers (need only the Envelope payload) are placement-free and are the cloud-burst candidates, especially parallel fan-outs.
- **Human-facing** steps are local.

Two loops, two policies:

| | Inner loop (dev / local) | Outer loop (shared / cloud) |
|---|---|---|
| Change a route/prompt | edit file → hot reload → instant | PR → checks → rollout |
| Ceremony | none | versioning + review |
| Optimize for | speed | safety |

Ceremony applies to crossings (promoting an artifact from local to shared), not to local edits.

Two change-rates are kept apart: the liquid edge (prompts, model choice, graph, wiring — daily, hot-swappable data) and the stable core (transport, durability, envelope format — the adopted framework).

the example app example: its parallel review/audit/sceptic stages are artifact-bound and parallel, so they are the natural cloud-burst tier; code/test/merge stay local. Expect mostly local early, with cloud-burst available by changing placement config.

---

## 9. Technology choice

Own the two interfaces (`Node`, `Comms`); they are small and kept stable. The comms backend is a swappable adapter:

- **in-process** — local dev, zero infrastructure.
- **NATS** — first distributed backend; a simple broker (subjects, pub/sub, request-reply, one binary; leaf nodes connect local to cloud).
- **Dapr** — a heavier adapter for later, if its building blocks (durable workflow, scoping) are needed. Adopting it later is not a one-way door, because `Comms` is an interface.

Not in the core: Temporal and Apache Camel make a node compile against the framework, which conflicts with the comms↔harness separation. Temporal is a possible later option for the stable core only. Camel is the right mental model (its endpoint/route DSL) but heavy for daily AI work.

Bus, not point-to-point. A broker gives O(n) coupling, config-driven rerouting, and location transparency. Point-to-point agent protocols give the opposite (O(n²) coupling, routing in the agent). So:

- Internal comms use the minimal Envelope over a bus (the harness routes; config reroutes).
- A2A and MCP are edge adapters only — for external agents (A2A) and external tools (MCP). Wrap an external agent as a `Node` whose body speaks A2A. They are not the internal envelope. (An earlier note suggested aligning the Envelope with A2A; that is withdrawn — A2A is point-to-point and not config-friendly.)

Reference pages: Dapr https://dapr.io/ · NATS https://nats.io/ · Temporal https://temporal.io/ · Camel https://camel.apache.org/ · LiteLLM https://github.com/BerriAI/litellm/ · A2A https://a2a-protocol.org/

---

## 10. Relationship to the example app

YAAH is generic; the example app is the first application on it. Dependency direction: `app → yaah`, never the reverse. YAAH contains no application-specific concepts.

Boundary test: vocabulary. Harness words (Node, Envelope, Comms, route, verdict, baton, placement) are YAAH. Application words (spec, RED/GREEN, code, review-lens, data-audit, findings) are the example app. A domain word appearing in YAAH means an abstraction is not yet generic.

| YAAH (generic) | the example app (application) |
|---|---|
| Node / Envelope / Comms, wiring runner, validator loop, baton, placement, model gateway, substrate adapters | the agents, the specific graph, domain validators, RED/GREEN, the data-audit gate, prompts, report templates, bug catalog, placement config |

Conformance: the app's stage→node mapping is also YAAH's acceptance test. Every stage must map onto generic primitives with nothing left over. Anything left over is a missing YAAH primitive. The mapping lives in the app's docs and points at this document.

---

## 11. Deferred (not in v1)

Add each only when a concrete failure requires it:

- **Schema registry** — until a real version mismatch occurs, a version field on the Envelope is enough.
- **GitOps control plane, topic scoping, tiered routing authority** — shared-cloud governance; add when shared cloud nodes need it.
- **Cost/speed optimization** beyond prompt caching and gateway telemetry — substrate overhead is small next to model cost.

---

## 12. Open questions / TODO

- ~~Package/repo layout for the harness.~~ DONE — engine vs `adapters/` swap layer, see §2 "Package layout".
- ~~Exact `Envelope` schema (fields, version field, correlation id).~~ DONE — `core/envelope.py`: `id` / `kind` / `payload` / `headers`, with `correlation_id` / `causation_id` / `baton` / `schema` (the version stand-in, §11) as standard headers and `reply()` chaining.
- ~~Where baton/ownership state persists in the in-proc and NATS backends.~~ DONE — the `StoreBackend` port (`store/`) with `MemoryBackend` (default) and `FileBackend` (`adapters/stores/`); `harness/baton_store.py` persists the baton as the resume cursor, proven durable cross-process (`test_gates_cross_process.py`). Idempotency (§5/§11) rides the same store (`store/idempotency.py` + the `once` node).
- ~~PoC scope: one the example app stage end-to-end with one validator loop, in-proc + NATS.~~ DONE and exceeded — eval stage over NATS, plus a repo-bound worktree→RED→code→GREEN→diff→review run with real `claude -p`.
- Wiring/config file format: DONE (JSON pipeline config + a root/deployment config; see `runtime.py`). **Hot-reload still open.**
- the example app conformance: the stage→node mapping holds — the app's pipeline config is the runnable full vertical (offline fake twin verified end-to-end; a real-model run on a real task is the remaining acceptance gap). Mapping lives in the app's docs.

---

## Changelog

- 2026-06-06 — initial draft as the factory's substrate replacement.
- 2026-06-06 — reframed as YAAH, a generic harness; the example app recast as the first application. Added §1 (workers, not citizens), §7 (provider-agnostic workers), §10 (relationship to the example app). Corrected §9: A2A/MCP are edge adapters only; bus over point-to-point.
- 2026-06-06 — added the human-gate suspend/await/resume primitive to §5 (the one real gap from the example app conformance pass; the mid-flight-help and resource-lease gaps were dropped — never used / per-worker config). Started the Python kernel under `yaah/src/yaah/`.
- 2026-06-08 — status flipped from "nothing built" to **implemented & proven**. The design below is now realized in `src/yaah/`: kernel + harness (retry/fan-out/branch/suspend-resume, bounded baton lifecycle), pluggable layers on one `source:key` pattern (prompts / data get+post / mcp / model backends), the full node library (incl. transform fn/node/http, worktree, once), agent capability triad (prompt + tools + mcp), and secured multi-process distribution over NATS (auth + TLS + subject scoping + queue-group pools). §2 package-layout list expanded to name the port-home dirs; §12 open questions reconciled (Envelope schema, baton persistence, PoC → DONE; hot-reload and real-prompt e2e still open). Recent capability detail: `agent-tools.md`, `durable-state.md`; pre-readiness review: `early_review.md`; build state/roadmap: `TODO.md`.
