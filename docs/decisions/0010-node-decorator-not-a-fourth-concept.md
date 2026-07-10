# 0010 — The node decorator: name the wrapper pattern, don't grow a `wrap:` key

**Status:** Proposed — design-only. The maintainer decides; do NOT read this as Accepted.
**Date:** 2026-07-07
**Answers:** the parked `.notes/todos.md` item "Wrapper-as-fourth-concept ADR — form-plan
eval finding. `OnceNode + CarriageBoundaryNode + AttachingAgent` is an unnamed pattern.
Either name it (ADR: 'Decorator on Node') or fold `attach:` into a generic `wrap:` config
key with one registry."

## Context

Three engine classes have the same shape: each holds an inner `Node`, delegates
`invoke()`, and adjusts the result. They are the harness's "capability slots" — a way to
add a cross-cutting concern to a node without teaching the base node about it.

```python
# all three: same shape, different concern
class OnceNode(Node):            # src/yaah/nodes/once_node.py
    def __init__(self, inner, store): ...
class CarriageBoundaryNode(Node): # src/yaah/trace/carriage_boundary.py
    def __init__(self, inner, tracer): ...
class AttachingAgent(Node):       # src/yaah/agents/attaching_agent.py
    def __init__(self, inner, attachers, tracer): ...
```

The pattern is unnamed, so a fourth one has no home and no rules. The queued decision is a
fork: **(A)** name the pattern and give it an invariant, changing no config; or **(B)** fold
the author-facing wrappers into one generic `wrap:` config key with one registry.

The sharp facts that decide it — all verified against the source, not assumed:

### The three wrappers are NOT the same kind of thing

| Wrapper | What triggers it | Scope | Wired at | Changes the **payload** contract? |
|---|---|---|---|---|
| `AttachingAgent` | config key **`attach: [fn:…]`** | `agent` nodes only | inside `_build_agent` (`builders.py:98–142`) | **YES** — merges attacher keys onto the payload: `{**result.payload, **attached}` |
| `OnceNode` | config key **`idempotent: true`** | any side-effecting node | `_wrap_node` (`builders.py:327–332`) | **NO** — returns the inner envelope (or a cached copy) verbatim |
| `CarriageBoundaryNode` | **implicit** — tracer `is_carriage` is True | **every** node | `_wrap_node` (`builders.py:334–337`) | **NO** — adds `trace` to **headers**, never the payload |

Build order, innermost → outermost (from `build.py:127` → `_wrap_node`):

```mermaid
graph LR
    RAW["raw node<br/>(agent/shell/…)"] -->|"attach: present<br/>(agent only)"| ATT["AttachingAgent"]
    ATT -->|"idempotent: true"| ONCE["OnceNode"]
    ONCE -->|"tracer is_carriage"| CARR["CarriageBoundaryNode<br/>(outermost, implicit)"]
    CARR --> OUT["served reply"]
```

The load-bearing observation: **only one of the three trigger mechanisms is an author config
key that a `wrap:` list could subsume.** `CarriageBoundaryNode` is engine-injected off a
runtime tracer capability — the author never opts in, and it wraps *every* node. It
**cannot** become a `wrap:` list item. So a `wrap:` key unifies two of three and leaves the
third outside — "one registry" would be a half-truth.

### The one wrapper that mutates the payload is invisible to the contract lint (ADR-0006)

ADR-0006 made the data-flow lint read each node's contract from a pure `contract(cfg)`
function (`node_contract.py`). Those functions read the **raw node type + cfg only** —
`agent_contract` reads `parse`, `output_schema`, `provides`, `carry`, `cwd`. **None reads
`attach` / `idempotent` / `carriage`.** For two wrappers that is correct (they don't touch
payload keys). For `AttachingAgent` it is a **latent soundness gap**:

- **parse=true + attach** (every present example — `config-flow-ab`, `arch-drift-ab`): the
  agent contract is `closed=False`, so a downstream miss is only a WARNING, and those
  pipelines route `usage` into `transform` nodes (opaque), which the lint doesn't check.
  **Nothing is flagged today** — verified.
- **parse=false + attach + a `render`/`branch` reading an attached key**: the agent contract
  is `closed=True, provides={raw}`. A `render "{{usage}}"` would be a **hard false-positive
  ERROR** (`validate` fail-loud) even though the attacher supplies `usage` at runtime. **No
  such config exists in the repo** — so this is latent, not a live bug.

This is exactly the false-positive class ADR-0006 worked to design out for custom nodes,
re-opened by a wrapper that changes the payload behind `describe()`'s back. It is the single
strongest technical argument in this whole decision — and, verified, it has **no present
stakeholder** and a **cheaper fix than `wrap:`** (below).

### Present stakeholders (evidence, not speculation)

