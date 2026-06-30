# ADR-0005 — requires↔provides: a statically-lintable data-flow contract

Status: ACCEPTED — slices A, B, D implemented (`07b5adf` + this change); slice C (obligations)
DEFERRED until it has runtime teeth (see Build slices).
Source: [ADR-0004 §Extension](0004-parse-by-default.md);
[`.notes/silent-dataflow-class-2026-06-29.md`](../../.notes/silent-dataflow-class-2026-06-29.md);
[`.notes/seam-fix-plan.md`](../../.notes/seam-fix-plan.md); [`.notes/1b-design.md`](../../.notes/1b-design.md).
Builds on the 1a lints shipped in `e4ee358` / `0e215d4`.

## Context — the silent-dataflow failure class

yaah's deepest footgun is a single class (Y1/Y4/Y5 were three faces of it): **the
node-to-node data-flow contract is implicit and undefended.** Each node REPLACES the
payload (`reply_with(payload)` builds a fresh dict; inbound keys survive only if the node
re-includes them), so a key a downstream node needs can simply not be there — and the
failure is SILENT (a plausible wrong value flows, the router reasons around it) or
MISLABELED + LATE (surfaces far from its cause). The forward audit
(`.notes/silent-dataflow-class-2026-06-29.md`) found N1–N11; the ones this ADR targets:

- **N4** — an envelope-transform spreads an unchecked dict with NO output contract; a typo'd
  key (`decison`) spreads fine and a downstream `branch on: decision` silently misroutes.
