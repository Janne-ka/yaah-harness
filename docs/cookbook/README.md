# Cookbook

Reference patterns for code that ships in *your* project, not in YAAH.

The engine is deliberately small (see [ADR-0001](../decisions/0001-three-concepts.md)).
Most of what consumers want — domain-specific attachers, custom
transforms, app-shaped reducers — does not belong in `src/yaah/`. This
folder is the alternative: **non-importable reference recipes**, copied
into a consumer's own `transforms.py` (or wherever) and adapted.

## What "non-importable" means

These files are not a package. There is no `from yaah.cookbook import
UsageAttacher`. Trying to import them would defeat the point:

- An importable cookbook would carry semver obligations, deprecation
  cycles, and the slow accumulation of marginal entries.
- A copy-paste cookbook is *yours* the moment you adopt it. We can
  change the canonical version freely; your local copy is unaffected
  until you choose to re-sync.

This is the same rule [ADR-0003](../decisions/0003-attacher-port.md)
applies to attachers — *engine ships zero built-ins, consumers wire
their own*. The cookbook just makes the canonical implementations
discoverable.

## Workflow

1. Find a pattern you want under `docs/cookbook/<category>/`.
2. Read it; copy the function or class into your own `transforms.py`.
3. Keep the attribution comment at the top: `# Source:
   docs/cookbook/<category>/<file>.py` so a later contributor knows
   where the canonical version lives.
4. Adapt as needed. If your adaptation generalizes well, propose a PR
   to the cookbook — but don't make the cookbook itself the dependency.

## When to lift a pattern *out* of the cookbook

A cookbook entry might grow into something stronger. Two triggers:

- **Engine import.** If a pattern proves to be load-bearing for the
  engine's three concepts, it earns a place in `src/yaah/` and an ADR
  explaining why. Most cookbook entries should never cross this line.
- **Sibling repo.** If two independent consumers both depend on the
  pattern and want a pinned, versioned dependency, that's the trigger
  for `yaah-contrib-<thing>` as a sibling repo (own release cadence,
  own tests). The cookbook entry stays; the package emerges alongside.

Until those triggers fire, the cookbook stays read-only-by-design.

## Current entries

- [attachers/](attachers/) — implementations of the
  [Attacher port](../decisions/0003-attacher-port.md). Currently:
  `UsageAttacher` (tokens + model from the tracer's last model_call
  span).
- [offline-runs.md](offline-runs.md) — three idiomatic patterns for
  running a pipeline without an API key (single-file fake provider,
  paired `*.local.json` + `*.real.json` via `_extends`, the inline
  `_fake` block + `--fake` CLI flag). Config-shape reference, not
  Python code — but same audience as the rest of the cookbook.
- [deploy.md](deploy.md) — production-path conventions: single-binary
  Docker, distributed NATS fleet, state-store choices, env-var secrets,
  trace sinks, `yaah doctor` as a HEALTHCHECK. Pairs with
  [offline-runs.md](offline-runs.md) — that's CI / dev mode; deploy.md
  is the real-mode complement.
- [debugging.md](debugging.md) — the "stuck pipeline" playbook. Six
  commands in the order to reach for them: `doctor` → `validate` →
  `explain` → `list` → `trace --pretty` → state-store inspection.
  When to ask "is this a yaah bug or a pipeline bug" + symptom → command
  table.
- [bounded-loop-best-of-n.md](bounded-loop-best-of-n.md) — bounded
  cross-stage loop + best-of-N reduce: produce → judge → tally (with
  cycle counter + best-so-far), branch backward, exit at the cap or
  a score threshold, return the highest-scoring attempt. Reference
  implementation in `examples/verify-loop/`.
- [preflight-guard.md](preflight-guard.md) — validate + normalize
  input in a deterministic `transform` and `branch` on the result, so
  garbage input is rejected BEFORE any model call ("don't pay for a
  model call on garbage input"). Shape gate, not a semantic one.
  Reference implementation in `examples/preflight-guard/`.
- [model-cascade.md](model-cascade.md) — cheap model first, escalate to
  expensive only when needed. Two shapes: the built-in one-rung ladder
  (`escalate_model`, escalates on the cheap model's `help` self-report)
  and the branch cascade (escalates on an external validator verdict),
  with a table for choosing. Reference implementation in
  `examples/model-cascade/`.
- [operator-loop.md](operator-loop.md) — an AI improves a running
  pipeline under a safety envelope: read run evidence → propose ONE
  config revision → `validate --strict` + surface/anti-parroting gate
  → run, score, keep-or-rollback. Proven 33%→96% in two iterations on
  real models. Includes the risk→verb envelope table, the recipe, six
  use cases, and the claim discipline ("gated operator loop", never
  "self-improving pipeline").
