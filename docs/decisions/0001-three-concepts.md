# 0001 — Three concepts, and what protects them

**Status:** Accepted
**Date:** 2026-06-17

## Context

YAAH's value proposition is not features. Many orchestration libraries have
features. YAAH's value proposition is that the **whole system is three
concepts**:

- **Envelope** — one message shape, used everywhere.
- **Node** — `invoke(input, config) → output`. Every worker, including agents.
- **Comms** — `request` / `publish` / `subscribe`. The only thing the harness
  calls.

A reader who internalizes those three nouns can predict where any piece of
behavior lives. A contributor who respects those three nouns can extend YAAH
without breaking anyone else's mental model.

The risk to a project of this shape is well-known: contributions individually
seem small, but cumulatively erode the conceptual minimalism that made the
project worth using. Every "small new thing" — a fourth top-level concept, a
new built-in node where composition would have done, a runtime dependency in
the core — taxes every future reader.

This ADR exists so that future contention has something to point at. When
someone proposes a change that crosses one of the lines below, the answer is
not the maintainer's taste; it is this document.

## Decision

YAAH commits to five architectural invariants. Changing any of them requires
its own ADR.

### 1. Three concepts only

Envelope, Node, Comms. No fourth top-level abstraction is added without
unanimous maintainer agreement and an ADR explaining what was tried and why
composition of the existing three was not enough.

### 2. Zero-dependency core

`src/yaah/core/`, `src/yaah/harness/`, and `src/yaah/comms/` may not import
anything outside the Python standard library and YAAH's own modules.

Every third-party library — `nats`, `litellm`, `langfuse`, `httpx`, `git` — is
an opt-in adapter under `src/yaah/adapters/`. Adapters import the engine;
the engine never imports adapters.

Enforced by CI (`check_core_zero_dep.py`).

### 3. Domain-free core

Nothing in `src/yaah/` may name a stage, tenant field, test runner, or
anything app-specific. If a domain term such as "spec" or "RED" appears in
the engine, an abstraction is not yet generic.

Domain knowledge lives in the consuming app's pipeline configuration.

Enforced by a banlist + CI (`check_domain_banlist.py`).

### 4. Compose, don't invent

A new built-in node type must justify, in writing, why `fork` + `fanin` +
`transform` cannot express the need. The default answer to a new node type
proposal is rejection.

Historical evidence: a `subpipeline` node was added and retired within 24
hours when the maintainer realized the same expressiveness was already
available through composition. The reflex this captures is the one to keep.

### 5. Budgets, not infinity

The following budgets are tracked. Adding to a budget requires an ADR
explaining what the project gains in return; removing from a budget is
celebrated and credited.

| Budget | Rule |
|---|---|
| Top-level concepts | No growth without unanimous maintainer agreement. |
| Built-in node types | Adding one requires removing or merging one, or an ADR. |
| Top-level root-config keys | Same rule. |
| Public modules in `core/` | Same rule. |

Reviewers watch these budgets on every PR; crossing one without an accompanying
ADR is grounds for rejection. (There is no automated cap check yet — when CI
grows one, the numeric caps and the check land together.)

## Consequences

### What this enables

- A new reader can be onboarded with three sentences and `examples/hello-yaah/`.
- Adapter contributors can ship without negotiating with the engine.
- The engine stays small enough that one person can hold it in their head.
- The "no" conversations stop being about the maintainer's taste and start
  being about a document the contributor has already read.

### What this forbids

- Convenience features that would add a fourth concept, even helpful ones.
- "Configurable" as a way to resolve a design debate inside the core.
- Drive-by additions to `core/`/`harness/`/`comms/` that pull in a new library
  because "it's just one import."
- Vendor-shaped features in the engine that exist to serve one app.

### What we expect to regret

- Some legitimately useful node types will be rejected and end up in
  community packages instead of the core. That's the cost of the bargain;
  the ecosystem path ([`ECOSYSTEM.md`](../../ECOSYSTEM.md)) exists to absorb
  those without the engine paying for them.
- Budgets are easy to game by splitting one change into several. Reviewers
  watch for this. The values rubric in [`CONTRIBUTING.md`](../../CONTRIBUTING.md)
  is the backstop.
- This ADR will, eventually, be wrong about something. When that happens, we
  write the next ADR; we don't quietly ignore this one.

## Related

- [`CONTRIBUTING.md`](../../CONTRIBUTING.md) — the values rubric and the
  contributor workflow.
- [`GOVERNANCE.md`](../../GOVERNANCE.md) — roles, ADR process, and how this
  document gets revised.
- [`AGENTS.md`](../../AGENTS.md) — the cross-tool brief for AI assistants
  drafting pipelines and PRs.