| Thing | Present stakeholder? | Evidence |
|---|---|---|
| `attach:` | **yes** | `examples/config-flow/config-flow-pipeline-ab.json`, `examples/arch-drift/arch-drift-pipeline-ab.json`; ADR-0003; A/B model comparison is a committed goal |
| `idempotent:` | **no** (config) | appears only in `docs/durable-state.md`; **no example pipeline sets it** — built, wired, tested, documented, unused by any config |
| `CarriageBoundaryNode` | n/a | engine-implicit; never author-facing |
| a unified **`wrap:`** key | **none** | no example, no mailbox suggestion, no other ADR references it. The only driver is "name the pattern." |

## Decision

**Option A — name the pattern as a `Node decorator`, give it one invariant, and do NOT add a
`wrap:` key.** A decorator is a `Node` wrapping a `Node` — pure composition, the thing
ADR-0001 §4 ("compose, don't invent") celebrates. **It is not a fourth concept** (ADR-0001
§1): it *is* a Node; it needs no new noun. Naming it is a documentation + invariant move, not
a config-grammar move.

**The invariant (new, load-bearing):**

> A node decorator MUST be **payload-contract-transparent** — it may change headers,
> timing, idempotency, or observability, but the set of payload keys it emits MUST equal
> the inner node's. **If a decorator mutates the payload** (adds/removes keys), its keys MUST
> be reflected into the node's `Contract` (ADR-0006), so the data-flow lint stays sound.

Under this invariant, `OnceNode` and `CarriageBoundaryNode` are conformant; **`AttachingAgent`
violates it latently** and is the one recorded exception, with its fix named (Consequences).

**Reject `wrap:` for v1**, and park it in the deferred ledger with a named trigger, because:
no present stakeholder; the unification is *incomplete* (the implicit carriage wrapper can't
join, so "one registry" oversells); and it adds config grammar with no demand — the reflex
ADR-0001 warns against ("*'Configurable' as a way to resolve a design debate*"; the
subpipeline node, added and retired in 24h, is the standing precedent).

### The two options, pinned concretely

**Option A (recommended) — today's shape, named and governed.** No config change.

```json
"role:commit": {"type": "shell", "command": "git commit …", "idempotent": true},
"role:extract": {"type": "agent", "prompt": "file:extract",
                 "attach": ["fn:transforms:UsageAttacher"]}
```

**Option B (rejected for v1) — one `wrap:` list + a wrapper registry.**

```json
"role:commit": {
  "type": "shell", "command": "git commit …",
  "wrap": [
    {"kind": "once"},
    {"kind": "attach", "attachers": ["fn:transforms:UsageAttacher"]}
  ]
}
```

with `register_wrapper(kind, factory, contract_compose)`; list order = build order;
`attach:`/`idempotent:` become sugar or migrate (aliased for back-compat).

| | **A · name + invariant** | **B · generic `wrap:` + registry** |
|---|---|---|
| **Costs** | ~one ADR. Three trigger idioms persist. The name lives in docs, not grammar. | New node-level config grammar (a reader tax, ADR-0001 §1). A wrapper registry (new extension surface). Migration of two keys + back-compat aliases = **two ways to spell one thing** (its own slop). Carriage can't join → unification incomplete. |
| **Buys** | Names the slot; a 4th wrapper has a home and a rule. Preserves the three-concept budget. Zero migration, zero new surface. | One idiom for author-facing wrappers. Explicit build-order control. A natural seam for **contract-compose** (each `wrap:` item declares how it changes `describe()`). |
| **Contract (ADR-0006)** | Invariant makes transparency a *rule*; the one violator is fixed cheaply and directly (read `attach` in `agent_contract`). | The registry's `contract_compose` *forces* every wrapper to declare its key delta — structurally sound, but solves a gap no present config hits. |
| **AI-authoring / lint** | Unchanged. Nothing new to lint (each key already validates at build). Less grammar to learn. | `validate` *could* lint a wrap chain (kinds + contract-compose known). But more grammar = more to learn; the payoff needs a stakeholder that doesn't exist. |

## Considered alternatives / objections

Each objection was run as a hard contrarian and **verified against the source before being
accepted or rejected** — advice, not truth.

**1. "Three ad-hoc triggers is slop; `wrap:` is the structural fix (memory: reach for the
structural fix)."** — *Survives partially; does not justify B.* Verified: the three triggers
are essentially different, and `CarriageBoundaryNode` is engine-implicit — it **structurally
cannot** be a `wrap:` list item (`builders.py:334`, keyed off `tracer.is_carriage`, wraps
every node, no author opt-in). So `wrap:` tidies the *two* config-driven wrappers but leaves
the third outside; the "one registry" framing is false. A structural fix that doesn't
actually structure the whole set is not the clean win the objection assumes. Net: mildly
pro-B on ergonomics, not on architecture.

