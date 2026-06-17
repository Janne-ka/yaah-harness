# Governance

How YAAH is run, who decides what, and how that set of people changes.

The values come first (see [CONTRIBUTING.md](CONTRIBUTING.md)): **simplicity,
elegance, ease of use**, in that order. Governance exists to keep those values
applied when the maintainer is tired, busy, or wrong about a specific case.

## Roles

| Role | Powers | How you become one |
|---|---|---|
| **Maintainer** | Final call on `core/`, `harness/`, `comms/`, and the three-concepts cosmology. Veto on everything. Owns releases. | By invitation from existing maintainers, after sustained substantive contribution and demonstrated alignment with the values. |
| **Adapter Owner** | Merge rights on a single adapter directory (`src/yaah/adapters/<name>/`). Responsible for that adapter's tests and its issues. | Three or more substantive PRs to that adapter and demonstrated engagement on its issues over at least two months. Invited by a maintainer. |
| **Contributor** | Open issues, open PRs, comment on design discussions. | Open a PR. |

Active roles are tracked in [`CODEOWNERS`](CODEOWNERS). Adding, changing, or
removing a role is recorded as an ADR under [`docs/decisions/`](docs/decisions/).

### Bus-factor commitment

The maintainer commits to onboarding a second person with merge rights to `main`
within the first 12 months of public use, even if that person is inactive
day-to-day. Continuity outranks tidiness.

## Decisions — Architectural Decision Records (ADRs)

Non-trivial decisions are written down in `docs/decisions/NNNN-slug.md`.
Format:

```
# NNNN — Title

## Context
What forced this question?

## Decision
What we decided.

## Consequences
What this enables, what this forbids, what we expect to regret.
```

**An ADR is required for:** a new top-level concept, a new built-in node type,
a new top-level root-config key, a new dependency in `core/`/`harness/`/`comms/`,
any change to the cosmology invariants in [ADR-0001](docs/decisions/0001-three-concepts.md),
or any change to roles or governance.

**An ADR is not required for:** bug fixes, adapter-internal changes,
documentation, tests, or performance work that doesn't add API.

## Day-to-day flow

1. **Issues before PRs** for non-trivial work. State the problem before
   proposing the solution.
2. **Pre-submission self-check.** Contributors run the `yaah-review-my-pr` AI
   skill (or the equivalent prompt under [`AGENTS.md`](AGENTS.md)) before
   opening a PR. The PR template asks them to confirm.
3. **Review.** At least one maintainer or relevant adapter owner approves.
   Changes to `core/`, `harness/`, or `comms/` always require maintainer
   approval, regardless of size.
4. **Merge.** Squash merges. The merge message is the PR title plus the
   rationale paragraph.

## Saying no

The maintainer's hardest job is saying no to good ideas that aren't right for
YAAH. Some norms:

- **No is the default for new nouns.** The contributor's burden is to prove
  the noun is necessary; the reviewer's question is whether composition would
  do.
- **Say no kindly and concretely.** Quote the cosmology invariant. Link the
  ADR. Suggest the composition that would have worked.
- **Don't say "maybe later."** It's not. A PR that compromises simplicity now
  will compromise simplicity later. Close it cleanly.
- **Reward subtraction.** PRs that remove code while preserving capability are
  fast-tracked and credited in release notes.

## Releases

- **SemVer.** From `0.1.0` onward; pre-1.0 minor bumps may break API with a
  CHANGELOG note.
- **CHANGELOG.md** is maintained by the maintainer at release time, not in
  feature PRs.
- **Signed tags.** PyPI publishes on tag via CI.

## Changing this document

Changes to governance are themselves an ADR. Open a PR that adds the ADR and
edits this document; the ADR explains why.
