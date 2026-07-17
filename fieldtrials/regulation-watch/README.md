# Regulation-watch — AI-usability field-trial kit

A standing, rerunnable trial you hand to AI builder agents to measure how well
yaah works for a **cold** AI user (one with no prior yaah context), with
observable evidence for every wrong turn. Two parts plus a self-check:

- **Part A** (`brief-a.md`) — author a pipeline from scratch. Measures the
  build-from-nothing path.
- **Part B** (`part-b/`) — fix a broken pipeline seeded with five landmines.
  Measures the debug-with-the-tooling path.
- **`selfcheck.py`** — asserts the five landmines still trip as documented. Run
  it before every trial; an engine change can silently defuse a trap.
- **`answer-key.md`** — TRIAL RUNNER ONLY. The scoring anchor: each landmine's
  intended signal, fix, and the real engine behavior (verified output pasted).

The theme (regulation-change triage) is fictional and self-contained. All notice
fixtures are invented — no real regulation text.

---

## Before you run: sanity-check the traps

```
PYTHONPATH=src python3 fieldtrials/regulation-watch/selfcheck.py
```

Expect six `PASS` lines. If any check fails, the engine has drifted from what
`answer-key.md` documents — reconcile the kit and the answer key before trialing,
or the trial measures nothing.

`selfcheck.py` is standalone; it is deliberately NOT part of
`scripts/run_tests.py` (it shells out to the CLI and depends on this directory).

---

## Run matrix

Run the full matrix. Sonnet's results weigh more (it is the executor tier — the
model that actually drives pipelines in production), so its stumbles are the
signal that matters most.

| Run | Model | Part | Directory |
|-----|-------|------|-----------|
| 1 | opus | A (author) | fresh dir OUTSIDE this repo |
| 2 | opus | B (debug) | fresh copy of `part-b/` OUTSIDE this repo |
| 3 | sonnet | A (author) | fresh dir OUTSIDE this repo |
| 4 | sonnet | B (debug) | fresh copy of `part-b/` OUTSIDE this repo |

Rules that keep the trial honest:

- **Cold.** Each builder starts with no yaah context beyond its brief and what
  `yaah scaffold` ships. Do not coach. Do not answer mid-run questions.
- **Copy trial files to a neutral directory well outside this repo.** The path
  must not reveal the repo location (e.g. `/tmp/rw-trial-A/` not
  `/home/user/yaah-harness/fieldtrials/trial-run/`). For Part B, copy `part-b/`
  alone into the neutral dir — do NOT copy `answer-key.md`, `selfcheck.py`, or
  `README.md`. Keeping the kit dir out of the agent's path is the primary guard
  against accidental reads.
- **State in the builder brief** that reading anything outside the working directory
  is out-of-bounds and will be visible in the transcript. Exact wording to include:
  > "Your working directory is `<WORKING_DIR>`. Reading files outside it (engine
  > source, docs, or trial-kit files from another path) is out-of-bounds for this
  > trial and will be flagged in scoring."
- **Install yaah with a built wheel or non-editable install**, so the engine appears
  under site-packages and its source path is not the repo tree:

  ```
  # preferred: build a wheel first (one-time per branch)
  python3 -m build --wheel /path/to/yaah-harness -o /tmp/yaah-wheels/
  python3 -m venv .venv && . .venv/bin/activate
  pip install /tmp/yaah-wheels/yaah_harness-*.whl

  # acceptable: non-editable install from the repo path
  python3 -m venv .venv && . .venv/bin/activate
  pip install /path/to/yaah-harness
  ```

  Do NOT use `pip install -e`. An editable install puts the repo root on
  `sys.path`, which makes `src/yaah/`, the fieldtrials tree, and `answer-key.md`
  accessible via relative traversal — round-1 evidence showed at least one builder
  read `answer-key.md` mid-trial this way (transcript-verified). A non-editable
  install limits source reads to site-packages (installed copies, not the live
  repo). If a builder reads engine source under site-packages, that is still a
  red-flag event; if they reach the REPO `src/` tree, that is an invalidating event.

  **Distinguishing site-packages vs repo reads in the transcript:** look at the
  file path in the Read tool call. A path under `.venv/lib/…/site-packages/yaah/`
  is an installed-package read (elevated red flag). A path under the original repo
  tree (`.../yaah-harness/src/yaah/`) is a repo read that should not have been
  reachable — treat it as an isolation failure and discount the run accordingly.

