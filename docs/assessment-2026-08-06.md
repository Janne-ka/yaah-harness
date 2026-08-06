# Assessment — 2026-08-06 (elegance + code review of the v0.3.0 delta)

Scope: the two yaah commits `d8b217d..HEAD` (7cdf68f engine seams, 025a29c fork
salvage + cost metric) and the consuming app's batch commit (s_factory
`yaah_app`, M33–M37). Method: four parallel cold reviewers (harness engine ·
build/runtime · providers/trace · app), four axes each with elegance/simplicity
weighted per the operator's request; both HIGHs independently re-confirmed
against the code by the orchestrator before this synthesis. Ground truth
established before fan-out: engine suite PASS=130 FAIL=0; app `run-checks.sh`
`== OK ==` twice (2026-08-06).

## Verdict

The delta's *architecture* is sound — every new seam (lease, wiring
fingerprint, first-stage checkpoint, macros, fork_partial, cost split,
worktree recovery) has a defensible single home, the enforced invariants all
survive (domain-free engine verified by grep; agent isolation verified against
the graph; no new `or "."` anywhere), and the incident-driven fixes close the
failures they name. The two systemic weaknesses are (1) **contract shapes
restated instead of shared** — the usage payload spelled in four places, the
CAS tier probed three ways, seven truncation policies, one 300-char JSON
string copied 30 times — which is exactly the drift class the codebase's own
derived-signature work (task_request, node_keys) exists to kill; and (2)
**prose outgrowing code** — 58–70% of the harness delta is comments/docstrings,
much of it re-arguing decisions that belong in DECISIONS/durable-state with
pointers. Two HIGH bugs shipped: `{{run_dir}}` placeholders are silently
corrupted by macro expansion, and the recovery lease is never released on the
failure path.

## Ratings

| Cluster | Elegant | Simple | Working | Extensible |
|---|---|---|---|---|
| Harness engine (lease/fingerprint/checkpoint/fork) | 4 | 3 | 3.5 | 3 |
| Build/runtime ({run_dir}, node_keys, lints) | 3.5 | 3 | 3 | 4 |
| Providers/trace (cost split, retry diagnostics) | 3 | 3 | 3 | 4 |
| App (s_factory yaah_app, M33–M37) | 3.5 | 2 | 3.5 | 4.5 |

## Prioritized fix list

Severity · site · defect · fix. "CONFIRMED" = reproduced or re-verified by a
second reader.

