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


# --- L1: missing-carry warning present at validate --------------------------------------

def check_l1_missing_carry():
    d = _fresh_partb()
    v = _validate_json(d)
    ids = [w["id"] for w in v["warnings"]]
    assert "missing-carry" in ids, ("L1: expected a missing-carry warning", v)
    msg = next(w["message"] for w in v["warnings"] if w["id"] == "missing-carry")
    assert "high_impact" in msg, ("L1: missing-carry should name high_impact", msg)
    assert not v["errors"], ("L1: validate should have no hard errors", v["errors"])
    print("PASS L1 — missing-carry warning present (names 'high_impact')")


# --- L2: loop-key mismatch is SILENT (no lint) ------------------------------------------

def check_l2_silent():
    d = _fresh_partb()
    v = _validate_json(d)
    ids = [w["id"] for w in v["warnings"]]
    # The only strict-blocking lint on the pristine project is L1. If a NEW lint
    # ever fires (e.g. a loop-key-mismatch rule), L2 stops being doc-only and the
    # answer key must change — so we assert the warning set is exactly {missing-carry}.
    assert set(ids) == {"missing-carry"}, (
        "L2: expected the only lint to be missing-carry (L2 is doc-only). "
        "A new lint here may mean L2 is no longer silent — re-read answer-key.md.", ids)
    print("PASS L2 — loop-key mismatch is silent (no lint beyond L1)")


# --- L3: dead route is silent at validate; form IS enforced on resume -------------------

def _fixforward_to_gate(d):
    """Fix-forward L1/L4/L5 in a throwaway part-b so a run REACHES and parks at
    the `gate` stage — leaving L3 (approve form + phantom reject route) intact.
    Returns the parked baton id."""
    # L1: declare classify output; L4: make {{loop_feedback}} optional in draft;
    # L5: give the judge fake reply its required `notes` key.
    ppath = os.path.join(d, "pipeline.json")
    pj = json.load(open(ppath, encoding="utf-8"))
    pj["nodes"]["role:classify"]["output_schema"] = {
        "type": "object",
        "properties": {"impact_area": {"type": "string"}, "high_impact": {"type": "boolean"}},
        "required": ["impact_area", "high_impact"],
    }
    # sanity: the gate really is the approve-form + reject-route shape L3 describes
    assert pj["nodes"]["role:gate"]["form"] == "approve", "L3 setup: gate form drifted"
    assert "reject" in pj["graph"]["stages"]["gate"]["branch"]["routes"], "L3 setup: reject route gone"
    json.dump(pj, open(ppath, "w", encoding="utf-8"), indent=2)
    _edit(os.path.join(d, "prompts", "draft.md"), "{{loop_feedback}}", "{{?loop_feedback}}")
    # the judge fake reply is a JSON STRING inside root.local.json, so its quotes
    # are backslash-escaped on disk — match the escaped form to add `notes` (L5).
    _edit(os.path.join(d, "root.local.json"),
          '{\\"verdict\\": \\"pass\\"}', '{\\"verdict\\": \\"pass\\", \\"notes\\": \\"ok\\"}')
    rc, out, err = _cli(["run", "root.local.json", "--json"], d)
    o = json.loads(out)
    assert o["outcome"] == "suspended", ("L3 setup: run should park at the gate", rc, o, err)
    return o["baton_id"]