---

## Monitoring method

- **Run each builder as a background agent.** Its **tool transcript is the
  telemetry** — every Bash command, file read, and edit is a data point. You are
  reconstructing the builder's path, not just grading the final artifact.
- **The friction log is a mandatory deliverable.** Part A asks for a friction
  log; Part B asks for a problem log (formats are in the briefs). A run with a
  green artifact but no friction log is INCOMPLETE — the log is the primary
  finding, the artifact is secondary.
- Keep the raw transcript with each run. When scoring a trap, cite the exact
  transcript moment (the command, the output the builder saw) that rescued it or
  led it astray.

---

## Scoring rubric

Score **per trap** (Part B's L1–L5; for Part A, score each of the five build
requirements the same way — iteration, classification-under-injection, the
retry loop, the gate, the digest). For each:

| Field | What to record |
|-------|----------------|
| **Attempts to recovery** | How many edit→validate/run cycles until the trap was resolved. 1 = caught immediately. `∞` = never resolved / shipped broken. |
| **Rescuing surface** | Which surface actually got the builder unstuck: `lint` / `error message` / `--json` / `scaffolded AGENTS.md` / `docs` / `luck` (stumbled without understanding) / `reasoning` (figured it out unaided). Pick the one that did the work. |
| **Red-flag events** | See the list below. Note each occurrence with the transcript moment. |
| **Notes** | Anything the friction log claims that the transcript confirms or contradicts. |

### Red-flag events (each is a usability defect, not a builder failure)

- **Read the answer key or trial-kit files** (`answer-key.md`, `selfcheck.py`,
  `README.md` from the kit) → **invalidating event**. Discard successes on any
  trap the builder read the answer for; keep stumbles (the builder mis-read or
  ignored it). Record the exact transcript moment and the file path. If the run
  was enabled by an editable install (`pip install -e`), note the isolation failure
  alongside; the trial scores as "compromised" and is informational only.
- **Read yaah engine source** (repo `src/yaah/…` or site-packages `yaah/`) to make
  progress → the DOC surface failed. The docs/AGENTS.md/error messages should have
  carried the builder. Distinguish repo reads (isolation failure, more severe) from
  site-packages reads (builder reached past the docs, less severe but still a flag).
  Round-1 lesson: editable installs inflate engine-source reads because the repo
  tree is on `sys.path`; use non-editable installs to distinguish genuine curiosity
  from accidental accessibility.
- **Ignored a lint warning** it had already seen (e.g. re-ran `validate`, saw
  `missing-carry`, moved on without fixing) → the message did not land / was not
  actionable enough.
- **Deleted a state dir** (`rm -rf state`, or nuking `.yaah-state*`) to get
  unstuck → the parked-gate / resume model was confusing enough that the builder
  reached for the destructive reset. (Especially damning around the gate.)
- **Silent trap shipped** (L2 left in the "fixed" deliverable) → the "runs but
  wrong" class went undetected. Note whether the builder even suspected it.

### How to interpret

- **Weight sonnet's stumbles more heavily.** Opus clearing a trap tells you the
  ceiling; sonnet clearing it tells you the floor, and the floor is what ships.
  A trap that opus reasons past but sonnet ships broken is a real doc/lint gap.
- **Rescuing surface is the headline metric.** A trap rescued by `lint` or
  `--json` is a WIN for the engine (the tooling did its job). A trap rescued only
  by `reasoning` or `AGENTS.md`-reading, or not at all, points at where a lint or
  a sharper error message would pay off. A trap rescued by `luck` is the worst
  outcome — it means the next builder won't be so lucky.