| # | Sev | Site | Defect | Fix |
|---|---|---|---|---|
| 1 | HIGH · CONFIRMED | `build/macros.py:74` | `str.replace` fires inside `{{run_dir}}`/`{{base_dir}}` — a runtime interpolation placeholder is corrupted to `{/abs/path}` at build time; the collision test only tries non-colliding names; silent wrong output at exit 0 | Replace with per-token `re.compile(r"(?<!\{)\{run_dir\}(?!\})")`; add `{{run_dir}}`/`{{base_dir}}` to `scenario_no_collision_with_runtime_interpolation` |
| 2 | HIGH · CONFIRMED | `harness/harness.py:477-487` | Recovery lease claimed, never released when `_settle` raises — a long-lived embedded harness then refuses its own retry as LIVE (its own pid); `--force` text becomes false | `LeaseState.of` gains a self-owner tier (a lease held by me never refuses me), and/or try/except around `_settle` nulling owner/leased_at before re-raise |
| 3 | MED | `harness/harness.py:524` | `--allow-rewiring` (or pre-upgrade `wiring None`) into a deleted stage → bare `KeyError` at `:854`, AFTER the CAS claim committed (compounds #2) | Hoist a `_require_stage_exists` check ahead of the short-circuit; call from both `_check_wiring` and `_check_gate_wiring` |
| 4 | MED | `harness/harness.py:337-400` | Gate `resume()` never got the claim — two concurrent resumes both pass the suspended check and double-drive the post-gate stage | Same CAS pattern as `resume_running`: `load_rev` → flip status → `claim` → refuse on loss |
| 5 | MED | `adapters/providers/litellm_provider.py:119` | Negative clamp `max(prompt-cache, 0)` converts a cache-EXCLUSIVE dialect (litellm shim flip) into silent $0 fresh-input billing; test enshrines it | `rest = prompt - cache_read - cache_write; tokens_in = rest if rest >= 0 else prompt`; reframe the test |
| 6 | MED | `trace/pretty.py:196-202, 268-274` | `yaah trace --cost`/`--counts` never updated for the split — under-report tokens ~100x on cache-heavy runs, no cache column anywhere | Add cache column to `counts_table`, `(+Nk cached)` segment to `cost_summary`; data already in the rows |
| 7 | MED | `trace/contributors/cost.py:31-37` + `aggregate.py:46-51` | Pre-split record and no-cache record byte-identical — aggregates mix exact and upper-bound costs with no marker | Always emit both cache fields on `model_call`; absence then unambiguously = pre-split; count `unpriced_upper_bound_calls` in totals |
| 8 | MED | `trace/contributors/phase.py:79-80` | `error` projection writes payload VALUES (validator messages embed live payload) into trace JSONL + wire envelopes — breaks the keys-only contract stated at `:48`/`:59` of the same file | Project failure `code`s by default; gate free-text message behind explicit opt-in; amend the invariant docs either way |
| 9 | MED | `adapters/trace/langfuse_trace_sink.py:60-63, :124` | Cache split untested on both branches; v2 path sends anthropic cache keys into a typed `usage=` model that may reject them | Add cache-bearing record to sink scenarios; split `_usage_details` v4/v2 if v2 refuses |
| 10 | MED · CONFIRMED | app `factory_status.py:263-266` | `IndexError` on a failure with code but empty/no message (`"".splitlines()[0]`) — degrades exactly the fault-park row M35 built | `((f.get("message") or "").splitlines() or [""])[0]`; `str()` coerce; add the missing `_escalation_line` test |
| 11 | MED · CONFIRMED | app `transforms.py:64-118` | `normalize_where` promotes prose identifiers (`user.role`, `Task.update_status`) to tier `file` and REWRITES `where` — inflates ab_recall while the understatement note never fires; absolute paths score unlocatable | Require a dir separator or `:line` for file tiers, bare dotted tokens stay `prose`; drop `/` from the lookbehind; update the test pinning the over-eager direction |
| 12 | MED | app `preflight.py:1056-1063` | `_load_task_request` returns `{}` on inline-dict `input` → M34 template check silently no-ops in `factory doctor`; twin `run_root_input:540` handles it correctly | Delete `_load_task_request`; call `run_root_input` (fix = the dedup) |
| 13 | MED | `node_contract.py:259` via `templating.py:44-48` | Consumes-lint reads the unexpanded spec; a macro'd `template_file` opens `"{base_dir}/…"` → OSError → `None` → "reads nothing checkable", silently | Expand `{base_dir}` at the lint seam (it IS base_path there); surface an unresolvable `{run_dir}` as a concern, not `frozenset()` |
| 14 | LOW-MED | app `transforms.py:103-104` | Idempotence guard keys on model-writable `where_tier` — a model echoing a false tier suppresses normalization; same class the delta fixed for `lens` | Namespaced private marker or re-derive unconditionally (pure + cheap) |
| 15 | LOW-MED | app `transforms.py:4480+` | `salvage_worktree` still reads `p.get("task") or "task"` — the `task_key` seam stops at the pre-check; surviving instance of the exact `or "<literal>"` class M35 removed | Thread `task_key` through salvage; fix `provides` hardcoding `"task"` |
| 16 | LOW | `harness/lease_state.py:141-147` | Owner is per-PROCESS not per-run — a dead task in a live embedded process reads LIVE forever; not in the documented non-proof list | Document in the v1 non-proofs, or stamp the driving-task identity |
| 17 | LOW | `harness/fork_coordinator.py:52-60, :280` | `_expect_count` defaults absent `count` to 1 — a width-1 fork misreports `expected` for `{"any": true}`; contradicts "the engine will not guess" | `expect.get("count")` with no default; inline the helper |
| 18 | LOW | `validate.py:1608` + `runtime.py:180` | Quoted `"lease_horizon": "3600"` silently skips the coherence check; non-str `run_dir` dies as a bare TypeError | Type-error on non-numeric; one isinstance in validate_root |
| 19 | LOW | `adapters/mcp_server/tools.py:88` | MCP `list_gates` still omits `wiring` → `wiring_mismatch` permanently null on the surface whose docstring claims the drift was fixed | Pass `current_wiring(root, base)` like `cli.py:1016`; better, share the CLI's assembly helper |
| 20 | LOW | app `transforms.py:~712`, `factory_review.py:721, :1615, :1644`, `baton.py:64` + manual | Case-strict `!= "REAL_BUG"` silent exit in the absence cap; whitespace-only question defeats the ask fallback; `factory wait` lacks the fault-park arm; `checkpoint_ttl` frozen-at-mint vs manual prose | Lowercase the verdict compare; `.strip()` the question; add the wait arm; say "fixed at run start" in the manual |

## Elegance / simplicity — the cross-cutting findings

**Share the shape, not the spelling.** The four-field usage payload is written
literally in 4 places with no type (`claude_cli_provider.py:470`,
`litellm_provider.py:119`, `agent.py:350+356`, `langfuse_trace_sink.py:59`) —
a provider typo prices tokens at $0 forever with zero errors; `api_provider.py`
is the established TypedDict vocabulary home and has no entry for it. Same
class: the CAS tier hand-probed 3 ways in `baton_store.py:50/69/77` when
`store.py:48` already ships a runtime-checkable `CompareAndSet` Protocol
(resolve once in `__init__`; the half-tier state becomes unrepresentable);
`_bounded_error` as the engine's 7th truncation policy (one `bounded()` helper,
callers pass limits); lease-default resolution in 2 homes (normalize inside
`LeaseState.of`); the app's env-read+dual-import dance duplicated across
`_worktree_location_from_run_root`/`_task_from_run_root`; and
`EVIDENCE_SCOPES`/`_SCOPE_GAP` as twin lists (derive one from the other).

