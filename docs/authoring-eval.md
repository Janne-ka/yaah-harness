# Authoring eval — can a cold model author a valid config from the manual alone?

**What this is:** an internal, reproducible measurement — NOT a public
benchmark. The task set is small, self-chosen, and lives in this repo; the
numbers say how well *our* generated manual teaches *a* model to author *our*
configs, nothing more. We publish the methodology so the numbers can be
questioned, re-run, and compared over time — not so they can be compared
across frameworks.

**The question it answers:** give a model that has never seen this repo two
things — the output of `yaah manual` (the one generated agent manual,
`src/yaah/manual.py`) and a one-paragraph task description — and let it use
the documented generate → validate → repair loop. Does it land a config the
real validator accepts? How many repair rounds does that take?

Concretely, one round of the loop looks like this. The model gets the manual
plus a task ("a pipeline where an agent must reply with JSON containing an
`items` key; a json_object validator enforces that…") and replies with a
config whose `then` points at a typo'd stage. The validator answers:

<!-- doc-snippet: skip -->
```json
[{"message": "stage 'extract': then 'reprot' is not a stage", "stage": "extract"}]
```

That diagnostic list — the same items `yaah validate --json` emits — goes back
to the model verbatim, along with its previous reply, and it tries again. A
task "lands" when a round's config passes validation; it fails when
`--max-rounds` author calls (default 4: one cold draft + three repairs) are
exhausted.

## The loop

```
manual + task ──> author model ──> reply text
                                     │  yaah.jsonio.extract_json (fence-tolerant)
                                     ▼
                            parsed config object ──── not JSON? that IS the
                                     │                diagnostic; next round
                                     ▼
                     yaah.validate.validate_config
                     (the same check behind `yaah validate --json`)
                       │ valid                    │ invalid
                       ▼                          ▼
              record warnings, done      split_diagnostics ──> next round's prompt
```

Design decisions that keep the measurement honest:

- **Validation is the engine's own.** The script calls
  `yaah.validate.validate_config` / `split_diagnostics` programmatically — the
  exact functions behind `yaah validate --json` — so the eval can't drift from
  what the real repair loop sees.
- **Errors drive repair; lint warnings don't.** Warnings are recorded in the
  row but never fed back. The eval measures *validity*, not lint-cleanliness.
- **A trivially-valid reply doesn't count.** `{}` is a valid ROOT config (no
  pipeline key → nothing to check), so the harness additionally requires an
  inline pipeline object under `pipeline`. This closes only the *empty-reply*
  cheat: a minimally-valid config that ignores the task still counts as
  authored — see "Valid ≠ good" under Known limits.
- **Each round is a fresh, self-contained prompt.** No conversation state: the
  repair prompt carries the manual, the task, the previous reply, and the
  diagnostics explicitly, so a repair can't silently lean on hidden chat
  history and the transcript is fully reproducible from the JSONL.
- **One layer per round is possible.** `validate_config` reports the root
  layer first and the pipeline layer after that, so a draft broken in both
  can honestly cost two rounds.

## The task set

Six built-in tasks, one per topology the manual teaches. Domain-free by
construction (mirroring the engine invariant): each names shapes and node
types, never an application domain.

| id | topology exercised |
|---|---|
| `summarize-and-render` | agent → render (the minimal edge) |
| `gate-branch` | human gate branching on `decision`, revise-loop back |
| `fork-fanin` | parallel fork, fanin barrier, merged render |
| `retry-validator` | `validators` + `max_attempts` + `feedback` retry loop |
| `verdict-branch` | two agents, branch on a payload key with a default route |
| `shell-then-summarize` | deterministic shell node feeding an agent |

The descriptions live in `TASKS` in `scripts/authoring_eval.py`.

## Two modes, one harness

The harness is deterministic; only the author behind the `AuthorFn` seam
(one callable: `AuthorRequest → reply text`) changes.

**Scripted mode (default — CI).** A `ScriptedAuthor` double answers from
canned replies: every task's config is real and valid, one reply arrives
fenced inside prose (proving the tolerant-extraction path), and
`retry-validator`'s first draft carries a broken `then` target so the
diagnostics-feedback round runs end to end. This proves the *loop mechanics*
offline, with zero model calls — it says nothing about any model's ability.

