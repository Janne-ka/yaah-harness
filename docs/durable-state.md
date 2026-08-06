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
                                │  StoreBackend    │  get/put/delete/scan/cas + ttl
                                └──────┬──────┘
                  ┌──────────┬─────────┼──────────┬─────────────┐
               memory      file      sqlite     nats_kv      (redis…)
              (default)   (debug)  (1 host)   (distributed)
```

The substrate is `StoreBackend`; `BatonStore`, `IdempotencyStore`, and the durable
`DataSource`/`DataSink` are thin typed views over it (distinct key namespaces).

## 4. The substrate — a base store + capability tiers, backends are EXTENDERS

We do not pick a database. We define a **base store contract**; every concrete
store (memory, file, blob/object, sqlite, mongo, redis, nats_kv, …) is an
**extender** of it, selected by config from a registry — exactly like
`ApiProvider`, `DataSource`/`DataSink`, and `PrefixRouter` already work. None is
privileged. (Status 2026-07: three extenders ship — `memory` the default,
`file` single-host durable, `postgres` shared-database durable with an
optional psycopg dependency; further ones register through the plugins seam.)

Backends differ in what they can do (a blob/object store can't compare-and-set or
prefix-scan; a KV store can), so the contract is **capability-tiered** rather than
one fat interface every extender must stub:

```python
class StoreBackend(Protocol):                 # CORE — every extender provides this
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

**Lifecycle — `close()` is an optional capability, not a tier.** An extender that
holds a long-lived resource (postgres: a DB connection) exposes `async def
close()`; memory and file hold none (a dict; a per-op file open+close) and define
none. The runtime builds one backend per action from the `state:` block and
RELEASES it on exit — `runtime.run_root`/`list_gates`/`resume_gate`/`clear_state`/
`baton_schema` build under `runtime_factories.opened_store`, which `getattr`-probes
`close` and awaits it (normal, suspend, or error exit). An INJECTED backend is
caller-owned and left open — the same ownership-aware stance as
`experiment.store_factory.opened_store`. `close()` is deliberately off the port:
adding it to the core tier would force every extender (including future blob
stores) to stub a no-op, contradicting "an extender implements only what it can."
Crucially, `close()` releases the CONNECTION, never the durable state — a parked
baton persists (§5), so a fresh backend instance in another process (or a later
resume in this one) still finds and drives it.

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

