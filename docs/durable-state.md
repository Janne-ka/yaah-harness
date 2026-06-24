# Durable state store — design

Status: **design** (not built). Scope: make run state survive process restart and
cross-process resume, give side-effecting nodes execute-once, and provide the
substrate behind worker working-memory. Unblocks the **UI node + mailbox**.

> Read with `docs/design.md` (kernel + line) and `docs/TODO.md` (the items this
> closes: "Durable baton + state store", idempotency / early_review #14, "Worker
> memory / stateRef", and the UI-node dependency).

---

## 1. What is in-memory today, and what must become durable

| State | Today | Problem |
|---|---|---|
| **Baton** (resume cursor: stage / status / parked_at / ttl / concerns / pending) | `Harness._batons: dict`, bounded by evict-on-terminal + TTL sweep | A parked human gate dies with the process; a resume can only happen in the SAME process that suspended. |
| **Execute-once** (a retried/replayed side-effecting node runs once) | nothing — `idempotency_key` is a header/`NodeConfig` field with no consumer | A retried `git commit` / external POST runs twice (early_review #14). |
| **Working memory** (`stateRef` scratchpad a node reattaches to) | nothing | No place for a node's durable bytes; `get`/`post` have the node side but no durable backend. |

Out of scope here: prompt caching in the model backend (a backend concern), and a
full workflow-engine event log (we checkpoint cursors, not every event).

## 2. Principles (unchanged from the rest of the system)

- **Pluggable, config-driven.** The store is a layer like prompts / data / mcp /
  providers: an interface with swappable backends, chosen in the root config.
  In-memory is the default so nothing changes until durability is asked for.
- **Kernel untouched.** `Node` / `Envelope` / `Comms` gain nothing. The store is a
  *line/harness* dependency and a *bundled stdlib* capability, not a kernel type.
- **Thin orchestrator.** The harness uses a small store interface; it does not
  learn what a node does or where bytes live.
- **One substrate, typed facades.** All three needs are "durable key→value with
  TTL and (sometimes) compare-and-set." Build that once; layer typed views on it.

## 3. Layering

```
                ┌───────────── typed facades (stdlib) ─────────────┐
   Harness ───▶ │ BatonStore   IdempotencyStore   KV-backed        │
   builders ──▶ │ (run state)  (execute-once)      DataSource/Sink  │   ← memory get/post, stateRef
                └──────────────────────┬───────────────────────────┘
                                       │  one interface
                                ┌──────▼──────┐
                                │  KVStore    │  get/put/delete/scan/cas + ttl
                                └──────┬──────┘
                  ┌──────────┬─────────┼──────────┬─────────────┐
               memory      file      sqlite     nats_kv      (redis…)
              (default)   (debug)  (1 host)   (distributed)
```

The substrate is `KVStore`; `BatonStore`, `IdempotencyStore`, and the durable
`DataSource`/`DataSink` are thin typed views over it (distinct key namespaces).

## 4. The substrate — a base store + capability tiers, backends are EXTENDERS

We do not pick a database. We define a **base store contract**; every concrete
store (memory, file, blob/object, sqlite, mongo, redis, nats_kv, …) is an
**extender** of it, selected by config from a registry — exactly like
`ApiProvider`, `DataSource`/`DataSink`, and `PrefixRouter` already work. None is
privileged; none is baked in beyond the in-memory default.

Backends differ in what they can do (a blob/object store can't compare-and-set or
prefix-scan; a KV store can), so the contract is **capability-tiered** rather than
one fat interface every extender must stub:

```python
class Store(Protocol):                 # CORE — every extender provides this
    async def get(self, key: str) -> Optional[bytes]: ...
    async def put(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None: ...
    async def delete(self, key: str) -> None: ...

class Scannable(Protocol):             # + SCAN — needed for baton sweep & mailbox view
    async def scan(self, prefix: str) -> AsyncIterator[Tuple[str, bytes]]: ...

class CompareAndSet(Protocol):         # + CAS — needed only for distributed single-owner resume
    async def get_rev(self, key: str) -> Tuple[Optional[bytes], Optional[int]]: ...
    async def cas(self, key: str, value: bytes, *, expected: Optional[int],
                  ttl: Optional[float] = None) -> Optional[int]: ...   # expected None = create-if-absent
```

Each facade (§5–§7) declares the tier it needs; the runtime validates the chosen
extender supplies it and **fails fast** otherwise ("baton store needs a Scannable
store; backend 'blob' only provides core") instead of breaking mid-run. Values are
bytes; facades JSON-encode.

**Possible extenders (add on need — this is a menu, not a decision):**

| Extender | Family | Tiers | When it earns its place |
|---|---|---|---|
| **memory** | KV | core+scan+cas | default & tests; = today's dict |
| **file** | KV | core+scan (cas via single-writer) | single host, no deps; matches the file-based-state philosophy |
| **nats_kv** | KV | core+scan+cas | distributed/HA — NATS is already the transport (native TTL, revisions, watch) |
| **sqlite / mongo / redis / …** | KV | core+scan+cas | only if a concrete deployment already runs one |
| **dir / S3 / nats object-store** | blob | core | large working-memory bytes by handle |

Ship the **base + tiers + registry + the memory extender**. Every other row is a
drop-in `class XStore(...)` written when a real deployment needs it — no database
chosen up front.

## 5. `BatonStore` — durable run state

Replaces `Harness._batons`. The harness depends on this interface; the default
in-memory impl is today's dict + sweep, so existing behavior is bit-for-bit.

```python
class BatonStore(Protocol):
    async def save(self, baton: Baton) -> None: ...
    async def load(self, baton_id: str) -> Optional[Baton]: ...
    async def delete(self, baton_id: str) -> None: ...
    async def sweep_expired(self, now: float) -> List[str]: ...      # evict + return ids
    async def list_suspended(self) -> List[Baton]: ...               # the mailbox view (§8)
```

**Serialization.** `Baton` gains `to_dict`/`from_dict`. `pending` is an `Envelope`
(already JSON via `to_dict`/`from_dict`); `concerns` are small dicts; the rest are
scalars. So a baton is a JSON object under key `baton:<id>`.

**Harness integration** (mechanical; the run loop is unchanged):

| Method | Today | With a store |
|---|---|---|
| `__init__` | `self._batons = {}` | `self._batons = baton_store or InMemoryBatonStore()` |
| `_settle` (suspend) | keep in dict | `await store.save(baton)` |
| `_settle` (terminal) | `pop` | `await store.delete(baton.id)` |
| `resume` | `self._batons.get(id)` | `await store.load(id)` |
| `sweep_expired` | scan dict, pop | `await store.sweep_expired(now)` |

Calls become `await`; in-memory ops are trivially async. A running baton is a
local var (not persisted) at **Level 1** — the store only ever holds *parked*
runs, exactly as `_batons` does today.

### Two durability levels (pick the goal)

- **Level 1 — gate durability (build first).** Persist on suspend, delete on
  terminal. A parked human gate survives restart, and **resume can run in a
  different process** (the baton is the rendezvous). One write per gate. Does NOT
  survive a crash *mid-stage* — that run is lost and re-run from the top.
  Closes the human-gate / mailbox need with minimal cost.

- **Level 2 — checkpoint durability (later).** Also persist the baton **and the
  inter-stage input envelope** after each completed stage. On startup a recovery
  pass scans `status="running"` batons and resumes each from its last checkpoint.
  Survives a crash mid-run. Cost: one write per stage, and `Baton` must carry the
  resume input (a `cursor_input: Envelope`). Pairs with §6: a stage that ran its
  side effect then crashed before checkpoint re-runs on recovery, so **idempotency
  is what makes Level 2 exactly-once** — design them together, ship L1 first.

## 6. `IdempotencyStore` — execute-once for side effects

```python
class IdempotencyStore(Protocol):
    async def lookup(self, key: str) -> Optional[dict]: ...            # cached result, or None
    async def claim(self, key: str) -> Tuple[bool, Optional[dict]]: ...# (won_first, existing)
    async def finalize(self, key: str, result: dict) -> None: ...      # store the result
    async def release(self, key: str) -> None: ...                     # failed → let a retry re-claim
```

**Where it plugs in: a wrapper node, by config.** Side effects are `post` /
`transform` / `shell` nodes. Mark one `"idempotent": true`; the builder wraps it:

```python
class OnceNode:           # wraps a side-effecting inner node; key = idempotency_key
    async def invoke(self, input, config):
        key = config.idempotency_key or input.headers.get("idempotency_key")
        if not key:
            return await self._inner.invoke(input, config)   # no key → not guarded
        hit = await self._store.lookup(key)
        if hit is not None:
            return Envelope.from_dict(hit)                    # already ran → cached output
        out = await self._inner.invoke(input, config)
        await self._store.finalize(key, out.to_dict())
        return out
```

Two phases:
- **Phase A — sequential dedup (build first).** The `lookup`/`finalize` shown
  above. Covers the real #14 case: within one run the retry loop is sequential, so
  a second attempt finds the first's result. No CAS needed.
- **Phase B — concurrent replicas.** Two replicas may both miss → both run. Use
  `claim` (CAS create of a "pending" marker via the store's `cas(expected=None)`): the
  winner runs + `finalize`s; the loser polls `lookup` until the result appears
  (bounded wait) or the claim is `release`d after a failure. Only needed when the
  same key can be processed by parallel workers.

The key derives from the existing `idempotency_key` (Envelope header / NodeConfig);
the app sets it (e.g. `task-123:commit`).

## 7. Working memory / `stateRef` — no new node type

Durable working memory is just a `get`/`post` whose source/sink is KV-backed:

- A `KvDataSource` / `KvDataSink` over the same `KVStore` (namespace `mem:`),
  registered as a data source/sink so `get`/`post` nodes use the normal
  `source:key` routing (`"source": "mem:run-123/scratch"`).
- **`stateRef`** is just that handle string. Convention: a node receives/returns a
  `stateRef` header (or payload field) naming its working-memory key; `get` reads
  it in, `post` writes it out. The bytes live in the substrate, never in the node
  or the envelope — so a large scratchpad doesn't ride every hop (mirrors
  `tail_only` for shell).

So "memory get / memory post" need **zero new code beyond a KV-backed
source/sink** — the node layer already exists.

## 8. The mailbox / UI node falls out of this

A durable `BatonStore` with `list_suspended()` *is* the mailbox backbone:

- On suspend, the baton (awaiting tag + concerns + pending artifact + baton_id) is
  already in the store — that record **is** the pending question.
- A UI process calls `list_suspended()` to show open gates, collects a human
  answer, and calls `harness.resume(baton_id, answer)` — which now works from **any
  process** because the baton is durable.
- `nats_kv` `watch` lets the UI react to new gates without polling.

So the UI node becomes thin: render suspended batons, post answers via `resume`.
The gate **driver** (already built) is the in-process version of the same loop; the
mailbox is its durable, cross-process counterpart. No harness change beyond §5.

**Principle — the human-interaction surface is a pluggable worker over a contract,
not a fixed thing.** The two seams ARE the contract: the gate driver's `Decider`
(`Suspended -> Envelope`) and the mailbox (`list_suspended` / `resume`). A decision
can come from a config map, an **agent** (auto-answer or assist), a **traditional
UI** (a human via a form), or **both — the best option**: the agent reads the gate +
concerns and drafts the question/answer; the UI presents it and lets the human
review/edit/commit (an AI-assisted gate — the agent lowers load, the UI keeps
control and trust visible). All are interchangeable workers behind the same
suspend/resume contract; "workers not citizens" applies to humans-in-the-loop too.
The decision *source* is plumbing; the decision *contract* is fixed.

## 9. Configuration & wiring

Root config gains one block (absent → in-memory, today's behavior):

```jsonc
"state": { "type": "memory" }            // default; a durable extender (file / nats_kv / …) is dropped in per-deployment
```

- `runtime` builds one store from `state` (via the backend registry), then derives `BatonStore` +
  `IdempotencyStore`, and (optionally) registers a `mem:` data source/sink over it.
- `BatonStore` → `Harness(..., baton_store=…)`.
- `IdempotencyStore` → `BuildContext` (the `OnceNode` wrapper reads it), alongside
  the existing `data_source` / `data_sink` / `mcp_source`.

This mirrors the existing builder pattern — a `_STATE_TYPES` factory map fed to the
same generic builder used for the other layers (`_build_router`-style).

## 10. Consistency & failure model

- **At-least-once delivery, idempotent effects.** The harness retries and (L2)
  re-runs on recovery; `OnceNode` + `IdempotencyStore` collapse repeats on
  side-effecting nodes. Pure nodes need no guard (re-running is harmless).
- **Single-owner baton.** A baton is processed by one holder at a time. With
  durable storage, enforce this with a CAS on the baton's revision at resume
  (`cas(expected=loaded_rev)`) so two processes can't resume the same gate twice.
- **TTL still applies.** `Baton.is_expired` is unchanged; the store's `sweep_expired`
  enforces it (a backend's native per-key TTL, where it has one, is a backstop).

## 11. Phased plan

The store interface is the deliverable; **which durable backend** is deferred (not
a current concern — see §4, it's a per-deployment drop-in extender). So the plan
builds the contract + the in-memory extender, and proves cross-instance behavior
with whatever extender is available.

1. **Base store + capability tiers + backend registry + the `memory` extender**
   (+ tests, incl. `scan`/`cas`).
2. **`BatonStore` + Baton (de)serialization; harness uses it; Level 1.** Default
   in-memory ⇒ no behavior change. (Cross-instance/-process resume is proven once a
   durable extender exists — step 4.)
3. **`IdempotencyStore` Phase A + `OnceNode` (`idempotent: true`)** — prove a
   retried `shell`/`post` runs its effect once.
4. **First durable extender, when a deployment needs it** — e.g. `nats_kv` (NATS is
   already the transport) to prove cross-PROCESS suspend/resume, or `file` for a
   single host. Either is a `class XStore(...)`; nothing above changes.
5. **KV-backed `mem:` source/sink + `stateRef`** convention.
6. **Mailbox view (`list_suspended`) + a thin UI node** (§8).
7. *(later)* **Level 2 checkpointing** + recovery pass; **Phase B** concurrent
   claims.

Build 1–3 first: they close idempotency (#14) and put the resume cursor behind a
store, with no backend decision required. A concrete durable extender (step 4)
arrives only when a deployment actually needs to survive a restart.

## 12. Open decisions

- **L1 vs L2 now.** L1 (gate durability) is cheap and covers the stated need;
  full crash-resume (L2) is a bigger commitment with a write per stage. Recommend
  L1 first, L2 only on a measured need.
- **Which durable extender first — DEFERRED, not a current concern.** The base +
  `memory` ship now; a concrete extender (file / nats_kv / other) is written when a
  deployment needs durability, against the unchanged base.
- **Idempotency key ownership.** App-set per side effect vs harness-derived
  (`correlation_id` + role + attempt-independent). Start app-set; it's explicit.
