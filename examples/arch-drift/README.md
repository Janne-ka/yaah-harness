# arch-drift — keep architecture docs in sync with the code

A pipeline that compares the architecture extracted from the code with the
currently-committed architecture SVG. If they disagree, it parks for a human
review and — on approve — lands a new versioned SVG.

## Files at a glance (inheritance map)

```
arch-drift.local.json                  ← offline / fake provider / canned renderer
  └─ arch-drift.real.json              ← swaps in real claude + real mmdc + by_model:null
       └─ arch-drift.dogfood.json      ← adds decisions:{} to auto-approve unattended
            └─ arch-drift.dogfood-self.json
                                       ← overrides `input` to point at input-self.json

arch-drift-ab.local.json               ← A/B variant on fake (two scripted models)

(pipelines, referenced by `pipeline:` in roots:)
arch-drift-pipeline.json               ← A-only graph
arch-drift-pipeline-ab.json            ← A/B graph (fork + fanin)

(fixtures, referenced by `input:` in roots:)
fixtures/input.json                    ← target (b) — yaah's internals visible
fixtures/input-self.json               ← target (a) — yaah as a black box
```

| Config | Use when | Costs |
|---|---|---|
| `arch-drift.local.json` | Testing the pipeline shape without an LLM key. Runs end-to-end on canned mermaid and a fixed SVG. | zero |
| `arch-drift.real.json` | First real run; parks at the gate for you to review. | ~$0.05 claude tokens + ~70s wall |
| `arch-drift.dogfood.json` | CI / unattended → produces `docs/architecture/yaah-with-arch-drift.svg`. | same |
| `arch-drift.dogfood-self.json` | CI / unattended → produces `docs/architecture/arch-drift-only.svg`. | same |
| `arch-drift-ab.local.json` | Demo the A/B model-comparison + attached-cost flow on fake. | zero |

## What it does, stage by stage

