# YAAH ecosystem

Most things built *on* YAAH live in their own repositories, not this one. The
core stays small on purpose (see [ADR-0001](docs/decisions/0001-three-concepts.md)) —
your code stays yours; we link to it.

## Tiers

| Tier | Lives in | Maintained by |
|---|---|---|
| **Core** | this repo — `src/yaah/{core,harness,comms}` | maintainer |
| **Official adapter** | this repo — `src/yaah/adapters/<name>` | an adapter owner (see [GOVERNANCE.md](GOVERNANCE.md)) |
| **Community package** | its own repo | its author |
| **Application** | its own repo | its author |

A community package can become an official adapter once it has proven stable and
its author is willing to own it here; the move is recorded as an ADR. The reverse
is also fine — an adapter that loses its owner can move back out.

## Naming & discovery

- Distribute as `yaah-<thing>` on PyPI (encouraged, not required).
- Tag your repo `yaah-pipeline` or `yaah-adapter` on GitHub so others find it.

## Registry

No community packages are listed yet. Built one? Open a PR adding a row here. The
only rules: your project has a README and a license, runs against the current
YAAH, and you respond to its issues.
