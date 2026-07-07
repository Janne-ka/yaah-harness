# 0007 — `foreach`: dynamic per-item fan-out (bounded map over a runtime list)

**Status:** Accepted — SHIPPED 2026-07-06/07
**Date:** 2026-07-06

## Context

yaah's parallel shapes are STATIC — names listed at author time:

- `fanout: [role…]` — the same input envelope to N different ROLES, gathered,
  merged as `{results, roles, failed_roles}`, `min_success` k-of-n.
- `fork: [stage…]` + `fanin` — the envelope spread to named successor STAGE
  CHAINS, durably joined via the clear-bus (timeout / reduce).

The gap (hit by a real design — the EHS refute swarm, `.notes/ehs-predesign-v2.md` §2):
**run one stage's node once per element of a RUNTIME-SIZED list, bounded to K
concurrent** — one skeptic agent per extracted requirement, N unknown until an
earlier stage runs, max 3 in flight. LangGraph's `Send` equivalent. Today the
only expression is a two-phase author loop (run extract → generate a config with
N baked branches → run it), which forfeits single-run integrity: one baton, one
lint pass, one trace chain, gates that span the whole flow.

## Decision

**A third parallel shape, `foreach` — a bounded map of ONE node over a payload
list, with per-item inputs built from the item plus NAMED CARRIES ONLY.**

```json
"swarm": {
  "node": "skeptic",
  "foreach": {
    "items": "requirements",        // payload key holding the list (from an upstream stage)
    "into": "item",                 // per-item input key for the element (default "item")
    "carry": ["doc", "task"],       // payload keys COPIED into each per-item input (default [])
    "max_concurrent": 3             // in-flight bound (default 3 — a safe LLM-rate default)
  },
  "min_success": 5,                 // reused k-of-n (optional; default = all must succeed)
  "then": "consolidate"
}
```

### D1. Per-item input is REPLACE + named carries, not a payload copy

Element *i*'s request envelope payload is exactly:
`{ <into>: items[i], "item_index": i, <carry keys copied from the inbound payload>,
<graph.sticky keys present on the inbound payload> }`.