```sh
PYTHONPATH=src python3 scripts/authoring_eval.py --out results.jsonl
```

**Real mode.** Set `YAAH_AUTHORING_EVAL_MODEL` to `<provider>:<model>` and the
same harness calls a real model through the engine's own provider adapters
(`claude_cli` — alias `claude-cli` — or `litellm`; empty model part = the
adapter's default):

```sh
YAAH_AUTHORING_EVAL_MODEL=claude_cli: \
  PYTHONPATH=src python3 scripts/authoring_eval.py --out real-run.jsonl
```

`--task <id>` (repeatable) runs a subset; `--max-rounds N` changes the budget.
Exit code is 0 only when every task landed valid — a CI signal in scripted
mode, a plain "validity < 100%" mirror in real mode.

## What gets reported

One JSONL row per task (deterministic in scripted mode — two runs are
byte-identical), plus a printed summary table:

<!-- doc-snippet: skip -->
```json
{"task": "retry-validator", "mode": "scripted", "valid": true, "rounds": 2,
 "round_details": [
   {"round": 0, "parse_ok": true, "errors": 1, "prompt_chars": 13520, "reply_chars": 1210},
   {"round": 1, "parse_ok": true, "errors": 0, "prompt_chars": 15115, "reply_chars": 1208}],
 "warnings": [], "final_diagnostics": []}
```

- `valid` / `rounds` — the two headline numbers: did it land, and in how many
  author calls (1 = the cold draft was already valid).
- `round_details` — per round: whether the reply parsed as JSON, how many
  validator diagnostics it drew, and prompt/reply sizes. Character counts are
  an honest *proxy* for cost — the provider seam doesn't surface token usage,
  and we'd rather report a crude true number than a precise fabricated one.
- `warnings` — lint warnings on the accepted config (recorded, not repaired).
- `final_diagnostics` — the last round's diagnostics when a task never landed.

The summary line reports validity rate and mean rounds-to-valid across tasks.

## Results

Real-model numbers are deliberately not baked into this page as a living
table — they belong to a dated JSONL artifact produced by the command above,
so a stale table can't masquerade as current. Dated snapshots (below) are the
one exception: the date in the heading says exactly how old they are. The
scripted mode's expected outcome IS fixed and test-enforced
(`tests/test_authoring_eval.py`): 6/6 valid, `retry-validator` taking exactly
one repair round, everything else landing cold.

### First real-model results (2026-07-05)

One run per model through the `claude_cli` adapter (Claude Code CLI 2.1.199),
default budget (4 rounds). N=6 internal datapoints per model — a smoke test,
not a ranking.

| model | first-shot valid | valid after repairs | never valid | mean rounds | reply chars (total) |
|---|---|---|---|---|---|
| `claude-haiku-4-5-20251001` | 6/6 | — | 0 | 1.00 | 10 621 |
| `claude-sonnet-4-6` | 6/6 | — | 0 | 1.00 | 12 510 |

Both models landed every task on the cold draft, with zero lint warnings on
any accepted config — so these runs say the manual suffices for these six
tasks, and say *nothing* about the repair loop's steering power, which never
fired with a real model. A ceiling this flat is a finding about the task set
as much as the models: the descriptions use the manual's own vocabulary (see
Known limits), so the next iteration should be colder-worded tasks, not more
models. Prompt cost was ~8.7k chars per task (the manual dominates); sonnet's
replies ran ~18% longer than haiku's for no validity gain. The JSONL rows
don't preserve reply text, so semantic fidelity (valid ≠ good) stays
unchecked, as designed.

## Known limits (read before quoting numbers)

- **Six tasks is a smoke test, not a distribution.** Enough to catch a manual
  regression that breaks authorability; not enough to rank models.
- **Valid ≠ good.** The validator proves shape, references, and enum
  membership — not that the pipeline does what the task asked. A config can
  land valid and still mis-read the task. Judging semantic fidelity would
  need a rubric or a judge model; this eval deliberately stops at the
  validator's boundary.
- **The task descriptions were written by the manual's authors**, so they use
  the manual's vocabulary. A colder-worded task set would be a harder (and
  fairer) test of the manual.
- **Prompt-sensitivity is unmeasured.** One fixed prompt template; no
  paraphrase robustness runs.