- **N3** — an agent drops inbound keys; a later `cwd_from`/render reads a key that's gone.
- The render-unfilled footgun (the project's "worst fault class").

The 1a lints (shipped) catch the SINGLE-HOP case: a render/branch and its immediate
predecessor agent. They cannot see across a transform chain, because a transform's output
keys are not declared anywhere — the engine can't know what an envelope-transform provides.

## Decision

Make the data-flow contract STATICALLY LINTABLE across the whole graph, via a
**requires↔provides** model with two declaration sources and a graph-dataflow join.

### 1. provides — what a node puts on the payload

Most node provides are already known from config/engine semantics (the transfer functions
below). The ONE opaque case is an envelope-transform (its fn returns an arbitrary dict). So:

- **New config key `provides: [str]`** on a node — declares the payload keys it guarantees.
  Required to lint across an envelope-transform; optional elsewhere (an explicit override).
- A node's effective provides (the per-type transfer function, verified in code):

  | node | provides_out(provides_in) |
  |---|---|
  | agent parse:true | {raw} ∪ output_schema(props∪required) ∪ carry ∪ cwd_from ∪ sticky — drops inbound |
  | agent parse:false | {raw} ∪ carry ∪ cwd_from ∪ sticky — drops inbound (carry_cwd forwards cwd_from regardless of parse, agent.py:342) |
  | transform call:"args" | provides_in ∪ {into} (into default "result"; preserves inbound) |
  | transform call:"envelope" | declared `provides` ⇒ provides_in ∪ declared (**PRESERVE + ADD** — the dominant fn returns `{**payload, ...}`, so `provides` lists the ADDED keys and inbound is kept; a true-reset fn is then over-stated, a safe false-negative). UNDECLARED ⇒ TAINT |
  | render | provides_in ∪ {output, path} |
  | human_gate | provides_in ∪ {decision} |
  | get / post | provides_in ∪ {into} |
  | validators (json_object/json_schema/expect_field) | provides_in (pass-through) |
  | graph `sticky: [keys]` | re-applied after EVERY stage — always present |

### 2. requires — what a node reads off the payload

Per consumer, statically known:
- render → the `{{key}}` placeholders in its template.
- stage `branch.on` → that one key.
- (later) `expect_field`'s key, `concerns_from`, `fanin.expect`.

### 3. The join — graph dataflow, branch/loop-aware

Forward dataflow over the stage graph:
- `provides_in(stage)` = **INTERSECTION** over all predecessor paths of `provides_out(pred)`,
  then ∪ sticky. Intersection (not union) is the sound choice: a render placeholder is
  UNCONDITIONAL (mustache has no `{{x || y}}`), so a key counts as provided only if EVERY
  path to the consumer provides it. Union would call a key "provided" on the strength of one
  branch and miss the crash on the other — false negatives that defeat the lint's purpose.
- Branch/fork → multiple predecessors → intersection at the merge.
- **Loops** → least-fixpoint: iterate provides_in to convergence; a back-edge contributes its
  current provides_out (monotone — sets only grow per iteration until fixed). Conservative
  seed (∅ for unreached) keeps it sound for "key first set inside the loop, read before the
  body could run."
- **TAINT** — if any predecessor path passes through an UNDECLARED envelope-transform,
  provides_in is UNKNOWN ⇒ SKIP that consumer (no false warning) AND warn on the transform
  itself (see Output, below) so the non-coverage is never silent.

### 4. obligation — the defensible tier beyond shape

Shape (a key is present, of type T) is what `check_schema`/mypy already cover. The tier no
type system expresses is OBLIGATION — flow-sensitive cross-node facts:
- `NonEmpty` — present AND not None/""/[] (defends N7/N8/N9: present-but-empty reads as a
  confident wrong value).
- `OnlyOnBranch(route)` — a key that exists only on a specific branch outcome; reading it on
  another path is the bug.
- `BeforeLoopGuard` — must exist before a loop's branch guard reads it.

Obligations are DECLARED, never inferred (a bare signature gives shape, not obligation). They
ride as annotations on the same `provides` declaration (e.g. `{"key": "verdict", "nonempty":
true}` in config, or `Annotated[str, NonEmpty]` in a decorated transform). The join carries
each key's obligation set along the path and checks the consumer's required obligations are
satisfied on every path.

### 5. Two sources, one contract format — extract where there's code, declare where there's config

- An **agent** is config (prompt + authored `output_schema`) → its contract stays authored.
- A **transform** IS typed Python → a `@provides(...)` decorator registers its added-keys next
  to the code, read at lint time (opt-in `--from-code`) as the same `provides` the engine
  consumes — one declaration instead of two. SHIPPED shape-only (keys); obligations would be
  the decorator's extra args once C lands. NOTE: the keys are hand-maintained, NOT introspected
  from the `return` dict, so the decorator can still drift (over-/under-declare) exactly like an
  `output_schema` — it removes the config/code DUPLICATION, not the honest-contract caveat (§6).
- **The engine stays domain-free** (ADR-0001): the decorator + extractor are an authoring
  TOOL that introspects app components and emits JSON; `src/yaah/` only ever CONSUMES a
  `provides` JSON declaration and runs the join. Removing the tool leaves a working engine
  that reads config-declared `provides`. Config-declared is the substrate; the decorator
  AUTO-FILLS it.

### 6. Output — every finding is ACTIONABLE (human OR llm)

A lint that says "wrong" without "here's the fix" is half a tool. Every 1b warning MUST name:
(a) the exact stage/node + key, (b) WHY it's unsound (depends on undeclared output / missing
obligation), (c) the CONCRETE repair as an imperative an author or an LLM can apply directly —
e.g. *"declare `summary` in stage 'review' agent's output_schema (typed), or add it to the
`provides` of transform 'parse', or carry/sticky it."* Honest framing (the 1a lesson):
`check_schema` doesn't enforce additionalProperties, so an undeclared key MAY be emitted and a
run MAY pass — the warning flags a CONTRACT gap (undeclared dependency), it does not predict a
certain crash. Advisory; `--strict` makes it CI-enforceable. The linter never raises.

## Build slices (each sound + committable; opus review gates the design before C/D)

- **A. substrate** ✅ SHIPPED `07b5adf` — `provides: [str]` config key + validation (list of
  non-empty strings; only meaningful on a node that drops/spreads). Foothold for everything else.
