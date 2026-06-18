# arch-drift — keep architecture docs in sync with the code

A pipeline that compares the architecture extracted from the code with the
currently-committed architecture SVG. If they disagree, it parks for a human
review and — on approve — lands a new versioned SVG.

## What it does, stage by stage

```
snapshot → read-svg → extract → parse → render-svg → diff
                       (agent +                         │
                        validator                       │
                        + retry-with-feedback)          │
                                                        ▼
                                                 changed?
                                                "no"     "yes"
                                                 │         │
                                                 ▼         ▼
                                                done     report → gate (approve_or_revise)
                                                                    │
                                                          approve ──┴── revise (loop to extract)
                                                            │
                                                            ▼
                                                          land (versioned SVG)
```

- **snapshot** — `transforms.snapshot_imports` walks `repo_path`, builds a
  bounded text summary of top-level packages + module docstrings + internal
  imports (the dependency edges that matter for an architecture picture).
- **read-svg** — reads the currently-committed `latest.svg` (or returns
  empty string on first run; the diff stage treats empty as "drift = yes").
- **extract** — agent prompted with the snapshot + any prior human feedback,
  outputs `{mermaid, notes}` JSON. Validator (`json_object`, required keys =
  `["mermaid"]`) + retry-with-feedback handles malformed replies.
- **parse** — merges `mermaid` and `notes` onto the payload.
- **render-svg** — `transforms.render_mermaid` shells out to `mmdc`
  (mermaid-cli). When `MERMAID_RENDERER=:canned` is set in the environment,
  returns a pre-baked SVG — used by the local/fake config so the example
  runs offline without npm.
- **diff** — normalized SVG comparison (strips run-unique ids, collapses
  whitespace), produces `{changed: "yes"|"no", summary}` and **branches** on
  `changed`.
- **report** — renders `arch-report.html` with both SVGs side-by-side.
- **gate** — `human_gate` with `form: "approve_or_revise"` (one of the
  decision forms shipped with YAAH; see
  [`docs/decision-forms.md`](../../docs/decision-forms.md)). Parks until a
  human resumes with `{"decision": "approve"}` or
  `{"decision": "revise", "feedback": "..."}`.
- **land** (on approve) — writes the new SVG to
  `docs/architecture/<utc-ts>.svg` AND updates `latest.svg` to a copy of
  the same content. **Does not auto-commit** — prints the suggested
  `git add && git commit` line and exits, so the user keeps control.

## Run it (offline, no API key, no mmdc)

```bash
MERMAID_RENDERER=:canned yaah run examples/arch-drift/arch-drift.local.json
```

The fake provider scripts a canned mermaid response and the canned renderer
returns a pre-baked SVG, so the entire pipeline runs end-to-end and parks at
the human gate. To inspect what's parked:

```bash
yaah list examples/arch-drift/arch-drift.local.json --json
```

To get the decision shape (so you don't have to guess):

```bash
yaah baton-schema examples/arch-drift/arch-drift.local.json <baton-id>
```

To approve:

```bash
echo '{"decision":"approve"}' > /tmp/d.json
yaah resume examples/arch-drift/arch-drift.local.json <baton-id> /tmp/d.json
```

To revise instead:

```bash
echo '{"decision":"revise","feedback":"split the adapters layer by kind (transports vs backends vs stores)"}' > /tmp/d.json
yaah resume examples/arch-drift/arch-drift.local.json <baton-id> /tmp/d.json
```

The revise feedback rides on the payload back into the `extract` stage; the
prompt template's `{{feedback}}` placeholder picks it up and the agent
attempts again with that guidance.

## Run it for real (claude-sonnet-4-6 + mmdc)

```bash
# install mermaid-cli once
npm install -g @mermaid-js/mermaid-cli

# then
yaah run examples/arch-drift/arch-drift.real.json
```

`arch-drift.real.json` `_extends` the local config and swaps the `claude`
provider from `fake_scripted` to the real `claude_cli` backend.

## Run it on another Python repo

`fixtures/input.json` declares `repo_path`, `arch_svg_path`,
`arch_svg_dir`. To target a different repo, copy the example directory and
edit `fixtures/input.json`:

```json
{
  "repo_path": "/path/to/your/repo",
  "arch_svg_path": "docs/architecture/latest.svg",
  "arch_svg_dir": "docs/architecture"
}
```

The default `snapshot_imports` strategy looks for top-level Python packages
under `src/` (or directly under `repo_path` if there's no `src/`). For
non-Python repos or non-standard layouts, drop in your own snapshot function
(see "Adding a snapshot strategy" below).

## Adding a snapshot strategy

Open `transforms.py`. Each snapshot strategy is one function that takes
`(envelope, config)` and returns `{snapshot: str, feedback: str}`. Plausible
alternatives:

| Strategy | When to use |
|---|---|
| `snapshot_imports` (shipped) | Default — works for any layered Python project. |
| `snapshot_readme_first` (not shipped) | Project keeps a hand-maintained `docs/architecture.md` you want to use as the seed instead of code-walking. |
| `snapshot_changed_files_only` (not shipped) | Only consider files changed since a baseline commit — for "did this PR's changes affect the architecture?" |
| `snapshot_top_modules_only` (not shipped) | Names + first docstring line only; cheaper, lower fidelity. |

To use one, add the function in `transforms.py` and change the pipeline's
`role:snapshot` `target` to `fn:transforms:your_new_function`. No engine
change needed.

## Planned variant: A/B model comparison

When we have signal that the pipeline works at all, the natural next step is
a second pipeline JSON that **forks** the `extract` stage to two models in
parallel — typically `claude-sonnet-4-6` (A) vs `claude-haiku-4-5` (B) — and
shows BOTH candidates in the report. The human picks one. This is genuinely
yaah-shaped (real fork, real fanin, real human judging) and answers an
empirical question: "is haiku good enough at this task to halve the cost?"

The open design question deferred to that variant: **what does "revise"
mean when there are two candidates?** Options: feedback goes to both
branches and they retry in parallel; feedback goes only to the human's
preferred candidate; the loser is dropped after revise. To be decided
when we build it. (The new `form: "json_schema"` escape hatch on
`human_gate` lets us define a richer decision shape — e.g.
`{decision: "approve_a"|"approve_b"|"revise", feedback: "..."}` — without
adding to the generic catalog.)

## What this example demonstrates

Picked specifically because it clears all four "yaah earns its keep" bars:

1. **Multi-stage** with deterministic + LLM + validator steps.
2. **Human gate** that can park for hours-to-days (you don't always approve
   architecture changes the same hour the code lands).
3. **Real branches** on `changed` (skip the gate when no drift) and on
   `decision` (approve vs revise loop).
4. **Amortizes** — runs on every push, nightly cron, or weekly review.

It also exercises the recently-shipped CLI surface end-to-end:
`yaah list --json` finds the parked baton, `yaah baton-schema` returns the
`approve_or_revise` shape, `yaah resume` delivers the decision, the
`feedback` payload key flows back into the agent prompt on revise.

## Dependencies

| Dependency | Required for | Install |
|---|---|---|
| Python 3.9+ | All runs | (system) |
| YAAH | All runs | `pip install -e .` from repo root |
| `mmdc` (mermaid-cli) | Real runs only — the canned overlay skips it | `npm install -g @mermaid-js/mermaid-cli` |
| `claude` CLI | Real runs only — the local overlay scripts a fake | https://docs.claude.com/en/docs/claude-code |
