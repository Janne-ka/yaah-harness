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