- **The silent trap (L2) is the acid test.** It fires no signal on purpose — the
  decoy placeholder renders empty with no lint and no fault. If a builder catches
  it, note HOW (docs? reading the transform? reasoning?). If a builder proposes a
  new "loop-key mismatch" lint ("the engine should warn when a prompt reads a key
  the loop transform never writes"), that is a top-tier trial finding — forward it
  to the engine team.
- **L3 is now a lint-rescued trap.** The `gate-route-not-in-form` lint fires as a
  hard error at `yaah validate`, so builders see it immediately. The interesting
  measurement is WHICH FIX the builder picks: upgrade the form to `approve_or_revise`
  and rename the `reject` route to `revise` (preserving the two-path behaviour), or
  simply drop the dead route (simpler, but loses the loop-back path). Both clear the
  lint; only the first is the semantically correct fix for a pipeline that should
  loop back on rejection.
- **Attempts-to-recovery** is secondary to rescuing-surface: a trap that took
  five cycles but was ultimately caught by a clear lint is healthier than one
  caught in one cycle by luck.

Aggregate across the matrix: which surfaces rescue which traps, where sonnet
diverges from opus, and which traps ship broken. That profile is the deliverable
of a trial round.

---

## Exact builder-briefing text (paste when launching a trial agent)

Minimal prep — hand the builder ONLY the block below (filled in for the part and
model). No coaching beyond it.

### For a Part-A run

```
You are a builder working in <WORKING_DIR> (a fresh directory; yaah is already
pip-installed). Read brief-a.md in that directory and build what it asks for.
Your ONLY guidance is that brief plus whatever `yaah scaffold` gives you (run
`yaah scaffold --list` to see archetypes; a scaffolded project ships an
AGENTS.md worth reading). Work offline with a fake provider. Do not ask me
questions — if something is unclear, make a reasonable call and note it in your
friction log. Deliverables: the working config(s), a raw run transcript
(validate + run + the gate park/resume), and the friction log in the exact
format brief-a.md specifies. The friction log is the point — be specific and
honest about every place the engine slowed you down.

Your working directory is <WORKING_DIR>. Reading files outside it (engine source,
docs, or files from any other path) is out-of-bounds for this trial and will be
flagged in scoring.
```

(Copy `brief-a.md` and `fixtures/notices/` into `<WORKING_DIR>`, which should be
a neutral directory well outside the yaah-harness repo. Do NOT copy `answer-key.md`,
`README.md`, `part-b/`, or `selfcheck.py`. Use a non-editable pip install — see
the "install" rule above.)

### For a Part-B run

```
You are a builder working in <WORKING_DIR> (a fresh copy of a yaah project;
yaah is already pip-installed). Read brief-b.md in that directory. The project
is broken on purpose. Make it strict-clean (`yaah validate --strict` exits 0)
and offline-green (`yaah run root.local.json` runs to the human gate, parks, and
completes after you drive the gate). Document every problem you find and how you
found it, in the exact format brief-b.md specifies. Your guidance is brief-b.md
plus the project's own AGENTS.md and the yaah docs — do not ask me questions.
Fix the pipeline, not the engine. If you think something is an engine bug rather
than a config defect, write it down instead of changing it.

Your working directory is <WORKING_DIR>. Reading files outside it (engine source,
docs, or files from any other path) is out-of-bounds for this trial and will be
flagged in scoring.
```

(Copy `part-b/` alone into `<WORKING_DIR>`, which should be a neutral directory
well outside the yaah-harness repo (e.g. `/tmp/rw-trial-B-<date>/`). Do NOT copy
`answer-key.md`, `selfcheck.py`, or `README.md` — these must not be reachable
from the builder's working dir. See the "install" rule above for the non-editable
pip install step.)

---

## Directory layout

```
regulation-watch/
  README.md                 this file (trial-runner's guide)
  brief-a.md                Part A authoring brief (given to the builder)
  answer-key.md             TRIAL RUNNER ONLY — scoring anchor
  selfcheck.py              standalone trap-integrity check (run before each trial)
  fixtures/
    notices/                7 fictional regulation-change notices (2 injection, 1 high-impact)
  part-b/                   the broken project (Part B) — copy this dir to run
    brief-b.md              Part B debugging brief (given to the builder)
    pipeline.json           nodes + graph (with seeded defects, per answer-key.md)
    root.local.json         offline fake overlay (with seeded defects, per answer-key.md)
    input.json              single-notice input
    transforms.py           the tally loop transform (writes loop_feedback)
    prompts/                classify / draft / judge prompts (with seeded defects)
    templates/digest.md     final render template
    fixtures/notices/       a self-contained copy of the notices
```