- **Level 2 — checkpoint durability (SHIPPED, status 2026-07).** After each
  completed stage the harness ALSO persists the baton with its cursor advanced to
  the next stage (`status="running"`), carrying the envelope that will feed it
  (`Baton.cursor_input`). A run killed mid-flight is recoverable —
  `Harness.resume_running(baton_id)` (runtime `resume_run`; CLI `yaah resume-run
  <root> ID`) re-drives from the cursor stage. Cost: one best-effort write per
  stage. Pairs with §6: a stage that ran its side effect then crashed before the
  checkpoint re-runs on recovery, so **idempotency is what makes Level 2
  exactly-once**.

  **One artifact, not two.** The design's "persist the baton AND the inter-stage
  input" is satisfied by the input riding ON the baton (`cursor_input`), persisted
  through the SAME `BatonStore` under the SAME `baton:<id>` key. This is deliberate:
  cursor + input in one write is atomic — a crash can never leave a cursor pointing
  at a stage whose input was not persisted (two separate artifacts would have a
  torn-write window and a second store to keep consistent on recovery). `to_dict`/
  `from_dict` round-trip `cursor_input` exactly like `pending` (both Envelopes).

  **Where it is written / deleted** (`harness.py`):
  - `_checkpoint(baton, next_input)` sets `cursor_input` + `checkpointed_at` and
    `save`s the baton. Called from `_drive` after each completed stage advances the
    cursor to a non-None next stage (the linear pass AND after a fork completes).
  - **Best-effort:** a checkpoint-write blip is NOTED on the trace
    (`event: checkpoint_failed`) and swallowed — the Level 1 park remains the
    authoritative durability point, so durability degrades, the run does not fail.
    The FIRST such failure also prints one stderr warning ("crash recovery is OFF
    for this run"); later ones are trace-only, since the cause is a broken store,
    not a per-stage event.
  - **Deleted on terminal** (`_settle` deletes the baton on Done/StageFailed) and
    **cleared on park** — a `_Suspend` transitions the record running→suspended and
    nulls `cursor_input`/`checkpointed_at`, so a parked gate is Level 1's own
    artifact and never ALSO looks mid-run-resumable (`list_running` filters
    `status=="running" and cursor_input is not None`).
  - **TTL sweep** reclaims an abandoned running checkpoint (`Baton.is_expired`).
    Since the split the two stored kinds have SEPARATE windows: a suspended gate on
    `baton_ttl` from `parked_at` (a human patience window), a running checkpoint on
    **`checkpoint_ttl`** from `checkpointed_at`. The second clock restarts at every
    completed stage, so it bounds how long ONE stage may be in flight before its
    recovery record is swept — set it above your slowest stage's wall-clock.
    `checkpoint_ttl` absent **inherits `baton_ttl`** (the pre-split behaviour):
    there is no engine default, because one would silently shorten an existing
    deployment's recovery window on upgrade. Recommended explicit value: `21600`.
  - **Written BEFORE the first stage of each leg** (since the first-stage
    checkpoint). Both windows that used to be uncovered are now inside it:
    - **The first stage of a run.** `Harness.run` mints the baton, `_checkpoint`s it
      with the task envelope, and only then drives into `graph.start`. A kill during
      the first stage leaves a recoverable checkpoint cursored at `graph.start` —
      which is exactly where a long seeding/discovery stage sits.
    - **The first stage after a gate resume.** `Harness.resume` merges the decision,
      advances the cursor, and checkpoints *before* driving on — so the human's
      decision is itself persisted. A kill in that window recovers with
      `yaah resume-run`, with the decision intact, and the gate does **not** re-open
      (the record is `running`, so `resume()` refuses it). Before this, the decision
      had to be submitted again.

      **One narrow window remains, and it is honest in the code.** The checkpoint is
      guarded by `if baton.stage is not None` — a gate whose decision routes to a
      TERMINAL outcome has no next stage to cursor at, so nothing is written and the
      record stays `suspended` until `_settle` deletes it. A kill between the merge
      and that delete therefore leaves the gate OPEN, and the decision must be
      submitted again. The alternative — writing a `running` checkpoint with a null
      cursor — would be a record that `resume_running` cannot drive and `resume`
      refuses: an un-recoverable run in place of a re-answerable gate. Re-answering a
      gate whose decision was terminal is the cheaper loss.

    Two honest consequences of that write:
    - The durable store now holds **one record from t0** of every run, not only of
      runs that have completed a stage. Delete-on-terminal covers it (`_settle`
      deletes on Done and on StageFailed), and the TTL sweep covers an abandoned
      one, so nothing leaks — but the store is never empty while a run is live.
    - `clear()`'s **`checkpoints_dropped` now counts never-advanced runs** too. The
      number is larger for the same fleet than it was; that is the checkpoint
      becoming complete, not a leak.
    - The first stage is now **re-runnable on recovery**, which is the at-least-once
      contract applied one stage earlier than before. A seeding first stage that
      commits a side effect (creates a worktree, claims a ticket, posts a webhook)
      needs the same idempotency guard as any other side-effecting stage (§6); a
      pure discovery/seed stage re-runs harmlessly.

  **Fork scope (v1).** Only the MAIN chain is checkpointed. A fork's inner branch
  stages run inside `ForkCoordinator` (via `_exec_stage`, not `_drive`), so they are
  not individually checkpointed and cannot double-checkpoint; the checkpoint after a
  fork stage completes records "fork done, cursor at the next main stage", so
  recovery re-runs the WHOLE fork (all branches) — acceptable under the
  at-least-once contract. Per-branch checkpointing is deferred.

  **Wiring fingerprint (SHIPPED).** `Baton.wiring` carries `wiring_fingerprint(graph)`
  — a sha256 over the graph's TOPOLOGY, stamped at mint and RE-STAMPED by every
  checkpoint, so it means "the topology this cursor was produced by".
  `resume_running` REFUSES a mismatch (escape: `--allow-rewiring`), naming the cursor
  stage and whether it still exists; a gate `resume` only WARNS, because a parked
  human must not lose their decision to an unrelated edit — EXCEPT when the parked
  stage itself vanished, which is a clean refusal instead of the bare `KeyError` that
  path used to raise. A pre-upgrade baton carries no stamp and is recovered with a
  note.

  The re-stamp is what keeps `--allow-rewiring` a ONE-TIME assertion. Stamping only
  at mint meant a run recovered onto an edited graph kept claiming the original
  topology while driving the new one, so every later crash refused again and the flag
  had to be passed once per crash — each time asserting compatibility with a graph
  nobody was running any more. The cost is that `wiring` is not mint provenance;
  nothing reads it as such, and a run's origin belongs on the trace.

  What the fingerprint covers is deliberately narrow: `start`, `sticky`, and per
  stage its name, node role/id, `then`, `branch` (on/routes/default), `fork`,
  `fanin.expect`, `fanout`, `foreach.items`, `final`. It EXCLUDES prompts, models,
  timeouts, retry budgets and validators — behaviour drift is `config_fingerprint`'s
  job (experiment identity), not recovery's. "Fix the prompt, resume the run" is the
  most common recovery there is; a check that refused it would train operators to
  pass `--allow-rewiring` reflexively and cost the guard its whole meaning.

  **Liveness lease + CAS claim (SHIPPED).** `Baton.owner` (`<host>/<pid>/<nonce>`)
  and `leased_at` are stamped on every checkpoint write — no heartbeat thread,
  because since the first-stage checkpoint every stage boundary is already a store
  write, so the lease refreshes exactly as often as the run makes progress. A park
  CLEARS both (a parked gate has no owning process). `resume_running` then decides in
  three tiers (`LeaseState`): **no owner** → allow with a note (a pre-upgrade
  record); **same host** → a real `os.kill(pid, 0)` probe, dead ⇒ recover, alive ⇒
  REFUSE naming host/pid (escape: `--force`, with a loud stderr line); **foreign
  host** → no probe is possible, so fall back to the lease age against root
  `lease_horizon` (default 3600s) — past it ⇒ allow with a warning, within it ⇒
  refuse. The decision is then CLAIMED with `BatonStore.claim(baton, expected_rev)`
  (CAS on the revision it was read at), so two operators racing the same recovery
  cannot both win; the loser is refused naming the winner.

  `--force` and `--allow-rewiring` are SEPARATE flags on purpose: they waive two
  different assertions ("that process is dead" / "this graph edit is
  cursor-compatible"), and one combined flag would let an operator silence the check
  they never considered.

  **v1 non-proofs, documented rather than defended against.** Clock skew between
  hosts distorts the foreign-host age (the horizon is a coarse fallback, not a
  consensus protocol). A SIGSTOP'd process answers the liveness probe and so refuses
  forever — the escape is `--force`. PID REUSE can report a dead owner as live (a
  false REFUSE, the safe direction). **The CAS claim fences RECOVERY, not the
  INCUMBENT: `_checkpoint` saves unconditionally, so an owner wrongly presumed dead
  (the foreign-host-past-horizon case) re-takes the record at its next stage boundary
  and the two drivers alternate ownership rather than one being stopped.**
  `FileBackend`'s CAS is flock-serialized, which is advisory over NFS;
  `BatonStore.has_cas()` is false on a backend without the tier and the refusal
  message says the claim was advisory. Every one of these fails toward "refuse" or
  "one loud `--force`", never toward a silent double-drive — except the incumbent
  case above, which needs the per-checkpoint claim below.

  **Container hostnames (root `lease_host`).** The same-host pid tier keys on
  `socket.gethostname()`, which in a container is the pod/container id and is
  different on every restart — so the same machine looks like a new host each time
  and every one of its own runs is `foreign`, decided by the coarse age guess rather
  than by a real `kill(pid, 0)`. Root `lease_host` overrides the name (absent =
  `gethostname()`, so nothing changes for anyone who does not set it). Give it a
  stable identity that is **unique per kernel** — the k8s node name, the VM's
  hostname. Two containers on *different* kernels sharing one `lease_host` would
  probe each other's pid namespace and read a coincidental pid as "alive".

  Recovery remains an EXPLICIT operator action naming a specific baton, never
  automatic, and `yaah run` does NOT gate on the presence of running checkpoints
  (that would deadlock a fleet whose concurrent runs each hold a live checkpoint on
  the shared store). `yaah list` now labels each RUNNING line
  `owner=… lease=live|stale(2h14m)|foreign(2h14m)|none` and prints the `resume-run`
  hint ONLY when the lease is not live.

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

- A `KvDataSource` / `KvDataSink` over the same `StoreBackend` (namespace `mem:`),
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
"state": { "type": "memory" }            // default; durable: {"type":"file","dir":...} or {"type":"postgres","dsn":...}
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
- **Single-owner baton — RECOVERY is fenced (shipped); the INCUMBENT is not.**
  Two mechanisms, both in §5: a **liveness lease** (`owner`/`leased_at`, refreshed
  by every checkpoint) answers *is the owner still alive?*, and a **CAS claim**
  (`BatonStore.claim(baton, expected_rev)` → `cas(expected=loaded_rev)`) makes the
  recovery itself atomic, so two processes can't both *take* the same record. The CAS
  tier is probed with `getattr` (the same optional-capability stance as `close()`):
  a backend without it falls back to a plain put and the claim is ADVISORY, which
  the refusal message states.

  What that does **not** give you is a fence on the process already running.
  `_checkpoint` saves unconditionally — it never checks whether it still owns the
  record — so if a recovery took the baton from an owner that was in fact alive (the
  foreign-host-past-horizon tier, or a `--force`), that owner simply re-stamps itself
  as owner at its next stage boundary and both keep driving. The claim decides who
  *starts* a recovery, not who *continues*. Closing it is per-checkpoint CAS, in
  Phase B below.
- **TTL still applies**, with SEPARATE windows per stored kind: a suspended gate on
  `baton_ttl` from `parked_at`, a running checkpoint on `checkpoint_ttl` (inheriting
  `baton_ttl` when unset) from `checkpointed_at`. `Baton.is_expired` decides; the
  store's `sweep_expired` enforces it (a backend's native per-key TTL, where it has
  one, is a backstop). `lease_horizon` must be ≤ the effective checkpoint window —
  otherwise a foreign host's crashed run is swept before it is old enough to be
  declared stale, and could never be recovered. `validate_budgets` rejects that.

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
7. **Level 2 checkpointing + recovery** (SHIPPED 2026-07 — `_checkpoint` /
   `Harness.resume_running` / `yaah resume-run`, §5). **Completed 2026-08:** the
   first-stage checkpoint (both legs), the wiring fingerprint, the liveness lease +
   CAS single-owner claim (§10), and the split `checkpoint_ttl` / `lease_horizon`
   windows. *(later)* **Phase B** concurrent claims (many workers pulling from one
   queue of checkpoints, rather than an operator naming one), and with it
   **per-checkpoint CAS + refuse-on-lost**: `_checkpoint` would write under a CAS on
   the revision it last held and, on losing it, STOP the run instead of re-stamping
   itself as owner. That is what fences the INCUMBENT (§10) — today's claim only
   fences the recovery. It costs a `load_rev` per stage boundary and turns a
   best-effort write into a run-terminating condition, so it belongs with Phase B's
   concurrency rather than being bolted onto the operator-driven recovery path.

Build 1–3 first: they close idempotency (#14) and put the resume cursor behind a
store, with no backend decision required. A concrete durable extender (step 4)
arrives only when a deployment actually needs to survive a restart.

## 12. Open decisions

- **L1 vs L2 now — RESOLVED (status 2026-07):** L1 (gate durability) shipped
  first; L2 (full crash-resume, one best-effort write per stage) shipped when a
  measured need arrived — a killed s_factory run lost all completed mid-run stages
  (RED/code/GREEN unrecoverable). Both against the unchanged store contract.
- **Which durable extender first — RESOLVED by need (status 2026-07):** `file`
  shipped first (cross-process gates), `postgres` followed (shared-database
  durability for multi-host + experiment campaigns) — both written against the
  unchanged base, as designed.
- **Idempotency key ownership.** App-set per side effect vs harness-derived
  (`correlation_id` + role + attempt-independent). Start app-set; it's explicit.
