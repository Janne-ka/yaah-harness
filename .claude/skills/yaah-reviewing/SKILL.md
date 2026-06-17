---
name: yaah-reviewing
description: Use when auditing or reviewing YAAH engine code under src/ or tests/. Not for small targeted reads — use Read directly. Not for in-flight edits — use yaah-extending.
---

# Reviewing YAAH

**Standing rule:** never commit unless explicitly asked.

## Overview

A YAAH review judges **four axes** (elegant, simple, working, extensible) alongside bugs and security — never just one. The codebase is large (~6.5k lines, ~12 sub-packages); an inline read misses cross-file invariants. **Fan-out is a tool, not a reflex — match it to scope (see Cost ladder below).** Output feeds the autonomous fix-batch loop; the report goes in `docs/assessment-YYYY-MM-DD.md` and the most recent one is the canonical starting point.

## Cost ladder — match scope to method

| Scope of change under review | Method |
|---|---|
| 3-file targeted patch | Inline `Read` + grep. No sub-agents. |
| One cluster (e.g. all of `agents/`) | 1-2 sub-agents on that cluster only |
| Cross-cutting or milestone (first run, branch merge, major refactor) | All 6 clusters in parallel |

## When to Use

- User asks: "review", "assess", "audit", "check for bugs / security / elegance"
- A milestone hits (first end-to-end run, branch merge, port complete)
- You're judging whether a port preserves design invariants
- Not for: small targeted reads ("what does X do") — use direct `Read`

## The six clusters (one sub-agent each)

| # | Cluster | Files |
|---|---|---|
| 1 | Harness engine | `src/yaah/core/`, `harness/` (includes `fork_coordinator.py`, `span_emitter.py` — concerns already split out of `harness.py`) |
| 2 | Comms / transports / store | `comms/` (including the `Subscription` Protocol), `adapters/transports/`, `store/`, `adapters/stores/` |
| 3 | Agents / backends | `agents/`, `adapters/backends/` |
| 4 | Nodes / build / runtime | `nodes/`, `build/`, `runtime*.py`, `external_call.py`, `cwd.py` |
| 5 | Ports & adapters | `data/`, `prompts/`, `mcp/`, `adapters/{data,prompts,mcp,trace}/`, `trace/`, `validators.py`, `recall.py`, `jsonio.py` |
| 6 | App on YAAH (only if your project ships one) | its pipeline configs, transforms, prompts, renderers |

## Invariants to check (yaah-specific)

These are **enforced**, not aspirational:

- **Domain-free engine.** No application-specific (domain) references in `src/`. Engine never references stages by name, tenant fields, test runners.
- **One class per file** + use-case docstring (who calls, where, why).
- **Hug-the-world ports.** Each port has a `routing_*` multiplexer + concrete adapters (`file_*`, `http_*`). Triad must be consistent across data/prompts/mcp.
- **Trust boundary is implicit and undefended.** `fn:module:func` in config = RCE if untrusted; payload-derived paths (`worktree.task`, `cwd_from`) reach destructive ops with no sanitization. Flag every payload→fs/shell/network edge.
- **Agent isolation.** Each stage = fresh `comms.request`. Only named `carry` keys forward. Never wire a self-correction loop between an agent and its own critic.
- **RED/GREEN.** Tests fail before code (hard verdict, `max_attempts:1`); pass after (`shell_check`, feedback refix). Verify both gate sides.
- **Hard human gates must `branch` on `decision`.** A gate with only `then` is decision-blind — a pause, not a gate.
- **Counterfactual sceptics cold-read.** Separate cheap agent, never sees the canonical agent's reasoning.

## Method

1. **Ground truth first.** Run the test suite — it's script-style, not pytest:
   ```bash
   cd yaah; PY="${PY:-$([ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)}"
   for f in tests/test_*.py; do PYTHONPATH=src "$PY" "$f" >/tmp/o 2>&1 && pass=$((pass+1)) || { fail=$((fail+1)); failed="$failed $f"; }; done
   echo "PASS=$pass FAIL=$fail$failed"
   ```
   If a test fails, run it once verbose — distinguish missing-infra from real bug *before* delegating.

2. **Fan out 6 sub-agents in parallel** (one tool block, six `Agent` calls). Each prompt must contain: the cluster's file list, the design invariants above, any known bug to confirm. Ask for: 3-sentence verdict → ARCHITECTURE table (4 axes /5 + one-line justification) → BUGS (file:line + severity) → SECURITY (quote dangerous lines verbatim).

3. **Cross-confirm anything CRITICAL/HIGH.** A bug isn't real until reproduced (run the failing test, decode the parked baton, etc.). Two sub-agents independently flagging the same defect is strong; a single agent's claim is a hypothesis.

4. **Synthesize.** Per-cluster ratings → prioritized fix table (file:line, severity, fix) → philosophy-preservation matrix for cluster 6. Write to `docs/assessment-YYYY-MM-DD.md`.

## Common mistakes

| Mistake | Reality |
|---|---|
| Fanning out 6 sub-agents for a small change | Use the Cost ladder — six is for cross-cutting/milestone scope. |
| Reviewing inline a cross-cutting change | Misses cross-file invariants; blows context. Cluster fan-out is the right shape. |
| Only finding security/bugs | Elegance + simplicity matter equally — they predict what *will* break. |
| Reporting without `file:line` | Unactionable. Every finding needs a citation — but cite the *symptom + file*, not the brittle line number (rots fast across refactors). |
| Trusting docstring claims | Run the tests. The `Comms.subscribe` teardown gap surfaced only under NATS — read tracebacks before dismissing infra failures. |
| Counting NATS failures as "infra" without reading | They may be a real `AttributeError` in a clearable/fork teardown path. Read the traceback. |

## Output anchor

The report goes in `docs/assessment-YYYY-MM-DD.md` and must include: verdict, ratings table, headline findings, prioritized fix list, philosophy-preservation matrix, reproduce-ground-truth appendix. **This file is the input to the autonomous fix-batch loop** — write it so a downstream agent can pick fixes from the prioritized table without re-deriving context. Keep it a point-in-time snapshot (do not fold later actions in); start a new dated file on the next review.
