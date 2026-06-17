<!-- Thanks for contributing to YAAH. Keep this short; delete what doesn't apply. -->

## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## The three lenses (see CONTRIBUTING.md)

- **Simplicity** — can this be expressed with what already exists? If not, why?
- **Elegance** — does it read like it belongs (naming, file shape, patterns)?
- **Ease of use** — can you explain the change in one sentence?

## Pre-submission self-review

- [ ] Ran the self-review (`/yaah-review-my-pr`, the `AGENTS.md` section, or `python3 scripts/review_my_pr.py`)
- [ ] No new top-level concept / built-in node type / root-config key — or an ADR is included (see [ADR-0001](docs/decisions/0001-three-concepts.md))
- [ ] `src/yaah/{core,harness,comms}` gained no third-party import; no domain words anywhere in `src/yaah/`
- [ ] New files: one class, class name = filename, "who calls this, where, why" docstring
- [ ] New behavior has a `tests/test_*.py`; the suite passes and stays above the coverage floor

<!-- If you ran the review skill/AGENTS section, paste its report below. -->