**Delete the speculative surface.** `retry`/`attempt`/`n` are projected and
read by nothing (wire them into `_render_errors` or drop; rename bare `n`);
the aggregate's cache rollups have no renderer (fix #6 makes them necessary —
do both); `allow_unknown_node_keys` is a blanket opt-out shipped in the same
commit as the check it disables, set by nobody (drop, or scope per-node); the
catalog-fence machinery is ~165 lines + a 105-line test protecting one fenced
block, which it relocates to the end of the file (a sibling notes file gives
the same guarantee in ~5 lines and preserves position); dead defensive
`setdefault` at app `transforms.py:4298` (both `_route` callers always supply
the key); `_expect_count` is 9 lines for one call site.

**Move the narrative out of the code.** Measured comment/doc ratios in the
harness delta: `harness.py` 70%, `baton.py` 73%; `_checkpoint` carries a
43-line docstring over 5 lines of code. The app's worktree incident is retold
in ten homes (a 66-line docstring — largest in a 5,000-line module — plus a
3,018-char and a 2,913-char graph note); one 300-char `where` description is
copied 30 times in the pipeline JSON, a new tool-usage string 8 times.
DECISIONS is the declared canonical home — keep the *rule* and one pointer at
each site, keep the alternatives-considered arguments and review-round
provenance (unresolvable "MED-2" citations) in the decision log. Hoist the
repeated JSON strings via the graph's existing `extends`. The 0.1×/1.25×
pricing rationale is a constant in one place and prose in six others — cut the
prose to pointers.

**Two policies over one comparison.** `_check_wiring`/`_check_gate_wiring`
share the "does the stage still exist" question but only one implements it
(fix #3 hoists it); `_escalation_text`/`_escalation_line` disagree on
null-safety and empty-message handling across two files that import each other
(one shared `escalation_lines(baton, first_line=)`, which also fixes #10);
`run_root_input`/`_load_task_request` diverged within a single commit (fix
#12); five hand-rolled stderr prints in `harness.py` in the same commit whose
`fork_coordinator._error_span` exists to stop exactly that drift.

## Philosophy preservation (app cluster)

| Invariant | Verdict |
|---|---|
| Markdown, not code | PRESERVED — M36/M37 behaviour lands in prompt prose; the one new transform (cap_absence) genuinely needs determinism. One leak: `_SCOPE_GAP`'s framework-flavoured vocabulary list belongs in the prompt, a token in code. |
| Agent isolation | PRESERVED, verified against the graph — `refix_evidence` is neither sticky nor in `role:code.carry`; it composes fresh per pass and carries runner output + the judge's one-line reason, no reviewer reasoning. |
| Cold sceptics | PRESERVED — cap-absence is pure code; judge-findings reads `verdict_precap` as a flag. |
| Thin orchestrator | PRESERVED — all routing is graph branches on payload keys; the worktree 3-arm branch defaults to `blocked` with a non-guessable success token. |
| Config as adaptation layer | PRESERVED — zero host/language leakage in production code (every framework-ish hit is a fixture or help example); the surviving `or "task"` in salvage is fix #15. |
| Fail-loud | PRESERVED — no new `or "."`; `load_merged_pipeline` abspaths precisely to kill a cwd-rooting. |
| Py3.9 | CLEAN — AST-scanned all 14 delta modules. |
| Domain-free engine | CLEAN — grep over `src/` returns zero app/consumer references. |

## Verified NOT bugs (so the fix batch doesn't re-litigate)

- `_degraded_output`'s store drain does not race a live fan-in coordinator —
  the coordinator task is cancelled and gathered before it runs.
- The CAS read→claim window in `resume_running` contains no intervening await.
- First-stage checkpoint batons default `status="running"` — picked up by
  `list_running` and `is_expired`; no orphan.
- `validate.py:1605` does enforce `lease_horizon ≤ checkpoint window`.
- The removed `validators:` keys on A/B-eval NODE specs were dead config
  (yaah reads `validators` as a stage key) — deleting them disabled nothing.
- Cost arithmetic itself is correct in all three providers — no
  double-subtraction; back-compat pricing has a real test.

## Ground truth appendix

- Engine: `python3 scripts/run_tests.py --no-coverage` → `PASS=130 FAIL=0`
  (2026-08-06, pre-review).
- App: `bash S_Factory/yaah_app/run-checks.sh` → `== OK ==` twice
  (2026-08-06), including the three M35 fault-park recovery e2e scenarios.
- Fix #1 repro: `"{{run_dir}}/x".replace("{run_dir}", "/ABS")` →
  `'{/ABS}/x'` (also re-confirmed by reading `macros.py:69-75`: no boundary
  guard exists).
- Fix #2 re-confirmed by reading `harness.py:474-487`: owner stamped and
  CAS-claimed, then `return await self._settle(...)` with no release path.
- Fix #10 repro: `{'code': 'worktree_dirty'}` → `IndexError`;
  `{'code': 'c', 'message': ''}` → `IndexError`.
- Fix #11 repro: `'the user.role check is missing'` → `('user.role', 'file')`;
  `'/app/models/task.rb:42'` → tier `prose`.
