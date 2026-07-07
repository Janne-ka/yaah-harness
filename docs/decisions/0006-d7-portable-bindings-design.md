# 0006-D7 — Portable bindings for node contracts (design)

**Status:** Proposed (design-only). Companion to `0006-node-contract-capability.md` §D7 (which
carries the REQUIREMENTS this doc turns into a NORMATIVE shape). Nothing here is built yet.
**Date:** 2026-07-07
**Scope:** the two binding-time sources ADR-0006 deferred — a **frozen manifest** (so author-time
lint can ERROR-check pipelines that use CUSTOM node types WITHOUT importing them) and a **live
describe mode** (for CI). Covers BOTH sides of the contract: `provides` (§D1) and `consumes` (§D10).

This is prescriptive. Where it says MUST, deviating is a bug.

---

## 0. What already exists (so the design stays honest about its size)

The static core (§D1–D5, §D10) shipped. The seam D7 plugs into is already in the code:

- `resolve_contract(ntype, cfg, *, contract_for=None)` and
  `resolve_consumes(ntype, cfg, base_path, *, consumes_for=None)` (`node_contract.py`) take an
  **injectable source**. Default is `builtin_contract_for` / `builtin_consumes_for` (built-ins only).
  **`resolve_contract` / `resolve_consumes` need NO change for D7** — a manifest or a live registry
  is just a different callable passed as `contract_for` / `consumes_for`.
- A Contract is a pure `contract(cfg) -> Contract` (four fields: `mode, provides, complete, closed`);
  `consumes(cfg, base_path) -> frozenset`. **Neither runs the node, does I/O, nor raises.** This is
  the single fact that shrinks live-describe (see §3).

The gaps D7 fills are exactly two, and both are small:

1. **No custom-type contract source.** Node TYPES register via `build.Registry.register(type_name,
   builder)` — which has **no contract slot**. Built-in contracts live in the separate
   `node_contract.BUILTIN_CONTRACTS` dict. So an app-registered node type has no way to supply a
   `contract(cfg)`; it resolves to `opaque` (sound skip, but zero checking).
2. **The seam isn't forwarded.** `analyze_dataflow` calls `resolve_contract(node.get("type"), node)`
   (`dataflow.py:158`) and `resolve_consumes(...)` (`:425`) with the DEFAULT source — it never
   threads a caller-supplied `contract_for` / `consumes_for` down. So even a fully-registered custom
   contract can't reach the lint today.

Everything below is built on those two seams, not on new machinery.

### 0.1 Two worlds, and why the manifest is narrower than it first looks (verified 2026-07-07)

A first draft of this design treated the frozen manifest as the centrepiece. Verifying against the
actual code moved it to a DEFERRED tier. Three facts, checked in-tree:

- **CLI `yaah validate` ALREADY imports `plugins:` modules** before validation
  (`cli.py:1010` `load_plugins(...)`, printed as a note). So at the CLI, "validate must not import
  app code" is **already false for `plugins:`** — the no-import property the manifest exists to
  preserve is not a property CLI-validate actually has today.
- **But `plugins:` does not cover node TYPES.** `plugins._KINDS` is providers/state/prompt/data/…;
  a NODE type has no `plugins:` path — it registers only via `build.Registry.register` +
  `build(registry=…)`, an **embedding-app** path unreachable from `yaah validate <file>`.
- **`validate_pipeline` does NOT reject an unknown node type** (`validate.py:542` only requires a
  `type` string to be present). A custom `type:"my_thing"` PASSES validate and fails only at BUILD
  (`Registry.build` → KeyError). So today a custom node reaches the dataflow lint as an unregistered
  string → `opaque`.

**Consequence — the two worlds have different needs:**

| World | How a custom node arrives | Live contract reachable? | Needs a manifest? |
|---|---|---|---|
| **Embedding app** (`build(registry=…)`, app drives validate/lint) | programmatic `Registry.register` | **yes** — the registry is in-process; pass `contract_for=registry.contract_for` | **no** — live is cheaper and drift-free |
| **CLI `yaah validate <file>`** | there is **no** node-type plugin path today | not without first adding a node-plugin path — which would IMPORT the module anyway (like `plugins:`) | only if you want checking **without import** (untrusted config, or a cross-language node) |

