# YAAH whole-project assessment — 2026-06-25

Milestone review after the Phase 1b merge (PR #4) + polish lane (PR #3).
Method: 7 independent cluster reviewers (read-only) over the engine, examples,
and docs, judged on the four axes (elegant / simple / working / extensible)
plus bugs and security. Ground truth established first; every CRITICAL/HIGH
reproduced before inclusion. This file is a point-in-time snapshot and the
input to the autonomous fix-batch loop — pick fixes from the prioritized table
without re-deriving context.

## Verdict

**Healthy and shippable.** 0 CRITICAL, **1 HIGH** (confirmed by reproduction),
~5 MED, ~16 LOW. The load-bearing invariants all hold: the three-concept
cosmology, the domain-free engine, agent isolation, the implicit trust boundary
(every `call_target`/`importlib`/binary target is config-derived, never
payload-derived — confirmed independently by clusters 1, 3, 4, 5), and the
coding-agent security hardening (every escape vector reproduced and refused).

The defects cluster into three themes: (1) one real **fail-open in
`overlay_lint.py`** — the AI-overlay safety gate; (2) **doc drift from the
Phase 1b merge** — the `agent_loop` node shipped but is undocumented in the two
files an author reads first; (3) low-severity concurrency/edge-case hardening
and example hygiene.

**Status (updated):** the HIGH and the doc-drift MEDs are resolved in PR #5 — see
*Resolved in PR #5* below; the two latent-engine MEDs and the LOW list remain open.

## Ground truth

- `python3 scripts/run_tests.py` → **PASS=75, FAIL=0, coverage 89%** (re-run
  this session after the AI-friendliness fixes; suite CI-safe with no nats).
- `scripts/review_my_pr.py` on the working tree → deterministic subset PASS.

## Ratings by cluster

| Cluster | Elegant | Simple | Working | Extensible | Headline |
|---|---|---|---|---|---|
| 1. Harness engine (`core/`, `harness/`) | 5 | 4 | 4 | 5 | Clean run-stage seam; latent fork-clears ordering + concern-aliasing hazards. |
| 2. Comms / store (`comms/`, `store/`, adapters) | 4 | 5 | 4 | 5 | Strong distributed-systems care; NATS unsubscribe fire-and-forget; idempotency guards caching not execution (documented Phase A). |
| 3. Agents / backends (`agents/`, `adapters/backends/`) | 5 | 4 | 5 | 5 | Streaming protocol clean; trust boundary tested; claude_cli `stream()` cost-bridge gap (latent). |
| 4. Nodes / build / runtime (`nodes/`, `build/`, `runtime*`) | 5 | 4 | 5 | 5 | Trust boundary defended end-to-end; dead import; agent_loop justified as model-driven-iteration primitive. |
| 5. Ports & stdlib (`data/`,`prompts/`,`mcp/`,`trace/`,`validate*`,`recall`,`jsonio`,`safepath`) | 5 | 4 | 4 | 4 | safe_join/extract_json verified; **overlay_lint fail-open (HIGH)**; triad asymmetric. |
| 6. Examples (`examples/`) | 4 | 4 | 5 | 5 | All five archetypes run offline; hardening holds; doc drift + one unconfined PoC tool. |
| 7. Docs (`AGENTS.md`, `docs/`, skills) | accurate 3 | complete 3 | non-redundant 5 | navigable 5 | `agent_loop` undocumented; `render: out_key:` fabricated; catalog generator glitch. |

## Resolved in PR #5

- **HIGH** `overlay_lint.py` fail-open — deny-by-default for a numeric bound
  absent from base (+ regression test).
- `agent_loop` documented (`node-reference.md` section + `shape-grammar.md` row).
- `shape-grammar.md` render keys (`out_key`→`out`, `template`→`template_text`),
  added `completion` verb, fixed `human_gate` `decision_schema` wording.
- `arch-drift/README.md` nonexistent `parse` stage removed.
- `cli.py:296` stale `test_completion.py` comment.

## Open findings

### MED — latent engine

5. **`harness/fork_coordinator.py`: branch-level `clears` not ordered vs fan-in
   completion** (latent). A fast sibling can meet the join policy and reduce
   before a slow branch publishes its `clears`, delivering to a torn-down
   subscription. Document the ordering, or gate the fan-in flush on branch
   settlement (the `_release_unmeetable_joins` machinery already tracks done).
6. **`harness/harness.py`: soft-concern aliasing** — `ctx.concerns is
   baton.concerns`; a future `baton.concerns.extend()` after a fork would
   double-count. Pass `concerns=list(baton.concerns)` and re-extend explicitly,
   symmetric with the linear path.

### LOW — batchable hygiene / hardening

7. `examples/spike-harness/tools.py` — unconfined `read_file` (`../../etc/passwd`
   escapes). Add confinement or a prominent README note pointing at
   `coding-agent/tools.py` as the safe pattern.
8. `examples/coding-agent/README.md` — stale "run shell commands" (tools were
   de-shelled to fixed-argv `run_tests`).
9. `examples/config-flow/transforms.py` — dead `parse_extracted` / `noop_done`.
10. `runtime.py:58` — dead `StageFailed` import.
11. `build/builders.py` — `agent_loop` build errors don't name the node
    (`spec.get("_role", "?")`), unlike `_wrap_node`.
12. `adapters/backends/claude_cli_backend.py` — `stream()` never calls
    `on_usage` (cost capture silently zero once a consumer streams claude;
    latent — claude currently routes through `complete()`).
13. `claude_cli_backend.py` — `complete()` lacks the stdin/stdout None-guard
    `stream()` has; a dead process raises a bare error (error-voice).
14. `adapters/backends/litellm_backend.py` — partial/odd tool-call `arguments`
    degrade to `{}` silently; assert on non-empty-args round-trip when a real
    key lands.
15. `adapters/transports/nats_comms.py` — `_NatsSubscription.cancel()` is
    fire-and-forget/unobserved; note that `drain()`/`close()` is authoritative.
16. `store/idempotency.py` — execution race (guards caching, not execution);
    documented Phase A, fix with `cas(expected=None)` before invoke in Phase B.
17. `store/adapters/file_store.py` — add an invariant comment that keys must
    stay funneled through `_path()` (`quote(key, safe="")`) so a future
    sharded layout can't reintroduce traversal from headers.
18. `trace/*` — `record.get("corr") or ""` buckets uncorrelated spans together;
    add an assertion at the emit boundary if uncorrelated spans should never
    occur (impact is mislabeling, not loss).
19. `overlay_lint.py:136` — a new *numeric* `config` key is rejected with
    "non-numeric config change" — misnames the cause. Distinguish absent-base
    numeric from genuinely non-numeric.
20. Ports triad asymmetry — only `prompts` ships an `http_*` adapter; `data`/`mcp`
    docstrings advertise remote sources that don't exist. Ship them or trim the
    docstrings.
21. `docs/shape-grammar.md` — missing `completion` CLI verb; `human_gate`
    `decision_schema` framing implies it's valid without `form: "json_schema"`.
22. `docs/module-catalog.md` — `_build_agent` "Constructs" column shows `s`
    (generator glitch in `scripts/build_catalog.py`, not a hand-edit).

## Security notes (by design — document, don't "fix")

- `external_call.py` `http:` tool dispatch: the URL is trusted config, but the
  POST body is **model-chosen** when fired from the agent tool-loop. The `# nosec`
  justifies the URL only. An `http:` tool hands the model a request-forgery
  primitive against whatever that endpoint trusts — name this at the seam so
  authors don't expose an internal endpoint unaware.
- `safepath.py` `allow_absolute=True` default: absolute keys bypass containment
  by design; no current caller passes `allow_absolute=False`, so the
  "HTTP-exposed sink" protection is unexercised — a future HTTP-exposed adapter
  author must remember to set it.
- `clear_bus.py` cross-run scopes (bare node / `*`) correctly document that
  broker-level ACL is the real boundary in distributed deployments — the model
  for how an implicit trust boundary should be disclosed.

## Philosophy preservation

Three concepts intact (Envelope / Node / Comms); `agent_loop` is the one new
node and is justified (model-driven iteration is not expressible by author-static
`fork`/`fanin`/`transform`). Engine ships zero attachers (ADR-0003). Domain-free
engine holds (banlist clean). Error-voice is strong at the config boundary
(validators name the fix + cite examples) with a few internal raises noted above.

## Reproduce-ground-truth appendix

- **overlay_lint HIGH:** base `{"nodes":{"bare":{"type":"agent","model":"haiku"}}}`
  + AI overlay `{"_authored_by":"ai","_extends":"base.json","nodes":{"bare":
  {"timeout":99999,"retries":50}}}` → `lint_overlay()` returns `[]`. A `writer`
  node with `timeout:30` in base correctly rejects the same overlay value.
- **coding-agent hardening:** absolute (`/etc/passwd`), relative (`../`),
  symlink-out, and shell-injection (`x.py; rm -rf /`) all return
  "outside the working directory" / execute nothing; unset workdir refuses.
- **Suite:** `python3 scripts/run_tests.py` → PASS=75 FAIL=0, 89%.
