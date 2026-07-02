"""End-to-end offline test for examples/author/ — the authoring meta-pipeline.

Runs the .fake overlay (scripted drafts, auto-approved gate) in a tmpdir copy
of the example and proves the REPAIR LOOP repairs:

  - the scripted draft #1 is INVALID (typo'd `then` target) — asserted here by
    running validate_pipeline on the fixture itself (falsification guard: if
    the fixture drifts valid, the test fails instead of blessing a no-op loop);
  - the run still exits 0 and WRITES a config, so some later attempt passed;
  - the trace records TWO model_call spans for the draft stage — the retry
    fired (one attempt could not have produced a valid artifact, see above);
  - the WRITTEN pipeline/root are valid per the engine's own
    validate_pipeline/validate_root, and the typo'd target is gone.

What this does NOT prove: that a real model converges in <= max_attempts, or
that assets the drafted config references (prompts/templates) exist — both
documented limitations in examples/author/README.md.

Run: cd yaah && PYTHONPATH=src python3 tests/test_author_example.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from yaah.validate import validate_pipeline, validate_root  # noqa: E402

EXAMPLE_DIR = os.path.join(REPO_ROOT, "examples", "author")
COPIED = ("author-pipeline.json", "author.local.json", "author.fake.json",
          "transforms.py", "prompts", "fixtures")


def _copy_example(td: str) -> None:
    for name in COPIED:
        src = os.path.join(EXAMPLE_DIR, name)
        dst = os.path.join(td, name)
        (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, dst)


def _fixture_drafts() -> "list[dict]":
    with open(os.path.join(EXAMPLE_DIR, "fixtures", "draft.fake.json")) as f:
        scripted = json.load(f)
    (replies,) = scripted.values()  # one scripted model
    return [json.loads(r) for r in replies]


def test_fixture_attempt1_is_invalid_and_attempt2_valid() -> None:
    """Falsification guard on the fixtures themselves: the repair-loop claim is
    only meaningful if draft #1 REALLY fails the same validator the pipeline
    runs, and draft #2 really passes."""
    drafts = _fixture_drafts()
    assert len(drafts) == 2, "expected exactly two scripted drafts, got {}".format(len(drafts))
    try:
        validate_pipeline(drafts[0]["pipeline"])
        raise AssertionError("scripted draft #1 must FAIL validate_pipeline (fixture drifted valid?)")
    except ValueError as e:
        assert "reviw" in str(e), "draft #1 must fail on the typo'd then-target; got: {}".format(e)
    validate_pipeline(drafts[1]["pipeline"])  # raises -> test fails
    validate_root(drafts[1]["root"])


def test_author_fake_run_repairs_and_writes_valid_config() -> None:
    with tempfile.TemporaryDirectory() as td:
        _copy_example(td)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "yaah.runtime", os.path.join(td, "author.fake.json")],
            cwd=td, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            "pipeline must exit 0; got {}\nSTDOUT:\n{}\nSTDERR:\n{}".format(
                result.returncode, result.stdout, result.stderr))

        # The repair loop fired: attempt 1 (invalid) + attempt 2 (valid) = two model calls.
        trace_path = os.path.join(td, "trace.jsonl")
        assert os.path.exists(trace_path), "fake overlay declares a file trace sink"
        with open(trace_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        model_calls = [r for r in records if r.get("name") == "model_call"]
        assert len(model_calls) == 2, (
            "expected exactly 2 model_call spans (invalid draft + repaired draft); "
            "got {}:\n{}".format(len(model_calls), model_calls))
        # ...and the retry was caused by the VALIDATOR rejecting attempt 1: the
        # draft stage traces error (rejected) then ok (repaired), in that order.
        draft_statuses = [r["status"] for r in records
                          if r.get("name") == "stage" and r.get("stage") == "draft"]
        assert draft_statuses == ["error", "ok"], (
            "expected draft stage spans [error, ok] (attempt 1 rejected by "
            "check_config, attempt 2 passed); got {}".format(draft_statuses))

        # The written artifact exists and is VALID per the engine's own validator.
        gen = os.path.join(td, "generated")
        root_path = os.path.join(gen, "summarize-review.json")
        pipeline_path = os.path.join(gen, "summarize-review-pipeline.json")
        for p in (root_path, pipeline_path):
            assert os.path.exists(p), "expected written config {}; generated/ has: {}".format(
                p, os.listdir(gen) if os.path.isdir(gen) else "<missing>")
        with open(pipeline_path) as f:
            written_pipeline = json.load(f)
        validate_pipeline(written_pipeline)  # raises ValueError -> test fails
        with open(root_path) as f:
            written_root = json.load(f)
        validate_root(written_root)
        # coherence: the written root points at the written pipeline file
        assert written_root["pipeline"] == os.path.basename(pipeline_path)

        # The specific repair landed: the typo'd target from draft #1 is gone,
        # replaced by the resolvable stage ref (so the VALID draft, not the
        # invalid one, is what got written).
        assert written_pipeline["graph"]["stages"]["summarize"]["then"] == "review"
        with open(pipeline_path) as f:
            assert "reviw\"" not in f.read(), "the invalid draft's typo leaked into the artifact"


def main() -> None:
    test_fixture_attempt1_is_invalid_and_attempt2_valid()
    test_author_fake_run_repairs_and_writes_valid_config()
    print("test_author_example: PASS (2 scenarios)")


if __name__ == "__main__":
    main()
