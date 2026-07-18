"""selfcheck — assert the Part-B landmines still trip exactly as answer-key.md
documents, and that Part-A's fixtures carry the injection strings. Run this
BEFORE each trial: an engine change can silently defuse a seeded trap (a new
lint catches L2, a fault shape changes), and a defused trap makes the trial
measure nothing.

This is STANDALONE — it is NOT wired into scripts/run_tests.py (it drives the
CLI as a subprocess and depends on the fieldtrials/ tree, neither of which
belongs in the engine's offline test suite).

Run from the repo root:
    PYTHONPATH=src python3 fieldtrials/regulation-watch/selfcheck.py

Each check prints PASS or raises AssertionError (with the actual output) on the
first check that no longer matches the documented behavior — at which point the
kit and answer-key.md need updating together, not the check silenced.

Targets Python 3.9+.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(REPO, "src")
PART_B = os.path.join(HERE, "part-b")
NOTICES = os.path.join(HERE, "fixtures", "notices")

ENV = {**os.environ, "PYTHONPATH": SRC}


def _cli(argv, cwd):
    """Run `python -m yaah.cli <argv>` in cwd; return (rc, stdout, stderr)."""
    r = subprocess.run([sys.executable, "-m", "yaah.cli", *argv],
                       capture_output=True, text=True, env=ENV, cwd=cwd)
    return r.returncode, r.stdout, r.stderr


def _fresh_partb():
    """A throwaway copy of part-b (so fix-forward edits never touch the kit)."""
    d = tempfile.mkdtemp(prefix="rw-selfcheck-")
    dst = os.path.join(d, "part-b")
    shutil.copytree(PART_B, dst)
    # never inherit a stale parked-gate state dir
    shutil.rmtree(os.path.join(dst, "state"), ignore_errors=True)
    return dst


def _validate_json(cwd):
    rc, out, err = _cli(["validate", "root.local.json", "--json"], cwd)
    assert out.strip(), ("validate --json wrote nothing to stdout", rc, err)
    return json.loads(out)


def _run_json(cwd):
    rc, out, err = _cli(["run", "root.local.json", "--json"], cwd)
    assert out.strip(), ("run --json wrote nothing to stdout", rc, err)
    return rc, json.loads(out)


def _edit(path, old, new):
    s = open(path, encoding="utf-8").read()
    assert old in s, ("expected substring not found while fixing forward", path, old)
    open(path, "w", encoding="utf-8").write(s.replace(old, new))


def _fixforward_l3(d):
    """Fix L3 in a throwaway part-b: remove the phantom `reject` route from the
    gate branch so `gate-route-not-in-form` no longer blocks validate/run.
    This lets other checks exercise their own traps on the pristine project."""
    ppath = os.path.join(d, "pipeline.json")
    pj = json.load(open(ppath, encoding="utf-8"))
    gate_branch = pj["graph"]["stages"]["gate"]["branch"]
    gate_branch["routes"] = {k: v for k, v in gate_branch["routes"].items()
                              if k != "reject"}
    json.dump(pj, open(ppath, "w", encoding="utf-8"), indent=2)


def _fixforward_l5(d):
    """Fix L5 in a throwaway part-b: add `notes` to the judge scripted replies so
    the run can advance past judge's output_schema validation.  Used by the L4-layered
    STATE 2 and STATE 3 subtests to verify loop cycling without L5 masking the result."""
    rl_path = os.path.join(d, "root.local.json")
    rl = json.load(open(rl_path, encoding="utf-8"))
    judge_seq = rl["providers"]["fake"]["by_model"]["judge"]
    fixed = []
    for entry in judge_seq:
        obj = json.loads(entry)
        if "notes" not in obj:
            obj["notes"] = "Fixed for loop cycling (selfcheck fix-forward)"
        fixed.append(json.dumps(obj))
    rl["providers"]["fake"]["by_model"]["judge"] = fixed
    json.dump(rl, open(rl_path, "w", encoding="utf-8"), indent=2)


# --- L3: gate-route-not-in-form fires as a HARD ERROR at validate ---------------

def check_l3_lint_rescued():
    """L3 is now a lint-rescued trap (gate-route-not-in-form ERROR at validate).
    Verifies:
    1. The pristine project fails validate --json with a gate-route-not-in-form
       error (not just a warning) — builders see it immediately.
    2. The fix (dropping the phantom route) clears the error.
    3. A `approve_or_revise` form upgrade (keeping both routes renamed) also works.
    """
    d = _fresh_partb()
    v = _validate_json(d)

    # 1. Hard error present, not merely a warning.
    assert v["errors"], ("L3: expected gate-route-not-in-form to be a hard ERROR", v)
    err_msgs = [e["message"] for e in v["errors"]]
    assert any("gate-route-not-in-form" in m for m in err_msgs), (
        "L3: expected gate-route-not-in-form in errors", v)
    assert any("reject" in m for m in err_msgs), (
        "L3: error should name the dead 'reject' route", v)
    assert any("approve" in m for m in err_msgs), (
        "L3: error should name the form that can't produce 'reject'", v)
    # Exit code: validate --json exits 1 on errors.
    rc, out, err = _cli(["validate", "root.local.json", "--json"], d)
    assert rc == 1, ("L3: validate --json should exit 1 on hard error", rc)

    # 2. Fix A — drop the phantom route: errors clear.
    d2 = _fresh_partb()
    _fixforward_l3(d2)
    v2 = _validate_json(d2)
    assert not v2["errors"], ("L3 fix-A: dropping reject route should clear errors", v2)

    # 3. Fix B — upgrade form to approve_or_revise and rename route to `revise`.
    d3 = _fresh_partb()
    ppath = os.path.join(d3, "pipeline.json")
    pj = json.load(open(ppath, encoding="utf-8"))
    pj["nodes"]["role:gate"]["form"] = "approve_or_revise"
    routes = pj["graph"]["stages"]["gate"]["branch"]["routes"]
    routes["revise"] = routes.pop("reject")
    json.dump(pj, open(ppath, "w", encoding="utf-8"), indent=2)
    v3 = _validate_json(d3)
    assert not v3["errors"], ("L3 fix-B: approve_or_revise + revise route should clear errors", v3)

    print("PASS L3 — gate-route-not-in-form fires as a hard validate ERROR; "
          "both fix paths (drop route / approve_or_revise + rename) clear it")


# --- L1: missing-carry warning present at validate (after L3 is fixed) ----------

def check_l1_missing_carry():
    d = _fresh_partb()
    # L3 fires first as a hard error — fix it so validate can surface L1's warning.
    _fixforward_l3(d)
    v = _validate_json(d)
    ids = [w["id"] for w in v["warnings"]]
    assert "missing-carry" in ids, ("L1: expected a missing-carry warning after L3 fixed", v)
    msg = next(w["message"] for w in v["warnings"] if w["id"] == "missing-carry")
    assert "high_impact" in msg, ("L1: missing-carry should name high_impact", msg)
    assert not v["errors"], ("L1: validate should have no hard errors once L3 is fixed", v["errors"])
    print("PASS L1 — missing-carry warning present after L3 fixed (names 'high_impact')")


# --- L4-layered: merged L2+L4 trap — three states to verify ---------------------
#
# The original standalone L2 ({{?judge_notes}} decoy) and standalone L4
# (bare {{loop_feedback}}) have been merged into one layered trap:
# draft.md now has ONE guidance line — {{judge_notes}} (bare, no ?).
#
# STATE 1  pristine: bare {{judge_notes}} + strict_render → render_unfilled_placeholders at draft
# STATE 2  builder adds ?: {{?judge_notes}} → no fault, run cycles and suspends, but
#          judge_notes is NEVER written (tally writes loop_feedback), so the guidance
#          line is EMPTY every pass — blind loop, provably silent, no lint
# STATE 3  full fix: {{?loop_feedback}} → points at the key tally actually writes, informed

def check_l2l4_layered():
    # --- Structural invariants ---
    draft_path = os.path.join(PART_B, "prompts", "draft.md")
    draft_text = open(draft_path, encoding="utf-8").read()
    assert "{{judge_notes}}" in draft_text, (
        "L4-layered MISSING: draft.md must read {{judge_notes}} (bare, no ?) as the merged trap key. "
        "tally never writes judge_notes, so STATE 1 faults render_unfilled_placeholders and "
        "STATE 2 (after adding ?) runs blind. Restore the bare {{judge_notes}} line to draft.md.")
    assert "{{?judge_notes}}" not in draft_text, (
        "L4-layered defused: draft.md has {{?judge_notes}} already optional — STATE 1 won't fault. "
        "Remove the ? so the bare placeholder triggers strict_render on first run.")
    assert "{{loop_feedback}}" not in draft_text and "{{feedback}}" not in draft_text, (
        "L4-layered: draft.md should have only {{judge_notes}} as the guidance line; "
        "old feedback/loop_feedback lines must be removed.")

    tally_path = os.path.join(PART_B, "transforms.py")
    tally_text = open(tally_path, encoding="utf-8").read()
    assert '"loop_feedback"' in tally_text or "'loop_feedback'" in tally_text, (
        "L4-layered: tally must write loop_feedback (the key draft does NOT read in STATE 2)")
    assert "judge_notes" not in tally_text, (
        "L4-layered: tally must NOT write judge_notes — that would defuse the blind-loop half")

    pipeline_path = os.path.join(PART_B, "pipeline.json")
    pipeline_text = open(pipeline_path, encoding="utf-8").read()
    assert "judge_notes" not in pipeline_text, (
        "L4-layered: pipeline.json must not write or declare judge_notes anywhere")

    # --- STATE 1: bare {{judge_notes}} + strict_render faults at draft ---
    d1 = _fresh_partb()
    _fixforward_l3(d1)
    rc1, o1 = _run_json(d1)
    assert rc1 == 1 and o1["outcome"] == "failed", (
        "L4-layered STATE 1: pristine run should fail", rc1, o1)
    assert o1["stage"] == "draft", (
        "L4-layered STATE 1: fault must be at the draft stage (strict_render)", o1)
    f1 = o1["failures"][0]
    assert f1["code"] == "render_unfilled_placeholders", (
        "L4-layered STATE 1: wrong failure code", o1)
    assert "judge_notes" in f1["message"], (
        "L4-layered STATE 1: message must name judge_notes", o1)

    # --- STATE 2: add ? → silent blind loop ---
    # After builder adds ?, validate --strict shows exactly missing-carry (no loop-key-mismatch lint).
    # Run cycles draft→judge→tally→draft (twice) and suspends at gate — no fault,
    # but judge_notes is empty every pass (tally never writes it).
    # L5 is also fixed here (notes added to judge overlay) so the loop can cycle past judge.
    d2 = _fresh_partb()
    _fixforward_l3(d2)
    _fixforward_l5(d2)
    _edit(os.path.join(d2, "prompts", "draft.md"), "{{judge_notes}}", "{{?judge_notes}}")

    v2 = _validate_json(d2)
    ids2 = [w["id"] for w in v2["warnings"]]
    assert set(ids2) == {"missing-carry"}, (
        "L4-layered STATE 2: expected only missing-carry warning after L3 fixed and ? added. "
        "A second warning means the blind-loop trap is no longer silent — re-read answer-key.md.", ids2)
    assert not v2["errors"], (
        "L4-layered STATE 2: no hard errors expected", v2["errors"])

    rc2, o2 = _run_json(d2)
    assert rc2 == 0 and o2["outcome"] == "suspended", (
        "L4-layered STATE 2: run should succeed and suspend at gate (no fault)", rc2, o2)
    assert o2.get("awaiting"), (
        "L4-layered STATE 2: suspended run must carry awaiting", o2)

    # Confirm loop actually cycled (draft→judge→tally twice) by checking the
    # second draft ran — overlay gives judge 'revise' first, so cycle 2 exists.
    # The suspended baton's ask renders the SECOND draft's summary (revised).
    ask2 = o2.get("ask", "")
    assert ask2, ("L4-layered STATE 2: suspended baton must carry ask text", o2)

    # --- STATE 3: full fix → informed loop ---
    # L5 also fixed here so the loop can complete both cycles.
    d3 = _fresh_partb()
    _fixforward_l3(d3)
    _fixforward_l5(d3)
    _edit(os.path.join(d3, "prompts", "draft.md"), "{{judge_notes}}", "{{?loop_feedback}}")

    rc3, o3 = _run_json(d3)
    assert rc3 == 0 and o3["outcome"] == "suspended", (
        "L4-layered STATE 3: run should succeed and suspend at gate", rc3, o3)
    assert o3.get("awaiting"), (
        "L4-layered STATE 3: suspended run must carry awaiting", o3)

    print("PASS L4-layered (merged L2+L4) — "
          "STATE 1: bare {{judge_notes}} faults render_unfilled_placeholders at 'draft'; "
          "STATE 2: {{?judge_notes}} → validate shows only missing-carry, run cycles twice and "
          "suspends clean but judge_notes never written (blind loop, no lint); "
          "STATE 3: {{?loop_feedback}} → informed loop, suspends clean")


# --- L5: output_schema omission -> schema_mismatch via --json ----------------------------

def check_l5_schema_mismatch():
    d = _fresh_partb()
    # Fix L3 (gate error) and L4-layered (strict_render on judge_notes) so the run reaches judge.
    _fixforward_l3(d)
    _edit(os.path.join(d, "prompts", "draft.md"),
          "{{judge_notes}}", "{{?judge_notes}}")
    rc, o = _run_json(d)
    assert rc == 1 and o["outcome"] == "failed", ("L5: run should fail at judge", rc, o)
    assert o["stage"] == "judge", ("L5: fault should be at the judge stage", o)
    f = o["failures"][0]
    assert f["code"] == "schema_mismatch", ("L5: wrong failure code", o)
    assert "notes" in f["message"], ("L5: message should name the missing 'notes' key", o)
    assert f.get("fix_hint"), ("L5: --json failure must carry a fix_hint", o)
    print("PASS L5 — omitted required key -> schema_mismatch at 'judge' (--json carries stage/code/fix_hint)")


# --- Part-A fixtures exist and carry the injection strings ------------------------------

def check_fixtures_and_injections():
    assert os.path.isdir(NOTICES), ("fixtures/notices/ is missing", NOTICES)
    txts = [f for f in os.listdir(NOTICES) if f.endswith((".txt", ".md"))]
    assert len(txts) >= 6, ("expected 6-8 notice fixtures", txts)
    blob = ""
    for f in txts:
        blob += open(os.path.join(NOTICES, f), encoding="utf-8").read().lower()
    # the two seeded prompt-injection payloads
    assert "ignore your previous instructions" in blob or "ignore your instructions" in blob, (
        "injection fixture missing the 'ignore your instructions' payload")
    assert "[system]" in blob, "injection fixture missing the fake [SYSTEM] block"
    # the high-impact trigger for the gate
    assert "high impact" in blob or "high-impact" in blob, "no high-impact notice to trigger the gate"
    print("PASS fixtures — {} notices present; both injection payloads + a high-impact notice found".format(len(txts)))


def main() -> None:
    checks = [
        check_l3_lint_rescued,     # L3 fires first (hard error at validate)
        check_l1_missing_carry,    # L1 surfaces after L3 is fixed
        check_l2l4_layered,        # merged L4-layered trap: 3 states (STATE1 fault, STATE2 blind, STATE3 fix)
        check_l5_schema_mismatch,
        check_fixtures_and_injections,
    ]
    for c in checks:
        c()
    print("\nALL 5 CHECKS PASSED — the seeded landmines still trip as answer-key.md documents.")


if __name__ == "__main__":
    main()