def check_l3_form_enforced_on_resume():
    d = _fresh_partb()
    # 1. Silent at VALIDATE — the phantom reject route trips no lint (unchanged).
    ppath = os.path.join(d, "pipeline.json")
    pj = json.load(open(ppath, encoding="utf-8"))
    pj["nodes"]["role:classify"]["output_schema"] = {
        "type": "object",
        "properties": {"impact_area": {"type": "string"}, "high_impact": {"type": "boolean"}},
        "required": ["impact_area", "high_impact"],
    }
    json.dump(pj, open(ppath, "w", encoding="utf-8"), indent=2)
    v = _validate_json(d)
    ids = [w["id"] for w in v["warnings"]]
    assert not v["errors"], ("L3: validate should not hard-error on the dead route", v["errors"])
    assert "gate-decision-ignored" not in ids, (
        "L3: gate-decision-ignored fires for the OPPOSITE shape and must NOT fire here", ids)
    assert not any("reject" in w["message"] for w in v["warnings"]), (
        "L3 REGRESSION: a lint now flags the dead reject route — update answer-key.md", v["warnings"])

    # 2. LOUD at RESUME — the `approve` form is now BINDING (N1 enforcement). The
    #    reject route is only reachable by a decision the form forbids, and the
    #    engine now REJECTS it instead of silently taking the dead route.
    d2 = _fresh_partb()
    baton = _fixforward_to_gate(d2)
    with open(os.path.join(d2, "decision.json"), "w", encoding="utf-8") as f:
        json.dump({"decision": "reject"}, f)
    rc, out, err = _cli(["resume", "root.local.json", baton, "decision.json", "--json"], d2)
    assert rc == 1, ("L3: a form-violating decision must exit 1", rc, out, err)
    o = json.loads(out)
    assert o["outcome"] == "failed" and o["code"] == "decision_rejected", ("L3: wrong shape", o)
    f0 = o["failures"][0]
    assert f0["data"]["form"] == "approve" and f0["data"]["errors"], ("L3: data slot", f0)
    assert "baton-schema" in f0["message"] and "strict_resume" in f0["message"], (
        "L3: message must carry BOTH remedies", f0)

    # 3. The gate stays PARKED and re-submittable; the conforming `approve`
    #    decision then completes the run (the trap's fix is to send `approve`).
    rc2, out2, _ = _cli(["list", "root.local.json", "--json"], d2)
    assert len(json.loads(out2)["batons"]) == 1, "L3: rejected decision must NOT evict the baton"
    with open(os.path.join(d2, "decision.json"), "w", encoding="utf-8") as f:
        json.dump({"decision": "approve"}, f)
    rc3, out3, err3 = _cli(["resume", "root.local.json", baton, "decision.json", "--json"], d2)
    assert rc3 == 0 and json.loads(out3)["outcome"] == "done", ("L3: approve should complete", rc3, out3, err3)
    print("PASS L3 — approve-form silent at validate, but LOUD (decision_rejected) at resume; "
          "gate stays resumable and `approve` completes")


# --- L4: strict_render + bare {{loop_feedback}} faults on first run ----------------------

def check_l4_strict_render_fault():
    d = _fresh_partb()
    # Fix L1 so validate doesn't gate the run path (run works regardless, but keep it clean).
    rc, o = _run_json(d)
    assert rc == 1 and o["outcome"] == "failed", ("L4: first run should fail", rc, o)
    assert o["stage"] == "draft", ("L4: fault should be at the draft stage", o)
    f = o["failures"][0]
    assert f["code"] == "render_unfilled_placeholders", ("L4: wrong failure code", o)
    assert "loop_feedback" in f["message"], ("L4: message should name loop_feedback", o)
    print("PASS L4 — strict_render faults render_unfilled_placeholders on {{loop_feedback}} at 'draft'")


# --- L5: output_schema omission -> schema_mismatch via --json ----------------------------

def check_l5_schema_mismatch():
    d = _fresh_partb()
    # Fix-forward past L4 only (make the loop_feedback placeholder optional), so the
    # run reaches judge where the fake overlay omits the required 'notes' key.
    _edit(os.path.join(d, "prompts", "draft.md"),
          "{{loop_feedback}}", "{{?loop_feedback}}")
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
        check_l1_missing_carry,
        check_l2_silent,
        check_l3_form_enforced_on_resume,
        check_l4_strict_render_fault,
        check_l5_schema_mismatch,
        check_fixtures_and_injections,
    ]
    for c in checks:
        c()
    print("\nALL CHECKS PASSED — the seeded landmines still trip as answer-key.md documents.")


if __name__ == "__main__":
    main()
