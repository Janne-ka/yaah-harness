"""test_authoring_eval — the authoring-eval harness's loop mechanics, offline.

The eval script (scripts/authoring_eval.py) wraps a nondeterministic author
model in a DETERMINISTIC harness; these tests attack the harness with the
scripted double so CI proves the loop without any model:

- the built-in scripted run lands EVERY task valid, and the one task whose
  first draft is broken takes exactly one repair round — driven by the real
  validate_config diagnostics, not by the double peeking at the harness;
- the repair-round prompt actually CARRIES the diagnostics back to the author
  (the loop's whole point — assert the text, not just the count);
- a first reply that is unparseable prose — or a bare JSON `null`, which
  extract_json returns WITHOUT raising — is survived: it becomes a diagnostic
  round with a FRESH message, not a crash or a stale-message repeat;
- rows finished before an author crash are already on disk (real mode pays
  per reply; a buffered-write harness would discard them);
- an author that never produces a valid config exhausts max_rounds and the
  row says so (valid=False, final_diagnostics non-empty);
- the JSONL rows carry exactly the declared fields, the summary counts are
  right, and two runs are byte-identical (determinism is the design claim).

Run: cd yaah && PYTHONPATH=src python3 tests/test_authoring_eval.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "authoring_eval.py")

_spec = importlib.util.spec_from_file_location("authoring_eval", SCRIPT)
authoring_eval = importlib.util.module_from_spec(_spec)
# registering BEFORE exec is the full importlib recipe — @dataclass resolves
# its owning module through sys.modules at class-creation time
sys.modules["authoring_eval"] = authoring_eval
_spec.loader.exec_module(authoring_eval)

TMP = tempfile.mkdtemp(prefix="authoring-eval-test-")

# the tests exercise SCRIPTED mode only — never let an ambient env flag flip
# main() into spending money on a real model from inside the test suite
os.environ.pop("YAAH_AUTHORING_EVAL_MODEL", None)

ROW_FIELDS = {"task", "mode", "valid", "rounds", "round_details",
              "warnings", "final_diagnostics"}
ROUND_FIELDS = {"round", "parse_ok", "errors", "prompt_chars", "reply_chars"}


def _run_default(out_name: str):
    author = authoring_eval.default_scripted_author()
    out_path = os.path.join(TMP, out_name)
    rows, summary = authoring_eval.run_eval(author, mode="scripted", out_path=out_path)
    return author, out_path, rows, summary


def scripted_run_lands_every_task_valid() -> None:
    _author, _path, rows, summary = _run_default("all-valid.jsonl")
    assert len(rows) == len(authoring_eval.TASKS), "one row per built-in task"
    for row in rows:
        assert row["valid"] is True, "task {!r} never landed valid: {}".format(
            row["task"], row["final_diagnostics"])
    assert summary["tasks"] == len(rows)
    assert summary["valid"] == len(rows)
    assert summary["validity_rate"] == 1.0


def broken_first_draft_takes_exactly_one_repair_round() -> None:
    _author, _path, rows, summary = _run_default("repair.jsonl")
    by_task = {r["task"]: r for r in rows}
    repaired = by_task[authoring_eval.REPAIR_TASK_ID]
    assert repaired["rounds"] == 2, "broken draft must cost exactly one repair"
    first = repaired["round_details"][0]
    assert first["parse_ok"] is True, "the broken draft is valid JSON, invalid CONFIG"
    assert first["errors"] >= 1
    assert repaired["round_details"][1]["errors"] == 0
    # every other task lands on round 1
    for row in rows:
        if row["task"] != authoring_eval.REPAIR_TASK_ID:
            assert row["rounds"] == 1, "task {!r} unexpectedly needed repair".format(row["task"])
    # summary aggregates match: mean rounds over N tasks, one of which took 2
    n = len(rows)
    assert summary["mean_rounds_to_valid"] == (n + 1) / n


def repair_prompt_carries_the_real_diagnostics() -> None:
    author, _path, _rows, _summary = _run_default("diags.jsonl")
    repair_requests = [r for r in author.requests
                       if r.task_id == authoring_eval.REPAIR_TASK_ID]
    assert len(repair_requests) == 2
    first, second = repair_requests
    assert first.diagnostics is None, "round 0 must be cold — no diagnostics"
    assert second.diagnostics, "repair round must receive diagnostics"
    # the diagnostics are validate_config's own, structured by split_diagnostics
    messages = [d["message"] for d in second.diagnostics]
    assert any("reprot" in m for m in messages), \
        "diagnostic must name the bogus target: {}".format(messages)
    assert any(d.get("stage") == "extract" for d in second.diagnostics), \
        "split_diagnostics stage extraction lost: {}".format(second.diagnostics)
    # and they are IN the prompt the author sees — the loop feeds back text
    assert "reprot" in second.prompt
    assert first.prompt != second.prompt


def unparseable_reply_becomes_a_repair_round() -> None:
    good = authoring_eval.default_scripted_author()
    valid_reply = good.scripts["summarize-and-render"][-1]
    task = next(t for t in authoring_eval.TASKS if t.id == "summarize-and-render")
    author = authoring_eval.ScriptedAuthor({
        task.id: ["Sorry, I can not produce a config for that.", valid_reply]})
    out_path = os.path.join(TMP, "prose.jsonl")
    rows, summary = authoring_eval.run_eval(
        author, mode="scripted", out_path=out_path, tasks=[task])
    row = rows[0]
    assert row["valid"] is True
    assert row["rounds"] == 2
    assert row["round_details"][0]["parse_ok"] is False
    # the parse failure is fed back as a diagnostic, same channel as validate errors
    second = author.requests[1]
    assert second.diagnostics and any(
        "JSON" in d["message"] for d in second.diagnostics), second.diagnostics


def null_reply_is_a_diagnostic_not_a_crash() -> None:
    # extract_json("null") returns None WITHOUT raising (null is legitimate
    # JSON) — the non-dict path must not depend on the except branch having
    # run (adversarial review RED-1: UnboundLocalError on a bare-null reply)
    good = authoring_eval.default_scripted_author()
    valid_reply = good.scripts["summarize-and-render"][-1]
    task = next(t for t in authoring_eval.TASKS if t.id == "summarize-and-render")
    author = authoring_eval.ScriptedAuthor({task.id: ["null", valid_reply]})
    rows, _summary = authoring_eval.run_eval(
        author, mode="scripted", out_path=os.path.join(TMP, "null.jsonl"),
        tasks=[task])
    row = rows[0]
    assert row["valid"] is True
    assert row["round_details"][0]["parse_ok"] is False
    diags = author.requests[1].diagnostics
    assert diags and "OBJECT" in diags[0]["message"], diags


def null_after_prose_gets_a_fresh_message_not_a_stale_one() -> None:
    # the silent variant of the same bug: prose on round 0 sets the parse
    # message, null on round 1 must NOT reuse it — the model would be told
    # its parseable reply "did not contain parseable JSON"
    good = authoring_eval.default_scripted_author()
    valid_reply = good.scripts["summarize-and-render"][-1]
    task = next(t for t in authoring_eval.TASKS if t.id == "summarize-and-render")
    author = authoring_eval.ScriptedAuthor({
        task.id: ["no config, sorry", "null", valid_reply]})
    rows, _summary = authoring_eval.run_eval(
        author, mode="scripted", out_path=os.path.join(TMP, "stale.jsonl"),
        tasks=[task])
    assert rows[0]["valid"] is True
    round2_diags = author.requests[2].diagnostics
    assert round2_diags and "not as an OBJECT" in round2_diags[0]["message"], \
        "round-1 null reply got a stale round-0 message: {}".format(round2_diags)


def rows_written_so_far_survive_an_author_crash() -> None:
    # real mode pays per reply — a provider blow-up on task N must not discard
    # the N-1 finished rows (JSONL is written incrementally, not buffered)
    good = authoring_eval.default_scripted_author()

    def author(request):
        if request.task_id != "summarize-and-render":
            raise RuntimeError("provider fell over")
        return good(request)

    out_path = os.path.join(TMP, "crash.jsonl")
    tasks = [t for t in authoring_eval.TASKS
             if t.id in ("summarize-and-render", "gate-branch")]
    try:
        authoring_eval.run_eval(author, mode="scripted", out_path=out_path,
                                tasks=tasks)
    except RuntimeError:
        pass
    else:
        raise AssertionError("the author's crash must propagate, not be eaten")
    with open(out_path, "r", encoding="utf-8") as f:
        survived = [json.loads(line) for line in f if line.strip()]
    assert len(survived) == 1 and survived[0]["task"] == "summarize-and-render", \
        "finished rows must be on disk despite the crash: {}".format(survived)


def never_valid_exhausts_max_rounds_and_says_so() -> None:
    task = next(t for t in authoring_eval.TASKS if t.id == "summarize-and-render")
    hopeless = '{"pipeline": {"nodes": {}, "graph": {}}, "not_a_root_key": 1}'
    author = authoring_eval.ScriptedAuthor({task.id: [hopeless] * 3})
    out_path = os.path.join(TMP, "hopeless.jsonl")
    rows, summary = authoring_eval.run_eval(
        author, mode="scripted", out_path=out_path, tasks=[task], max_rounds=3)
    row = rows[0]
    assert row["valid"] is False
    assert row["rounds"] == 3
    assert row["final_diagnostics"], "a failed task must keep its last diagnostics"
    assert summary["valid"] == 0
    assert summary["validity_rate"] == 0.0
    assert summary["mean_rounds_to_valid"] is None


def trivial_valid_root_does_not_count_as_authored() -> None:
    # `{}` IS a valid root config (no pipeline key -> nothing to validate); the
    # harness must enforce the eval's own output contract or a lazy model
    # scores 100% by replying with an empty object
    task = next(t for t in authoring_eval.TASKS if t.id == "summarize-and-render")
    author = authoring_eval.ScriptedAuthor({task.id: ["{}"] * 2})
    rows, _summary = authoring_eval.run_eval(
        author, mode="scripted", out_path=os.path.join(TMP, "trivial.jsonl"),
        tasks=[task], max_rounds=2)
    row = rows[0]
    assert row["valid"] is False, "an empty root must NOT count as authored"
    assert any("pipeline" in d["message"] for d in row["final_diagnostics"]), \
        row["final_diagnostics"]


def scripted_author_exhaustion_is_loud() -> None:
    task = next(t for t in authoring_eval.TASKS if t.id == "summarize-and-render")
    author = authoring_eval.ScriptedAuthor({task.id: ["{}"]})  # 1 reply, needs more
    try:
        authoring_eval.run_eval(author, mode="scripted",
                                out_path=os.path.join(TMP, "exhausted.jsonl"),
                                tasks=[task])
    except RuntimeError as e:
        assert "summarize-and-render" in str(e)
    else:
        raise AssertionError("running out of canned replies must raise, not mask")


def jsonl_rows_carry_the_declared_fields() -> None:
    _author, out_path, rows, _summary = _run_default("fields.jsonl")
    with open(out_path, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == len(authoring_eval.TASKS)
    for row in lines:
        assert set(row) == ROW_FIELDS, "row fields drifted: {}".format(sorted(row))
        assert row["mode"] == "scripted"
        for detail in row["round_details"]:
            assert set(detail) == ROUND_FIELDS, \
                "round fields drifted: {}".format(sorted(detail))
            assert detail["prompt_chars"] > 0
            assert detail["reply_chars"] > 0
    assert lines == rows, "file rows and returned rows must be the same data"


def two_runs_are_byte_identical() -> None:
    _a, path_a, _r, _s = _run_default("det-a.jsonl")
    _b, path_b, _r2, _s2 = _run_default("det-b.jsonl")
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        assert fa.read() == fb.read(), "the scripted eval must be deterministic"


def main_scripted_mode_exits_zero() -> None:
    out_path = os.path.join(TMP, "main.jsonl")
    code = authoring_eval.main(["--out", out_path])
    assert code == 0, "scripted CI mode must exit 0 when every task lands valid"
    assert os.path.exists(out_path)


def main() -> None:
    scripted_run_lands_every_task_valid()
    broken_first_draft_takes_exactly_one_repair_round()
    repair_prompt_carries_the_real_diagnostics()
    unparseable_reply_becomes_a_repair_round()
    null_reply_is_a_diagnostic_not_a_crash()
    null_after_prose_gets_a_fresh_message_not_a_stale_one()
    rows_written_so_far_survive_an_author_crash()
    never_valid_exhausts_max_rounds_and_says_so()
    trivial_valid_root_does_not_count_as_authored()
    scripted_author_exhaustion_is_loud()
    jsonl_rows_carry_the_declared_fields()
    two_runs_are_byte_identical()
    main_scripted_mode_exits_zero()
    print("PASS test_authoring_eval")


if __name__ == "__main__":
    main()