Sticky keys are auto-included (design eval #7): sticky exists to thread run-wide
invariants (`workdir`, repo roots) through every node — a per-item worker with
`cwd_from` must resolve it exactly like a fanout member (which receives the
inbound verbatim, sticky already folded). Sticky values are small by convention;
the REPLACE principle applies to the bulky domain payload, not the run frame.

The per-item envelope is built with `input.reply_with(input.kind, item_payload)`
— NEVER a bare `Envelope(...)` (design eval #3): reply_with preserves
`correlation_id` / `causation_id` / `baton` / `clear_id`, so item spans stitch
into the run's trace (R6) and the baton frame survives. A naive fresh envelope
would mint a new corr per item and orphan every item's trace.

NOT a copy of the whole inbound payload (the counter-review's sharpest hit): a
50-KB document × 200 items would be an invisible 200× token/memory cost. Named
carries make the cost EXPLICIT and match the engine's stage-isolation idiom
(fresh agent, named carry). A node that needs the document declares
`carry: ["doc"]` and the author sees what every item pays.

### D2. Merge (the stage's OUTPUT)

Like fanout: the merged payload = the full INBOUND payload (so downstream
branches/renders still read pre-existing keys) plus:

- `results`: list of `{"item_index": int, "payload": dict}` PAIRS, in item
  order. Pairs, not bare payloads (design eval #2): the canonical item node is
  a RESET node (an agent replaces its input), so `item_index` does NOT survive
  into the node's output — and when item 2 of 5 fails, a bare list compacts and
  `results[i]` silently misaligns. The harness knows each index at gather time
  and pins it on the pair; provenance is structural, not hoped-for.
- `failed_items`: list of the FAILED items' indexes (ints).

`min_success: k` reuses fanout's k-of-n rule: ≥ k successes → PASS with
`failed_items` naming the losses (downstream degraded-mode concern); otherwise
the stage fails with a `foreach_error` verdict naming each failure. A failed
item is an exception, a Kind.ERROR reply, or a member-returned failed VERDICT —
the exact fanout classification, shared code.

### D3. Runtime semantics (v1 scope, documented not hidden)

- **Concurrency**: an `asyncio.Semaphore(max_concurrent)` — items dispatch as
  slots free (no wave-batching; the bound is the requirement).
- **Suspend**: an item replying Kind.AWAIT parks the WHOLE stage (fanout's
  rule). Per-item gates are a named v1 NON-goal — no reserved config keys for
  it (no speculative surface); if the need materializes it gets its own design.
- **Retry**: `max_attempts` (default 1) re-runs the whole produce — ALL items.
  Documented loudly: a retried 200-item swarm costs 200 more calls. Per-item
  retry is a named non-goal for v1; `min_success` is the partial-failure tool.
  The SEPARATE transient budget (`error_retries`, default 2) is EXEMPT for
  `foreach_error` (impl-eval MED): one item's 429 embedded in the aggregate
  message read as "the stage is transient" and re-ran the whole swarm — but a
  swarm is not pre-effect (the healthy items' cost already happened), so
  transient re-runs do not apply; declare `min_success` to tolerate blips.
- **Durability**: in-memory, like fanout — a crash mid-swarm loses the stage's
  progress (the run resumes at the stage). For durable per-arm state, use
  `fork`/`fanin`. Stated in the reference docs, not discovered in an outage.
- **Bad input**: `payload[items]` missing or not a list → the stage fails LOUD
  with a `foreach_input` failure naming the key and the actual type. An EMPTY
  list passes with `results: []` (`min_success ≥ 1` then fails it) — a swarm
  over zero findings is a legitimate no-op (matches fanout's `roles: []`).
- **Feedback**: `feedback: true` retry keys (`feedback`/`priorAttempt`) are NOT
  threaded into per-item inputs (design eval #8) — the prior attempt is the
  whole merged swarm, semantically odd per item. Documented v1 limitation.
- **Cost attribution**: the ADR-0003 attach path (`last_model_call_span(corr)`)
  cannot disambiguate N CONCURRENT model calls on one corr — under foreach an
  item's attacher may read a sibling's span (design eval #6). Inherited from
  fanout (same shared-corr concurrency), now guaranteed rather than latent;
  known limitation, fix belongs to the attacher seam, not this shape.

### D4. Config-shape rules (validate)

- `foreach` joins the one-stage-one-parallel-shape exclusions: a stage may not
  combine it with `fanout`, `fork`, or `fanin` (extends the existing rejects).
- Structural: `foreach` must be a dict; `items` a non-empty string; `into` /
  `carry` optional (string / list of strings); `max_concurrent` a positive int.
- The stage keeps its `node` (the per-item worker) — required.
- `_STAGE_KEYS` gains `foreach`; the JSON schema is generated from it (no
  hand-sync).
- **`min_success` cross-field check MUST be updated** (design eval #1 — the
  spec's own example would fail to load otherwise): today it requires `fanout`
  to be a list and bounds `ms ≤ len(fanout)`. With `foreach` it must accept the
  key, and the only static bound is `ms ≥ 1` — the item count is runtime-sized.
  (Also fix the stale `# ≤ len(fanout)` comment in schema_gen.)
- **`untrusted-unfenced` attribution**: a foreach stage's node output lands
  NESTED under `results`, not at the top level — the untrusted-flow lint must
  not attribute the agent's authored keys to the stage's top-level payload
  (design eval nit #9).

### D5. Author-time lint (dataflow)

- **Consumes**: the stage READS `payload[<items>]` and each `carry` key from its
  inbound flow. This is NET-NEW stage-level checker plumbing (design eval #5):
  the node-consumes resolver is keyed on the per-item WORKER's type, and the
  worker is not the consumer of `items`/`carry` — the ENGINE is. So the check is
  injected in `analyze_dataflow` beside `branch.on` (the other stage-level
  read), against the INBOUND flow `pin_here` (items are read before the node
  runs) — ERROR when provably absent on a closed flow, WARNING when
  declared-absent, silent otherwise.
- **Provides**: `_transfer` gets an EXPLICIT `foreach` arm placed BEFORE the
  node-contract `else` (design eval #4 — omission is silently wrong, not a
  crash: the else would apply the per-item worker's RESET contract to the STAGE
  output, dropping every inbound key the merge preserves and manufacturing
  false hard errors on `{{results}}` reads). The arm: merged output = inbound
  keys ∪ `{results, failed_items}`; `closed` survives only if the item node
  provably cannot suspend — `pin.closed and not may_suspend(item_node_type)`,
  the one-node analog of fanout's any-role rule.
- **Per-item node's view**: inside the swarm the node's input is
  `{<into>, item_index}` ∪ carries. v1 does NOT walk the per-item node's own
  reads against that set (the node is one hop, not a sub-graph); its
  `output_schema`/contract still governs what `results` elements contain.

### D6. Why a separate key (not `fanout: {over: …}`)

The counter-review proposed overloading `fanout` (list = roles, dict = items).
Rejected: type-dependent config behavior is the exact class the load-time crash
guards were built against; the merge vocabularies genuinely differ (`roles` are
names, `failed_items` are indexes — reusing `failed_roles` for ints would lie);
and the exclusivity rejects get simpler with distinct keys, not harder. The
real maintenance cost (three shapes in the lint/schema) is paid instead with
SHARED INTERNAL MACHINERY: one produce/merge/classify core parameterized by
(request-builder, merge-keys), used by both fanout and foreach, so the shapes
cannot drift.

## Alternatives considered (rejected)

- **Two-phase author loop** (agent authors a config with N branches baked):
  works today, kept as the fallback recipe — but forfeits one-baton / one-lint
  / one-trace / cross-flow gates, and the concurrency bound becomes manual.
- **Dynamic fork** (generate branch STAGES at runtime): the graph is static
  everywhere — validate, dataflow, batons, resume; runtime-shaped graphs break
  all four for one use case foreach covers.
- **Overloaded fanout** (see D6).

## Migration slices (each keeps the suite green)

1. `Stage.foreach` + build_graph pass-through + validate structural checks +
   exclusivity rejects (+ regenerated schema). Tests: config-shape rejects.
2. `_produce_foreach` in the harness behind the `_run_attempts` seam (the
   producer pattern) + the shared classify/merge helper extracted from
   `_produce_foreach`/`_produce_fanout`. Tests: order, bound (a probe that
   asserts ≤ K in flight), failures, min_success, empty/missing/non-list,
   AWAIT parks, carry contents, full-payload NOT copied.
3. Dataflow: consumes (`items` + carries) and provides (`results`,
   `failed_items`) modeling. Tests: lint catches a foreach over a key nothing
   provides; downstream `{{results}}` render is clean; closed drops when the
   item node may suspend.
4. Docs: node-reference / archetypes entry + the EHS predesign G1 row flipped;
   an example (`examples/`) exercising a small swarm offline.

## Review record (adversarial design eval, 2026-07-06 — pre-implementation)

An opus eval falsified the draft spec against the engine source (the ADR-0006
precedent). FIVE must-fix spec bugs, all folded into the sections above before
any code:

1. **min_success guard rejects foreach at load** (`validate.py:624-633`) — the
   spec's own example wouldn't load; the upper bound is runtime-sized → D4.
2. **results provenance unsound** — reset item nodes drop `item_index`;
   compaction misaligns bare lists → `{item_index, payload}` pairs (D2).
3. **per-item headers unspecified** — a bare Envelope orphans the trace and
   drops the baton → `reply_with` mandated (D1).
4. **missing `_transfer` arm is silently wrong** — the else would apply the
   worker's reset contract to the stage output → explicit arm mandated (D5).
5. **consumes check had no mechanism** — the resolver keys on the worker's
   type; the ENGINE reads items/carry → stage-level injection specified (D5).

Design-level: #6 attacher corr-collision (documented, D3), #7 sticky dropped
from per-item inputs (fixed structurally: auto-included, D1), #8 feedback
no-op per item (documented, D3), #9 untrusted-unfenced over-attribution (D4).
Verified sound: semaphore+gather ordering, empty-list parity with fanout,
producer-selection truthiness, single-node-instance concurrency safety,
stage-id-keyed clears, the one-node closed-survival rule.

## Review record (adversarial impl-eval, 2026-07-06 — post-implementation)

A second opus eval falsified the BUILT code. Two real findings, both fixed
before commit (plus one consistency nit, accepted as the more precise side):

1. **[HIGH] Worker-consumes false load-blocker** — the ADR-0006 node-consumes
   check ran against the STAGE inbound for foreach workers, but the worker's
   real input is the engine-made per-item payload; a render worker reading
   `{{item}}` was rejected at load. Fixed STRONGER than a skip: the worker's
   reads now check against the exact per-item set `{into, item_index} ∪ carry ∪
   sticky` — an uncarried read is a provable every-run failure with the remedy
   named (`foreach.carry`). [dataflow: foreach-worker-key-absent]
2. **[MED] Transient-retry swarm amplification** — see the D3 Retry bullet;
   `foreach_error` is now exempt from the `error_retries` budget (was: 15 calls
   for 5 items on one embedded 429 at the defaults, empirically confirmed).

Verified sound by the same eval: the `_classify_parallel` extraction (no drift),
the `_run_attempts` contract (StageFailed carries the merged output), lazy
per-item envelope construction under the semaphore, the fork-branch path
(foreach runs inside a fork; an AWAIT there raises the standard gates-inside-
fork error), feedback keys don't leak per item, the concurrency test is
deterministic (not flaky) and genuinely falsifies a missing semaphore.
KNOWN residual coverage gaps, accepted for v1: no scenario runs foreach inside
a fork, `escalate:"human"` on a foreach stage, or a `feedback:true` retry —
the eval verified those paths by reading, not execution.

## Review record (fast counter-arg, 2026-07-06)

A haiku contrarian attacked the draft; verdicts applied:
1. **Payload copy bloat (accepted, reshaped D1)** — per-item input is now
   REPLACE + named carries; the full-copy design died here.
2. **Whole-stage retry cost (accepted as documentation)** — default is already
   1 attempt; the all-items re-run cost is documented; per-item retry is a
   named non-goal, not a silent absence.
3. **"Merge into fanout" (rejected with reasons, D6)** — but its maintenance
   point shaped the shared-machinery requirement.
4. **Reserve `await_mode` keys now (rejected)** — no speculative config
   surface; the limitation is documented instead.
5. **Durability gap (accepted as documentation)** — in-memory stated loudly;
   fork/fanin is the durable alternative.