So the manifest's ONLY unique job is **"ERROR-check a custom node WITHOUT importing its code"** — and
the sole present holders of that need are (i) cross-language node types (a C node in a Python engine,
whose fn Python can't call) and (ii) an untrusted-config policy. **Neither has a present stakeholder.**
The embedding app — which is the real, present consumer of custom node types — is served fully by the
LIVE path (§3), which is small and drift-free. Therefore:

> **v1 pin: build the LIVE path (§3, slices D7.1–D7.2). The frozen MANIFEST (§1–§2, slices
> D7.3–D7.4) is specified normatively here but DEFERRED — no present stakeholder, and per the
> project's no-speculative-building rule it is spiked, not shipped, until a cross-language port or an
> untrusted-config requirement is real.** The manifest spec below is written so it can be picked up
> unchanged when that day comes; it is not dead design, just unbuilt.

---

## 1. Manifest format (the config-dependence problem, answered) — DEFERRED tier

> This section specifies the manifest normatively but it is the DEFERRED tier (§0.1): built only when
> a cross-language port or an untrusted-config / introspection-dependent contract is a real need. The
> v1 shipped path is the live registry (§3).

### The problem ADR-0006 flagged

A Contract is a FUNCTION of cfg, not a constant per type:

    worktree op:add    -> reset({workdir, branch, repo, base} ∪ carry, closed=True)
    worktree op:remove -> reset({removed, ok}, closed=True)
    agent parse:false  -> reset({raw} ∪ carry ∪ cwd, closed=True)
    agent parse:true+schema -> reset(schema keys ∪ {raw} ∪ …, complete=True, closed=False)

A naive `type -> keys` manifest is therefore WRONG (§D7 forbids it explicitly). The two honest
shapes ADR-0006 named:

| | **(a) per-INSTANCE freeze** | **(b) per-config-KEY conditional rules** |
|---|---|---|
| What it is | snapshot `describe(cfg)` for each concrete node in ONE pipeline | a mini-DSL encoding `if op==remove then … else …` the port re-evaluates against cfg |
| Produce | trivial — call the resolver over the pipeline's nodes, dump the result | hard — hand-author rules, or a tool emits the DSL; must express set-unions over cfg-derived keys (`carry`, `cwd_from`) |
| Consume | trivial — a lookup | needs a **rule interpreter in every language port** |
| Config-independent? | **no** — one manifest per pipeline | yes — one manifest per plugin type, reused |
| Handles config-dependence | **yes, for free** — each distinct cfg is its own entry | yes, if the rules cover it |
| Staleness on a cfg edit | detectable (see §2) | mild — only when the fn logic changes |
| Port work | **zero** (read JSON) | a DSL evaluator, rebuilt & kept-identical per port |

**(b) is exactly the fragility ADR-0006 rejected for source-parsing, relocated.** It rebuilds
`contract(cfg)` as a data-language every port must interpret identically — and there is no port and
no stakeholder for it. It is speculative building. **REJECTED for v1** (kept as a documented future
tier in §6 if a pipeline-independent, plugin-shipped manifest is ever genuinely needed).

### Manifest pin (when built) — per-instance freeze, **keyed by a hash of the node's cfg**

The refinement that makes (a) sound rather than merely simple: key the manifest not by node ROLE but
by a **stable content hash of the node's full config**. This falls out of the existing seam and
defangs staleness at the same time.

    // <pipeline-file>.contracts.json
    {
      "manifest_version": 1,
      "engine": "yaah-py",              // producer id — informational, for the stale-vs-port note
      "frozen_at": "2026-07-07T…Z",     // informational
      "nodes": {
        // key = sha256( canonical_json( node_cfg ) ), hex
        "3f9a…": {
          "role": "cleanup",            // hint only (last role frozen with this cfg) — NOT the key
          "type": "worktree_gc",        // re-verified on lookup (collision guard)
          "provides": {"mode": "reset", "keys": ["removed", "ok"],
                       "complete": true, "closed": true},
          "consumes": ["target_dir"]
        }
      }
    }

- **The key is the cfg hash.** Lookup in the injected `contract_for(ntype, cfg)` is:
  `h = sha256(canonical_json(cfg)); entry = nodes.get(h); if entry and entry.type == ntype: build
  Contract from entry.provides else None`. This fits the EXISTING `(ntype, cfg)` seam with **no
  signature change**, and it is why the resolver itself needs no edit.
- **Config-dependence is handled trivially and correctly.** `op:add` and `op:remove` are different
  cfgs → different hashes → separate entries, each carrying its own frozen Contract. The
  ADR-forbidden `type -> keys` collapse cannot happen; a manifest that mislabels one is caught by
  AT5 (§5).
- **`type` is re-verified on lookup**, not trusted from the hash — a sha256 collision (non-risk, but
  free to guard) or a hand-edited manifest can't smuggle a wrong-type contract in.
- **`canonical_json`** = `json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
  over the RAW node dict (before any `--from-code` augmentation — freeze already holds the real
  contract, so it never needs augmenting). The hash is over the WHOLE cfg, not a salient-keys subset:
  over-invalidation (an irrelevant `prompt_file` edit forces a re-freeze) is SOUND — worst case you
  re-freeze more often — whereas a partial hash risks UNDER-invalidation (a contract-relevant key the
  partial logic forgot → silently-wrong contract), which is the one failure mode we refuse. Documented
  as a known cost (§6).

Consumes rides the same entry (`"consumes": [...]`) — a static frozen set; `base_path` is irrelevant
on lookup because the render template (or custom reader) was already parsed at freeze time.

---

## 2. Freeze flow, and staleness (judged honestly) — DEFERRED tier (belongs to §1's manifest)

### Who writes it, where it lives, how the resolver finds it

- **Producer: a new CLI verb `yaah freeze-contracts <root-config>`.** It (1) `load_plugins` +
  registers node types — this **imports app code**, the same trust boundary `plugins:` already
  crosses at validate/run time, printed loudly; (2) walks the pipeline's `nodes`, calling the
  live-registered resolver for each node's `provides` Contract and `consumes` set; (3) writes
  `<pipeline-file>.contracts.json` beside the pipeline file.
- **Location = a convention path** (`<pipeline-stem>.contracts.json`, next to the pipeline), so a
  plain `yaah validate` discovers it with **zero config change**. An explicit override
  (`graph.contracts_manifest: "path"` in the root) is allowed for non-conventional layouts but is not
  required. The manifest sits beside the PIPELINE (nodes live there), not the root.
- **Discovery in validate/lint:** if the convention path (or override) exists, build a manifest
  `contract_for` / `consumes_for` closure (the hash lookup above), compose it into the seam (§4),
  pass it to `analyze_dataflow`. Absent manifest → today's behaviour exactly.
- **Plugin authors ship a FUNCTION, not a manifest.** A plugin registers a pure `contract=` /
  `consumes=` beside its builder (§3). Manifests are per-PIPELINE artifacts produced by the app that
  USES the plugin, via `freeze-contracts` — not shipped by the plugin. (This keeps a plugin's
  contract config-general — a function — and pushes the pipeline-coupling into the freeze, where it
  belongs.)

### Staleness — what is and isn't detectable (the honest answer)

Two independent staleness sources; they have DIFFERENT detectability, and saying so is the point:

1. **cfg edited after freeze** (author changes `op: add` → `op: remove`, or any key): **DETECTABLE,
   for free.** The new cfg hashes differently → manifest MISS → the resolver falls through to
   builtin/inline/**opaque** (a sound skip), NEVER to the stale frozen contract. So an edited node
   silently LOSES its ERROR-checking until re-freeze — it never gets WRONG checking. This is the
   whole reason for hash-keying instead of role-keying (a role key would still resolve → serve the
   stale contract → silently wrong).

2. **plugin module's contract LOGIC changed, cfg identical** (someone edits `worktree_gc`'s
   `contract()` fn but the pipeline node is untouched): **NOT detectable at author-time** — the hash
   still matches, so the OLD frozen contract is served. This is a genuine limitation. It is:
   - **Documented** here and in the manifest header (`frozen_at`), acknowledged, not papered over.
   - **Detected in CI**, which is the one place it matters: CI runs live (§3), so the live-registered
     contract and the manifest entry are BOTH present for the same node — if they disagree, emit a
     **`stale-manifest` warning** ("frozen contract for role X differs from the live contract;
     re-run `yaah freeze-contracts`"). A stale manifest thus fails `--strict` CI, which is where a
     re-freeze gets forced.
   - **Not** chased with a module content-hash / mtime check: a module's *file* mtime says nothing
     about whether the specific `contract()` fn changed (unrelated edits bump it; a vendored reinstall
     bumps it), so an mtime gate would cry wolf constantly and train authors to ignore it. Honest
     verdict: **module-logic staleness is not statically detectable from the manifest alone; live-CI
     disagreement is the detection mechanism.**

---

## 3. Live describe mode — it collapses to a thin wiring slice (say so)

**Claim: the ADR OVER-feared this. It does NOT need a describe-only boot mode or a query-over-the-bus
protocol for the case that has a stakeholder (same-language plugins).** Here is why, precisely.

A contract is a **pure `contract(cfg)`** — no node instance, no boot, no backend, no bus. So "ask the
node type for its contract, live" is just "call its registered pure fn." The bus/boot machinery the
ADR sketched is only required if a contract can ONLY be obtained by INSTANTIATING and BOOTING the node
(the `describe()`-method-on-a-live-instance model) — which is a heavier design we do NOT need, because
built-ins already prove the pure-fn model works.

So live mode = **the existing resolver seam + plugins loaded + a contract slot on the node registry.**
Concretely, the genuinely-new code is small and bounded:

- **(a) A contract slot on `Registry.register`.** Extend to
  `register(type_name, builder, *, contract=None, consumes=None)` and add
  `Registry.contract_for(ntype, cfg) -> Optional[Contract]` /
  `Registry.consumes_for(ntype, cfg, base_path) -> Optional[frozenset]`. Built-ins keep their
  `BUILTIN_CONTRACTS` dict (or are wired through the registry behaviour-preservingly — either is
  fine). This is the ONE real capability gap: today a custom node type literally cannot state a
  contract.
- **(b) Thread the seam.** `analyze_dataflow(... , *, contract_for=None, consumes_for=None)` →
  forward to the `resolve_contract` / `resolve_consumes` calls at `dataflow.py:158`/`:425`. Default
  `None` = today's behaviour byte-for-byte.
- **(c) Compose + invoke.** `yaah validate --live` (or the CI entry) runs `load_plugins`, builds a
  `contract_for` that chains `Registry.contract_for` (custom) with `builtin_contract_for`, passes it
  down.

**What is NOT needed and is a NON-GOAL (§6):** a describe-only boot mode; a `describe` reply over the
envelope bus; instantiating nodes to ask them. Those belong ONLY to a cross-language node used
in-process (a C node type inside a Python engine, queried live) — for which there is no port and no
stakeholder. For cross-language, the FROZEN MANIFEST is the bridge (the other language's freeze tool
produces the JSON; the Python lint reads it with no import) — which is exactly what §1 already gives.

**Net honesty:** live-describe is ~80% already-shipped (the resolver, the Contract type, the seam
plumbing, the never-raise discipline). The remaining ~20% is (a)+(b)+(c) above — a slot, a thread,
and a compose. **It is emphatically NOT the boot-mode/bus subsystem the ADR budgeted for.**

---

## 4. Precedence (updates D3/D10's resolver chains)

D3 originally sketched `registry > manifest > live > inline > opaque`. That conflated two things.
Re-pinned: there is **one injectable `contract_for` per run**, and the CALLER composes the sources
into it (the resolver stays a single-lookup-then-inline function). The composed order:

| # | Source | When present | Note |
|---|---|---|---|
| 1 | **in-process contract** — `builtin_contract_for` OR live-registered `Registry.contract_for` | builtins always; custom only when plugins loaded (`--live`/CI) | builtin & custom type-names are **disjoint** (registration forbids shadowing a builtin), so #1 is unambiguous |
| 2 | **frozen manifest** entry whose cfg-hash + type match | when `<pipeline>.contracts.json` present | the author-time bridge for custom types when NOT loaded |
| 3 | **inline `provides:`** (in `resolve_contract`) | cfg has `provides: [...]` | AUGMENTS a #1/#2 base; or stands alone as `preserve_declared` if none |
| 4 | **opaque** | nothing above | sound skip |

- **Composition lives in the closure the caller builds**, not in `resolve_contract`: the injected
  `contract_for` returns "#1 if in-process else #2-manifest else None"; `resolve_contract` then does
  its existing inline-augment / opaque logic (§D3) unchanged. So resolve_contract is untouched.
- **Manifest NEVER overrides a builtin.** Builtins are always live in-process and authoritative;
  #1 short-circuits before #2. (A freeze SHOULD skip built-in nodes when writing — they add nothing —
  but even if one leaks in, #1 wins.)
- **Conflict — live registration vs manifest (the case CI hits):** when plugins ARE loaded AND a
  manifest is present, **#1 (live) WINS** — it is authoritative / zero-drift; the manifest is a
  possibly-stale snapshot. If the two DISAGREE for the same node, emit the **`stale-manifest`**
  warning (§2). This is deliberately the detection mechanism for module-logic staleness, so the
  conflict isn't silently swallowed — it's the signal.

**Consumes precedence mirrors this with D10's asymmetry preserved:** live-registered `consumes` >
manifest `consumes` > (custom-only) inline `consumes` > empty. Inline `consumes` STILL never augments
a source that resolved from #1/#2 — a built-in/frozen reader's read-set is exact and complete, so a
stray inline `consumes` is ignored, not merged (the §D10 false-hard-error class stays designed out).
The manifest's frozen `consumes` counts as such a resolved reader.

---

## 5. Non-goals, migration slices, acceptance tests

### Non-goals (v1)

- **No conditional-rules DSL manifest** (tier b) — deferred; hash-keyed per-instance freeze is v1.
- **No describe-only boot mode / bus `describe` protocol** — only cross-language-over-bus needs it;
  no stakeholder (§3). Cross-language is served by the frozen manifest, not a live bus query.
- **No auto-freeze-on-save** and no author-time detection of module-logic staleness beyond the
  cfg-hash miss — that staleness is CI-detected only (§2).
- **The manifest is not truth and not a speed cache** — live is truth; the manifest is the
  no-import author-time bridge. It must be safe to delete (→ falls back to opaque).

### Migration slices (each keeps the suite green; own acceptance)

**v1 (present stakeholder = embedding apps) — the LIVE path:**

1. **D7.1 — registry contract slot.** `Registry.register(..., contract=, consumes=)` +
   `Registry.contract_for` / `consumes_for`. No lint change. *Acceptance:* a registered custom
   contract is returned by `contract_for`; suite green.
2. **D7.2 — thread the seam.** `analyze_dataflow(..., contract_for=, consumes_for=)` forwarded to the
   two resolver calls; default `None` unchanged. An embedding app (or a future `--live` CLI flag)
   composes `Registry.contract_for` chained with `builtin_contract_for` and passes it down.
   *Acceptance:* injecting a custom `contract_for` makes a custom node ERROR-checkable (AT1 via the
   live source instead of a manifest); default byte-identical; suite green.

**Deferred (no present stakeholder — spike, don't ship, until cross-language / untrusted-config is real):**

3. **D7.3 — manifest read.** Define the JSON shape (§1) + `load_contract_manifest(path) ->
   (contract_for, consumes_for)` hash-lookup closures; validate/lint auto-discover the convention
   path and compose per §4. *Acceptance:* AT2, AT3, AT5, AT6 below.
4. **D7.4 — freeze verb.** `yaah freeze-contracts <root>` (loads plugins/registry, resolves, writes
   the manifest). *Acceptance:* freeze then no-import validate ERROR-checks the custom node; a cfg
   edit + re-freeze updates the hash/entry.
5. **D7.5 — live-vs-manifest conflict.** When BOTH a manifest and a live registry are present, live
   wins and a disagreement emits `stale-manifest`. *Acceptance:* AT4. (Only meaningful once both 3–4
   and the live path exist.)

### Acceptance tests (written to FALSIFY, per house rule)

| AT | Scenario | Expected (the falsifier) |
|---|---|---|
| **AT1** headline | custom type `my_thing` whose contract (via a **live-registered fn** in v1, or a frozen manifest in the deferred tier) is `reset({a,b}, closed=True)`, downstream `render "{{missing}}"` | **validate/lint RAISES (ERROR)** — where today (inline-provides at best / opaque) it's a WARNING or a silent skip. Proves the portable binding gives ERROR-grade reach, not advisory. The v1 live-path form is the one that must pass first. |
| **AT2** stale (documented) | freeze; then EDIT the node's cfg so the contract WOULD change; validate WITHOUT re-freeze | hash MISS → resolver falls to **opaque** → **no false ERROR AND the old contract is NOT enforced**. Asserts "behaves as documented," not silently-wrong. |
| **AT3** no import | validate with a manifest present, plugin NOT in `plugins:` | plugin module is **absent from `sys.modules`** (spy) — D6-4 "default validate imports no app code" preserved. |
| **AT4** live authority + conflict | plugins loaded AND a deliberately-stale manifest on disk | **live contract wins**; a **`stale-manifest` warning** is emitted (fails `--strict`). |
| **AT5** config-dependence | freeze a pipeline with `worktree op:add` and another with `op:remove` | the two nodes hash differently; each entry carries its DISTINCT contract; `{{removed}}` is OK after remove, **ERROR after add**. Falsifies the forbidden `type→keys` collapse. |
| **AT6** consumes asymmetry | (a) custom node, frozen `consumes:["x"]`, inbound lacks `x`; (b) a render with a manifest entry + a stray inline `consumes` | (a) **checked** (ERROR/WARNING per flow); (b) inline `consumes` **still ignored** (D10 asymmetry holds through the manifest path — no false hard-error). |

---

## 6. Judged trade-offs, kept limitations (documented, not hidden)

- **Manifest is pipeline-coupled** (per-instance, not per-plugin). A plugin used in 10 pipelines needs
  10 frozen entries (one freeze per pipeline). Accepted: the freeze is a cheap CI step, and the
  alternative (tier-b rules) buys reuse at the price of a per-port DSL interpreter — a bad trade with
  no port to serve. If a genuine pipeline-independent need appears (a plugin distributed WITHOUT the
  app that freezes it, linted standalone), tier-b becomes a real future slice — not before.
- **Whole-cfg hashing over-invalidates** (irrelevant edits force a re-freeze). Accepted as the sound
  side of the trade; a salient-keys hash is a possible later refinement but risks under-invalidation.
- **Module-logic staleness is not author-time-detectable** (§2) — CI-detected via live disagreement.
  A `freeze-contracts` run in CI (or a pre-commit hook) keeps it fresh; a stale manifest degrades to
  opaque-or-warning, never to a wrong hard-error.
- **`freeze-contracts` must actually be run.** If nobody runs it, the manifest is absent (→ opaque,
  today's behaviour — no regression) or stale (→ CI `stale-manifest` warning). The design fails SAFE
  in both directions; it never manufactures a false ERROR from a missing/stale manifest. This
  decay-if-unrun risk is a further reason the manifest is DEFERRED behind the zero-ceremony live path.
- **A contract that needs runtime introspection is a second, genuine manifest niche** (haiku #4).
  §D2 requires `contract(cfg)` to be PURE (no I/O). A plugin whose real key-set is only knowable by
  runtime introspection (infer keys from a live DB schema, an async handshake) CANNOT honestly
  express itself as a pure fn — so its live contract is either unavailable or a lie. Freezing
  captures the introspected result ONCE, at a moment the resources ARE available. This is a real
  future justification for the manifest independent of cross-language — still no present stakeholder,
  still deferred, but recorded so the manifest isn't mistaken for pure over-engineering.

---

## 7. Review record

### Haiku counter-argument (design, 2026-07-07)

A fast contrarian attacked the five pins. Its central, repeated thrust — "**skip the manifest; a
custom node should register a pure `contract` fn, live forever; the freeze CLI is new ceremony that
will decay**" — was the strongest signal, and verification against the code (see §0.1) turned it from
a hypothesis into the reason the manifest tier is now DEFERRED and the live path is v1. Advice is a
hypothesis verified against the seams, not accepted as truth; verdicts below.

| # | Haiku's push | Verdict | Reason (verified) |
|---|---|---|---|
| 1 | Hash-keyed freeze gives the author NO SIGNAL why a node went unchecked; manifests calcify. | **ACCEPTED (design changed).** | Real. A silent opaque-fallback after a cfg edit is poor UX. Combined with §0.1 this is why the manifest is deferred, not v1. If ever built, D7 MUST surface a "node X unchecked: manifest miss — re-run freeze-contracts" NOTE, not a silent skip. |
| 1/3 | Just register a pure `contract` fn on the plugin — no manifest, no CLI, drift-free. | **ACCEPTED for the live path; REJECTED as a full manifest substitute.** | Right for the EMBEDDING world (§0.1) — now the v1 pin. But it requires IMPORTING the plugin, so it cannot serve the manifest's one unique job (no-import / cross-language). Haiku conflated the two; the design keeps both, live-first. |
| 2 | Don't freeze at all — call `contract_for()` every lint run; it's a cheap pure fn. | **ACCEPTED where reachable.** | This IS the live path, promoted to v1. Only fails when the fn isn't importable at lint time (untrusted/cross-language) — the deferred manifest's niche. |
| 2 | Whole-cfg hash over-invalidates on cosmetic edits. | **ACKNOWLEDGED, kept.** | True cost, but the SOUND side of the trade (over-invalidation → re-freeze; under-invalidation → silently-wrong). Applies only to the deferred tier anyway. |
| 4 | "Pure fn, no boot" is false if the plugin needs init (DB schema inference, async). | **ACCEPTED — added as a 2nd manifest justification (§6).** | Correct and sharp: an introspection-dependent contract can't be a pure fn; freezing captures it once. Recorded; still no present stakeholder. |
| 4 | Say "Python-in-process only, cross-language deferred" explicitly; "non-goal" is doing too much work. | **ACCEPTED.** | Made explicit in §3 and §0.1: v1 live path is same-language/in-process; cross-language is the manifest's (deferred) domain. |
| 5 | Precedence is racy / "inline provides augments" is undefined / ERROR if both sources present. | **PARTIALLY REJECTED.** | "augments" is NOT undefined — §D3 pins it as set-union (`base ∪ inline`); haiku didn't read D3. "ERROR if both" is wrong: a live registry coexisting with a not-yet-refrozen manifest is the NORMAL CI state, not an error — erroring would break every CI run pre-refreeze. The `stale-manifest` WARNING (not error) on disagreement is the right signal, and only fires in the deferred both-present case. |
| — | Verdict: ship inline `provides:` + opaque as v1, manifest is not worth it. | **CONVERGES with the corrected design.** | Inline `provides:` + opaque already SHIPPED (§D3). The genuine v1 ADD is the LIVE registry path (embedding apps, the real stakeholder) — small (D7.1–D7.2). The manifest is deferred exactly as haiku argues. Net: haiku moved the pin; the two-brain gate earned its keep. |

**One place haiku was wrong and it matters:** its "register a fn, done" assumes a CLI registration
path for node types EXISTS. §0.1 shows it does NOT — node types are embedding-only today. So even the
live path needs D7.1 (a contract slot) + D7.2 (thread the seam); it is not literally already-shipped.
Haiku under-counted that ~20%.