```
snapshot → read-svg → extract → render-svg → diff
                      (agent + validator           │
                       + retry-with-feedback,      │
                       parse-by-default)           │
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
  `["mermaid"]`) + retry-with-feedback handles malformed replies. The agent is
  parse-by-default (ADR-0004), so `mermaid` and `notes` land on the payload
  directly — no separate parse stage.
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

Run from the repo root (paths below are repo-root-relative; `fn:transforms`
resolves relative to the config's own directory, so no `PYTHONPATH` hack is
needed). Not installed? `python3 -m yaah.runtime <config>` is the equivalent of
`yaah run <config>`; from a source checkout prefix `PYTHONPATH=src`.

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

### Production checklist

Real-mode has three setup gotchas the offline run hides. All three were hit
the first time we ran this end-to-end. Address them once and you're set.

| Need | Why |
|---|---|
| `mmdc` on PATH | `npm install -g @mermaid-js/mermaid-cli` (may need sudo depending on your npm prefix). The `MERMAID_RENDERER=:canned` override is **test-only** — it returns a fixed pre-baked SVG that doesn't vary by input, so real artifacts need the real renderer. |
| `claude` on PATH | yaah's `claude_cli` backend shells out to it. Comes with Claude Code; check with `which claude`. |
| `extract_json` (already in `transforms.py`) | Real sonnet/haiku wrap JSON in markdown fences; strict `json.loads` fails. Only opus is reliably strict. The example uses `yaah.jsonio.extract_json` (fence/prose-tolerant) — copy that pattern in your own transforms. |
| `by_model: null` in real config (already there) | yaah's `_extends` is a deep merge per RFC 7396 JSON Merge Patch — child `null` deletes a key from the base. `arch-drift.real.json` extends `.local.json` and explicitly nulls the `by_model` field so the `claude_cli` backend doesn't get the fake's `by_model` map. See [docs/root-config-reference.md](../../docs/root-config-reference.md). |
| `decisions:` block for unattended runs | When running headlessly (CI, dog-food), set `decisions: {"<awaiting-tag>": {<decision>}}` in the root so the gate driver auto-resolves instead of parking. See `arch-drift.dogfood.json` for the pattern. |

### Interactive run (you approve at the gate)

```bash
yaah run examples/arch-drift/arch-drift.real.json
# parks at the gate; another terminal:
yaah list   examples/arch-drift/arch-drift.real.json --json
yaah baton-schema examples/arch-drift/arch-drift.real.json <baton-id>
echo '{"decision":"approve"}' > /tmp/d.json
yaah resume examples/arch-drift/arch-drift.real.json <baton-id> /tmp/d.json
```

### Unattended run (auto-approve at the gate)

```bash
yaah run examples/arch-drift/arch-drift.dogfood.json
# end-to-end, no human input; lands SVG under docs/architecture/
```

## Dog-food: produce two diagrams of arch-drift itself

`docs/architecture/` is the home for the diagrams arch-drift produces of its
own world. Two targets coexist there, written by the same pipeline pointed
at different roots via different fixtures:

| Target | Input fixture | Output | What it shows |
|---|---|---|---|
| **(a) arch-drift only** (yaah as a black box) | `fixtures/input-self.json` | `docs/architecture/arch-drift-only.svg` | The example's own pipeline graph (snapshot strategy: `pipeline_json`). yaah is a single external node. |
| **(b) yaah + arch-drift** (yaah's internals visible) | `fixtures/input.json` (default) | `docs/architecture/yaah-with-arch-drift.svg` | yaah's `core` / `harness` / `comms` / `adapters` layers extracted from the actual code (snapshot strategy: `imports`). |

To produce target (a) yourself — the diagram of arch-drift only:

```bash
cd examples/arch-drift
yaah run arch-drift.dogfood-self.json
# auto-approves; writes docs/architecture/arch-drift-only.svg
```

To (re-)produce target (b):

```bash
cd examples/arch-drift
yaah run arch-drift.dogfood.json
# auto-approves; writes docs/architecture/yaah-with-arch-drift.svg
```

Both runs cost a few cents of claude tokens and ~70s of wall time (model
extract ~30s, mermaid render ~5s). Commit the resulting SVGs to track
architectural drift over time.

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

## A/B variant — model comparison with attached cost data

A second pipeline that **forks** the `extract` stage to two models in
parallel — sonnet (A) and haiku (B) — and shows BOTH candidates in the
report alongside their token usage. The human picks one. This answers
the empirical question every advanced yaah user has: *is the cheaper
model good enough for my task?*

Files: `arch-drift-pipeline-ab.json` + `templates/report-ab.html` +
`arch-drift-ab.local.json`.

Demonstrates the `attach: [...]` opt-in (see
[decisions/0003-attacher-port.md](../../docs/decisions/0003-attacher-port.md)):
each fork branch uses `attach: ["fn:transforms:UsageAttacher"]`. The
reference `UsageAttacher` lives in `transforms.py` (engine ships zero
built-ins per ADR-0003 — every consumer copies the 10-line reference).
The report template flattens the `candidates` list into per-candidate
keys and computes approximate $ from a `prices` map on the prepare stage
(pricing stays in config per yaah's existing rule).

Run it offline:

```bash
MERMAID_RENDERER=:canned yaah run examples/arch-drift/arch-drift-ab.local.json
```

The fake provider scripts two distinct mermaid responses (visible
differences between A and B even on fake), and the gate uses the
`json_schema` decision form (see
[decision-forms.md](../../docs/decision-forms.md)) with decision shape
`{"decision": "approve_a"|"approve_b"|"revise", "feedback": "string?"}`.
Inspect with `yaah list <root> --json` and `yaah baton-schema <root> <id>`.

To approve:

```bash
echo '{"decision":"approve_a"}' > /tmp/d.json
yaah resume examples/arch-drift/arch-drift-ab.local.json <id> /tmp/d.json
```

Lands `docs/architecture/<utc>-a-claude-sonnet-4-6.svg` (the model name
gets embedded in the filename so the versioned dir disambiguates between
A and B runs).

**v1 limitation: revise is a non-looping exit.** Sending
`{"decision":"revise","feedback":"..."}` surfaces the feedback on stderr
and ends the run; re-run the pipeline with the feedback applied to your
prompt or input. The looping variant (feedback flows back to both
extract branches, both retry) is the v2 design question we deferred.

**Cost data on the fake provider is zero** (fake_scripted doesn't
populate `on_usage`). The price-map multiplication still happens but
produces `$0.00`. For real cost signals, use a real provider (`claude`,
`openai-compatible` etc.) — see the canonical `arch-drift.real.json`
shape for how to wire one up; you'd extend it to declare both sonnet
and haiku providers under the `claude` provider name.

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
