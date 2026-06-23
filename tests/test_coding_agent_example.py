"""B4 smoke test for the coding-agent worked example.

Validates that examples/coding-agent/ runs end-to-end against a fake_tool
provider scripted to mimic a successful bug-fix flow. The REAL tool
impls (read/edit/bash/done) execute against a per-test tmpdir copy of
the buggy fixture; the model decisions are scripted (no model calls,
no network) so the test is fast + deterministic + CI-safe.

What this test proves (after opus B4 review, narrowed):
- The example's pipeline.json + local.json load without error
- `call_target` resolves `fn:tools:edit_file` and the patch lands on disk
  (post-state assertions on the file content)
- The agent_loop walks all 5 scripted turns and exits cleanly with
  outcome="completed" (stdout assertion on the runtime's RESULT line)
- The accompanying test_is_fizzbuzz.py passes after the patch

What this test does NOT prove:
- That `read_file` and `run_bash` tool RESULTS round-trip back to the
  model — the fake_tool provider doesn't condition next-turn output on
  prior tool results. A regression that makes those tools always
  return errors would still ship the asserted post-state because the
  scripted edit-turn fires unconditionally. The honest verification
  for tool-result round-trip is via the real-claude smoke test
  documented in README.md.
- That a real model would make the right fix decisions (manual smoke
  test against claude.json — see examples/coding-agent/README.md)
- That the pipeline runs against claude_cli or litellm (the validate
  test below covers config shape only, not runtime wiring)

Run: cd yaah && python3 scripts/run_tests.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_DIR = os.path.join(REPO_ROOT, "examples", "coding-agent")
FIXTURE_BUGGY = os.path.join(EXAMPLE_DIR, "fixtures", "buggy_code")


def _scaffold_workdir(td: str) -> str:
    """Copy the buggy fixture into a tmpdir so the test owns the file state."""
    work = os.path.join(td, "work")
    shutil.copytree(FIXTURE_BUGGY, work)
    return work


def _write_local_for_workdir(td: str, work: str) -> str:
    """Generate a per-test local.json that overrides the input fixture's
    path to absolute (so the agent's tool calls land in the tmpdir). Copies
    the example's pipeline.json + prompts + tools.py into the tmpdir so the
    runtime resolves relative refs against the test's working tree."""
    # Copy the pipeline + prompts + tools into the test directory so
    # relative paths resolve from there.
    for name in ("pipeline.json", "tools.py", "prompts"):
        src = os.path.join(EXAMPLE_DIR, name)
        dst = os.path.join(td, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy(src, dst)
    # Write the input with the absolute work path so tool calls target this
    # tmpdir, not the repo fixture.
    inp_path = os.path.join(td, "input.json")
    with open(inp_path, "w") as f:
        json.dump({"goal": "Fix the bug in is_fizzbuzz.py — the test in test_is_fizzbuzz.py "
                           "currently fails. Read the file, find the wrong operator, edit it, "
                           "run the test to verify, then signal done.",
                   "workdir": work}, f)
    # Read the canonical local.json so the scripted-fix turns stay
    # co-located with the example; rewrite paths to absolute tmpdir.
    src_local = os.path.join(EXAMPLE_DIR, "local.json")
    with open(src_local) as f:
        local = json.load(f)
    # Rewrite scripted tool-call args to use the absolute tmpdir paths.
    # The local.json template uses {{WORK}} placeholder for the path the
    # test must substitute.
    raw = json.dumps(local)
    raw = raw.replace("{{WORK}}", work)
    raw = raw.replace('"input": "fixtures/input.json"', '"input": "input.json"')
    local = json.loads(raw)
    local_path = os.path.join(td, "local.json")
    with open(local_path, "w") as f:
        json.dump(local, f)
    return local_path


def test_coding_agent_example_fixes_bug_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        work = _scaffold_workdir(td)
        # Verify pre-state: the bug ('or' instead of 'and') is present;
        # the test currently FAILS.
        buggy_path = os.path.join(work, "is_fizzbuzz.py")
        with open(buggy_path) as f:
            pre = f.read()
        # The bug is in the return-statement boolean operator; the docstring may
        # legitimately mention 'and' so we check the operator specifically.
        assert "n % 3 == 0 or n % 5 == 0" in pre, \
            "fixture must start with the buggy 'or' operator; got:\n{}".format(pre)
        assert "n % 3 == 0 and n % 5 == 0" not in pre, \
            "fixture must not be pre-fixed"
        test_path = os.path.join(work, "test_is_fizzbuzz.py")
        pre_result = subprocess.run([sys.executable, test_path], capture_output=True)
        assert pre_result.returncode != 0, "fixture's test must FAIL before fix"

        # Run the example pipeline (fake_tool provider; tools dispatched to real impls).
        local_path = _write_local_for_workdir(td, work)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
        # The example's tools.py needs to be importable. Add the test workdir
        # to PYTHONPATH so 'fn:tools:read_file' resolves to the copy beside
        # the pipeline config.
        env["PYTHONPATH"] = td + os.pathsep + env["PYTHONPATH"]
        result = subprocess.run(
            [sys.executable, "-m", "yaah.runtime", local_path],
            cwd=td, env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, "pipeline must exit 0; got {}\nSTDOUT:\n{}\nSTDERR:\n{}".format(
            result.returncode, result.stdout, result.stderr)

        # Agent_loop must have walked all 5 scripted turns and exited cleanly.
        # The runtime prints the final envelope on stdout; agent_loop's payload
        # carries {turns, outcome}. Without these assertions, a regression that
        # bailed early (e.g. empty_response on turn 2, or max_turns_exhausted
        # below 5) could ship green if it happened to leave the file edited.
        assert "'outcome': 'completed'" in result.stdout, \
            "expected outcome='completed' in stdout; got:\n{}".format(result.stdout)
        assert "'turns': 5" in result.stdout, \
            "expected turns=5 (the scripted-turn count); got:\n{}".format(result.stdout)

        # Post-state: the bug is fixed; the test now PASSES.
        with open(buggy_path) as f:
            post = f.read()
        assert "n % 3 == 0 and n % 5 == 0" in post, \
            "expected fixed 'and' operator in patched file; got:\n{}".format(post)
        assert "n % 3 == 0 or n % 5 == 0" not in post, \
            "the buggy 'or' operator should be gone"
        post_result = subprocess.run([sys.executable, test_path], capture_output=True)
        assert post_result.returncode == 0, "test should PASS after fix; got rc={}\nSTDOUT:\n{}".format(
            post_result.returncode, post_result.stdout.decode())


def test_pipeline_native_and_claude_json_validate():
    """The claude variant uses pipeline_native.json (single `agent` node with
    no YAAH tools, claude uses its own --allowedTools loop). End-to-end run
    needs a real claude binary and ~$0.01; CI just validates the config.

    This test catches: misconfigured pipeline.json/local.json shape; missing
    keys; bad provider name; broken extends. The real fix-bug-end-to-end
    smoke test against claude is documented in examples/coding-agent/README.md.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(REPO_ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    for root in ("claude.json",):
        result = subprocess.run(
            [sys.executable, "-m", "yaah.runtime", "validate",
             os.path.join(EXAMPLE_DIR, root)],
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, "validate {} failed (rc={}): {}{}".format(
            root, result.returncode, result.stdout, result.stderr)


if __name__ == "__main__":
    test_coding_agent_example_fixes_bug_end_to_end()
    test_pipeline_native_and_claude_json_validate()
    print("OK")
