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
- **Fresh directory OUTSIDE this repo.** So the builder cannot read this kit's
  `answer-key.md`, the engine's own docs tree, or another run's artifacts. For
  Part B, copy `part-b/` to the working dir; do not hand over `answer-key.md`.
- **yaah pip-installed from the repo path**, so the builder uses the same engine
  under test but cannot browse `src/` casually:

  ```
  python3 -m venv .venv && . .venv/bin/activate
  pip install -e /path/to/yaah-harness
  ```

  (Reading `src/` is still possible — and if a builder does it, that is a
  red-flag event, see below.)

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

- **Read yaah engine source (`src/yaah/…`)** to make progress → the DOC surface
  failed. The docs/AGENTS.md/error messages should have carried the builder.
- **Ignored a lint warning** it had already seen (e.g. re-ran `validate`, saw
  `missing-carry`, moved on without fixing) → the message did not land / was not
  actionable enough.
- **Deleted a state dir** (`rm -rf state`, or nuking `.yaah-state*`) to get
  unstuck → the parked-gate / resume model was confusing enough that the builder
  reached for the destructive reset. (Especially damning around the gate.)
- **Silent trap shipped** (L2 left in the "fixed" deliverable, or L3's phantom
  route left unreconciled) → the "runs but wrong" class went undetected. Note
  whether the builder even suspected it. (L3 now fails LOUD at resume with
  `decision_rejected` when the gate is driven with the form-forbidden value — a
  builder who exercises the gate is rescued by the error; one who never drives it
  still ships the inconsistency.)

### How to interpret

- **Weight sonnet's stumbles more heavily.** Opus clearing a trap tells you the
  ceiling; sonnet clearing it tells you the floor, and the floor is what ships.
  A trap that opus reasons past but sonnet ships broken is a real doc/lint gap.
- **Rescuing surface is the headline metric.** A trap rescued by `lint` or
  `--json` is a WIN for the engine (the tooling did its job). A trap rescued only
  by `reasoning` or `AGENTS.md`-reading, or not at all, points at where a lint or
  a sharper error message would pay off. A trap rescued by `luck` is the worst
  outcome — it means the next builder won't be so lucky.
- **The silent trap (L2) is the acid test.** It fires no signal on purpose. If a
  builder catches it, note HOW (docs? reasoning? a hunch?). L3 is now a HALFWAY
  trap: silent at validate, but loud at resume (`decision_rejected`) once the gate
  is driven with the form-forbidden value. If a builder proposes a NEW lint for
  either ("the engine should warn when a loop key the prompt reads is never
  written", "…when a branch route can't match the gate's form AT AUTHOR TIME"),
  that is a top-tier trial finding — forward it to the engine team; it is the
  trial working as designed.
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
```

(Copy `brief-a.md` and `fixtures/notices/` into `<WORKING_DIR>` first. Do NOT
copy `answer-key.md`, `README.md`, `part-b/`, or `selfcheck.py`.)

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
```

(Copy the whole `part-b/` directory into `<WORKING_DIR>` first, EXCEPT
`brief-b.md` stays but `../answer-key.md` must NOT be reachable. The simplest
safe move: copy `part-b/` alone into a dir well outside this repo.)

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
  part-b/                   the broken project (Part B)
    brief-b.md              Part B debugging brief (given to the builder)
    pipeline.json           nodes + graph (seeded L1-L5)
    root.local.json         offline fake overlay (seeds L5)
    input.json              single-notice input
    transforms.py           the tally loop transform (writes loop_feedback)
    prompts/                classify / draft / judge prompts (seed L2, L4)
    templates/digest.md     final render template
    fixtures/notices/       a self-contained copy of the notices
```