**2. "The `AttachingAgent` contract gap is a REAL soundness hole in a SHIPPED, committed
feature — `wrap:` with contract-compose is the fix."** — *Survives as a genuine note; does
NOT require B.* Verified reachability: every present `attach:` agent is `parse=true`
(`closed=False` → miss is a WARNING) and routes `usage` into opaque `transform` nodes, so
**nothing is flagged today**; the hard false-positive needs `parse=false + attach +
render/branch reading an attached key`, a config that **does not exist** in the repo. The gap
is latent. And it closes **without** `wrap:`: teach `agent_contract(cfg)` to read `attach`
and widen `provides` / drop `closed` when attachers are present (a targeted ~3-line fix in
`node_contract.py`) — or, minimally, document it. `wrap:`'s `contract_compose` would solve it
generically, but you don't add a config grammar to fix a latent gap in one wrapper. **Recorded
as the invariant's one known violation + its cheap fix.**

**3. "A wrapper isn't a fourth concept — it's composition; naming it is enough, and `wrap:`
is the 'configurable as a way to resolve a debate' anti-pattern ADR-0001 forbids."** —
*Survives; strong for A.* Verified against ADR-0001: §5's budget lines are *root*-config keys
and *node types* — `wrap:` is a node-level key, so it isn't strictly a budgeted line. But §1
("every small new thing taxes every reader") and the retired-subpipeline precedent apply
squarely: adding grammar with no stakeholder is the exact reflex the ADR exists to check.

**4. (Against my own recommendation) "Name-only is a doc that rots — it defers the real
question with extra words. If the answer is do-nothing, close the todo."** — *Partially
valid; reshaped the decision.* A bare "call it Decorator" **would** be thin. The fix: name-only
is only load-bearing if it (a) states the **transparency invariant**, (b) records the
`AttachingAgent` violation + its concrete fix, and (c) pins the `wrap:` **trigger** in the
deferred ledger so it isn't re-litigated from scratch — which is ADR-0001's own stated purpose
("future contention has something to point at"). With those teeth, A discharges the question;
without them the objection lands. This ADR carries all three.

**Rejected outright:** a *third* option — make the pattern a real `Decorator` base class in
`core/` — was considered and dropped. It would formalize the shape but pull a shared base into
the zero-dependency core (ADR-0001 §2) for three classes that already share nothing but a
constructor signature; the Python duck-typed `Node` is sufficient. No base class.

## Consequences

**What this enables**
- A fourth wrapper has a name, a home (a decorator = a `Node` wrapping a `Node`), and a rule
  (the transparency invariant) — without a grammar change or a budget crossing.
- The `AttachingAgent` payload-contract gap is on the record with a bounded, direct fix,
  instead of lurking as an undocumented latent false-positive.

**What this commits us to (if A is accepted)**
- A one-paragraph "Node decorator" section in `AGENTS.md` / the engine map, stating the
  transparency invariant and listing the three decorators + their triggers.
- Either fix `agent_contract` to read `attach` (preferred — makes the invariant true), or
  document the parse=false+attach+render caveat where `attach:` is described. **This ADR
  recommends the fix**; it is small and removes a real (if latent) unsoundness.
- A deferred-ledger row (`.notes/deferred-ledger-2026-07-07.md`, section A):

  | Deferred | Use case it would serve | Why deferred / trigger |
  |---|---|---|
  | **Generic `wrap:` key + wrapper registry** | one author-facing idiom + a contract-compose seam for wrappers that change the payload | zero stakeholders; unifies only 2 of 3 wrappers (carriage is engine-implicit). **TRIGGER:** a *fourth author-facing* wrapper is proposed, **or** a real pipeline needs to read an attach-added key in a `render`/`branch` (the contract-compose seam becomes load-bearing) |

**What we expect to regret**
- If author-facing wrappers keep accreting one config key at a time (`idempotent:`,
  `attach:`, then a third), the day the `wrap:` trigger fires we migrate two-or-three keys at
  once with back-compat aliases — more churn than if we'd unified at two. Accepted: paid only
  if the fourth actually arrives, which no evidence today predicts.
- "Name-only" ADRs are the easiest to ignore. Mitigated by the three teeth above and the
  ledger trigger; if the invariant isn't wired into `AGENTS.md`, this ADR failed.

## Related
- [`0001-three-concepts.md`](0001-three-concepts.md) §1 (no fourth concept), §4 (compose,
  don't invent), §5 (budgets) — the frame that makes a decorator *not* a new concept and
  makes `wrap:` a grammar addition to justify.
- [`0003-attacher-port.md`](0003-attacher-port.md) — `AttachingAgent`, the one payload-mutating
  decorator and the `attach:` key `wrap:` would absorb.
- [`0006-node-contract-capability.md`](0006-node-contract-capability.md) — the `describe()` /
  `contract(cfg)` mechanism the transparency invariant protects; the false-positive class the
  `AttachingAgent` gap re-opens.
- `src/yaah/build/builders.py` (`_build_agent`, `_wrap_node`) — where all three decorators are
  wired; the build-order and trigger facts above come from here.
- `.notes/deferred-ledger-2026-07-07.md` — where the `wrap:` trigger row lands if A is accepted.