- **B. shape join** ✅ SHIPPED `07b5adf` — forward dataflow (intersection, taint+companion-warning,
  sticky, loop fixpoint) over render/branch consumers, actionable output. ABSORBED the 1a
  single-hop functions (one lint, no double-warnings).
- **D. extraction tool** ✅ SHIPPED — `src/yaah/contract.py`: a `@provides(*keys)` decorator
  (records the ADDED keys on the fn — hand-maintained, NOT introspected from the `return`, so
  the same drift caveat as `output_schema`) + `fn_provides_resolver` (imports an `fn:` target,
  reads the decorator). OPT-IN via `yaah validate --from-code`, dependency-injected into the
  lint so the default stays pure and the engine domain-free (remove the module → engine still
  reads config-declared `provides`). SHAPE ONLY — obligations wait for C. Proven end-to-end:
  decorating arch-drift's 5 transforms takes its `--from-code` lint from 1 warning to 0 (all 3
  consumers — the `changed` branch, `report.html` render, `decision` gate — now actually checked).
- **C. obligation** — DEFERRED (no runtime teeth yet: `check_schema` enforces
  type/enum/required/properties/items, NOT non-empty, so a `NonEmpty` obligation would be
  declared on both sides and enforced on neither — declaration-vs-declaration). Build only when
  a real NonEmpty need appears, AND with runtime enforcement first. `NonEmpty` is the most
  grounded (N7/N8/N9); `OnlyOnBranch` / `BeforeLoopGuard` the flow-sensitive tier.

## Consequences

### Positive
- Closes the N4/N3 silent-misroute and render-unfilled classes at AUTHOR time, graph-wide.
- The contract is the substrate for the AI-control north-star: an AI composing the DATA
  topology gets its wiring proven sound before a run, while components stay typed Python.
- Domain-free engine preserved; the tool is replaceable (ADR-0001).

### Negative / accepted tradeoffs
- A declared `provides`/`@provides`/`output_schema` is HAND-MAINTAINED and can drift from the
  fn's actual `return` in BOTH directions (the decorator does NOT close this — it records the
  author's declared keys, it doesn't read the function body):
  - OVER-declare (a key the fn may not emit) → a downstream check passes that shouldn't → false
    NEGATIVE. The dominant/accepted case (same trust model as `output_schema`).
  - UNDER-declare (the fn emits `{**payload, "y": …}` but `provides` omits `y`) → a downstream
    `{{y}}` is flagged though `y` IS present → false POSITIVE. Rare (authors list what they add)
    and the warning's advice ("declare `y`") is still correct, but it IS a false positive by the
    zero-FP goal. Mitigation if it bites: a test/extractor that cross-checks decorator keys
    against statically-visible `return {...}` literals. Not built (no signal it bites yet).
- Surface grows (`provides` key, obligation annotations). Justified by the failure class it
  defends; optional where provides are already known.
- The join is the real work and is easy to get subtly wrong (cf. the 1a "zero false positives"
  overclaim that the opus eval caught). Hence: full opus review before C/D; intersection +
  taint chosen for soundness over reach.

### What was rejected
- **Union join** (haiku opposer's recommendation) — unsound for unconditional consumers
  (false negatives that defeat the lint). Verified by counterexample; intersection kept.
- **Decorator-only (no config substrate)** — would couple the engine to Python introspection;
  the engine must consume plain JSON (ADR-0001). Config is the substrate; decorator fills it.

## Cross-references
- [ADR-0004](0004-parse-by-default.md) — parse-by-default; `output_schema` as the agent's
  contract foothold this ADR generalizes.
- [ADR-0001](0001-three-concepts.md) — Envelope/Node/Comms; the domain-free boundary the
  extraction tool must respect.
- [`.notes/silent-dataflow-class-2026-06-29.md`](../../.notes/silent-dataflow-class-2026-06-29.md)
  — the N1–N11 audit motivating this.
