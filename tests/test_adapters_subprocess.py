"""Unit tests for the subprocess adapters (ClaudeCliBackend, GitDiffSource), via
an INJECTED fake process spawner.

Both adapters take `spawn` (defaulting to asyncio.create_subprocess_exec) so a
test can assert the exact argv they build and exercise the exit-code and
timeout->kill paths without a real `claude`/`git` binary.

Run: cd yaah && PYTHONPATH=src python3 tests/test_adapters_subprocess.py
"""
from __future__ import annotations

import asyncio

from yaah.adapters.backends import ClaudeCliBackend
from yaah.adapters.data import GitDiffSource


class FakeProc:
    def __init__(self, *, returncode=0, stdout=b"", stderr=b"",
                 raise_timeout=False, wait_raises=None):
        self.returncode = returncode
        self._stdout, self._stderr = stdout, stderr
        self._raise_timeout = raise_timeout
        self._wait_raises = wait_raises
        self.killed = self.waited = False
        self.stdin_data = None

    async def communicate(self, data=None):
        self.stdin_data = data
        if self._raise_timeout:
            raise asyncio.TimeoutError
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        if self._wait_raises:
            raise self._wait_raises


def spawner(procs, captured):
    """Return an async spawn that records every argv/kwargs and yields procs in
    order (last one repeats if exhausted)."""
    seq = list(procs)

    async def spawn(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        return seq[min(len(captured) - 1, len(seq) - 1)]

    return spawn


# ---- ClaudeCliBackend -------------------------------------------------------

async def claude_complete_builds_argv_pipes_prompt_returns_stdout() -> None:
    calls = []
    proc = FakeProc(stdout=b"ANSWER")
    be = ClaudeCliBackend(spawn=spawner([proc], calls))
    out = await be.complete("do it", model="claude-x", cwd="/work")

    assert out == "ANSWER"
    argv = calls[0]["args"]
    assert argv[0] == "claude" and "-p" in argv
    assert "--model" in argv and argv[argv.index("--model") + 1] == "claude-x"
    assert calls[0]["kwargs"]["cwd"] == "/work"     # per-run worktree threaded through
    assert proc.stdin_data == b"do it"              # prompt piped on stdin, encoded


async def claude_cost_bridge_parses_json_usage() -> None:
    # L8: with on_usage (cost capture on), claude is asked for --output-format json;
    # complete() extracts `result` and feeds summed token usage to the bridge.
    import json as _json
    calls, usage = [], {}
    blob = _json.dumps({"result": "THE ANSWER", "model": "claude-sonnet",
                        "usage": {"input_tokens": 100, "cache_read_input_tokens": 20,
                                  "cache_creation_input_tokens": 5, "output_tokens": 30}})
    be = ClaudeCliBackend(spawn=spawner([FakeProc(stdout=blob.encode())], calls))
    out = await be.complete("hi", model="m", on_usage=usage.update)

    assert out == "THE ANSWER"                                  # result text, not the JSON
    argv = calls[0]["args"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert usage == {"tokens_in": 125, "tokens_out": 30, "model": "claude-sonnet"}  # summed


async def claude_cost_path_missing_result_returns_empty() -> None:
    # assessment cluster 3 B6: previously the cost path returned `raw` unchanged
    # if the parsed JSON had no `result` field — so the downstream stage received
    # the WHOLE JSON envelope as the agent's text and silently poisoned the run.
    # Now: a parseable JSON without `result` yields "" (well-defined contract).
    import json as _json
    blob = _json.dumps({"model": "claude-sonnet",
                        "usage": {"input_tokens": 10, "output_tokens": 5}})  # no result
    usage = {}
    be = ClaudeCliBackend(spawn=spawner([FakeProc(stdout=blob.encode())], []))
    out = await be.complete("hi", model="m", on_usage=usage.update)
    assert out == ""                                      # NOT the JSON envelope
    assert usage["tokens_in"] == 10 and usage["tokens_out"] == 5  # usage still extracted


async def claude_cost_path_non_json_passes_through() -> None:
    # if claude failed to honor --output-format json (returned plain text), pass
    # it through so the run still has SOMETHING — no surprise on a misbehaving CLI.
    be = ClaudeCliBackend(spawn=spawner([FakeProc(stdout=b"not json at all")], []))
    out = await be.complete("hi", model="m", on_usage=lambda u: None)
    assert out == "not json at all"


async def claude_no_cost_no_json_format() -> None:
    # without on_usage, the plain text path is unchanged (no --output-format json)
    calls = []
    be = ClaudeCliBackend(spawn=spawner([FakeProc(stdout=b"plain text")], calls))
    out = await be.complete("hi", model="m")
    assert out == "plain text" and "--output-format" not in calls[0]["args"]


async def claude_nonzero_exit_raises_with_stderr() -> None:
    calls = []
    be = ClaudeCliBackend(spawn=spawner([FakeProc(returncode=2, stderr=b"boom")], calls))
    try:
        await be.complete("x")
        raise AssertionError("expected RuntimeError on non-zero exit")
    except RuntimeError as e:
        assert "exit 2" in str(e) and "boom" in str(e), e


async def claude_timeout_kills_process_and_reraises() -> None:
    calls = []
    proc = FakeProc(raise_timeout=True)
    be = ClaudeCliBackend(spawn=spawner([proc], calls))
    try:
        await be.complete("x", timeout=0.01)
        raise AssertionError("expected TimeoutError")
    except asyncio.TimeoutError:
        pass
    assert proc.killed and proc.waited, "a timed-out process must be killed and reaped"


async def claude_build_args_covers_mcp_perm_and_tools() -> None:
    # _build_args is pure; assert each config branch shapes the argv.
    be = ClaudeCliBackend(permission_mode="acceptEdits", allowed_tools=["Read", "Edit"])
    args = be._build_args("m", {"mcp": {"srv": {"command": "x"}}})
    assert "--strict-mcp-config" in args and "--mcp-config" in args
    assert "--permission-mode" in args and "--allowedTools" in args
    assert args[args.index("--allowedTools") + 1] == "Read,Edit"

    # no mcp + strip_mcp default -> empty servers config
    bare = ClaudeCliBackend()._build_args(None, {})
    i = bare.index("--mcp-config")
    assert bare[i + 1] == '{"mcpServers":{}}'
    assert "--model" not in bare  # model None -> omitted


# ---- GitDiffSource ----------------------------------------------------------

async def git_diff_builds_argv_with_ref_paths_and_context() -> None:
    calls = []
    src = GitDiffSource(spawn=spawner([FakeProc(stdout=b"DIFF")], calls))
    out = await src.fetch("HEAD", cwd="/repo", context=0, paths=["a.py", "b.py"])

    assert out == "DIFF"
    assert calls[0]["args"] == ("git", "-C", "/repo", "diff", "--unified=0",
                                "HEAD", "--", "a.py", "b.py")


async def git_diff_intent_to_add_runs_add_first() -> None:
    calls = []
    src = GitDiffSource(intent_to_add=True,
                        spawn=spawner([FakeProc(), FakeProc(stdout=b"D")], calls))
    await src.fetch(cwd="/r")
    assert calls[0]["args"] == ("git", "-C", "/r", "add", "-N", "-A")   # add first
    assert calls[1]["args"] == ("git", "-C", "/r", "diff", "--unified=3")  # then diff (default ctx)


async def git_diff_rejects_leading_dash_in_key() -> None:
    # assessment cluster 5 #4: a key like "-rf" used to reach `git diff` as a
    # flag (no shell, so no command injection — but git interprets it as an
    # option = flag smuggling). Refs/ranges never legitimately start with '-'.
    src = GitDiffSource(spawn=spawner([FakeProc(stdout=b"x")], []))
    try:
        await src.fetch("-rf", cwd="/r")
    except ValueError as e:
        assert "flag-injection" in str(e), e
        return
    raise AssertionError("expected ValueError on leading-dash key")


async def git_diff_uses_constructor_repo_when_no_cwd() -> None:
    calls = []
    src = GitDiffSource(repo="/fallback", spawn=spawner([FakeProc(stdout=b"")], calls))
    await src.fetch()
    assert calls[0]["args"] == ("git", "-C", "/fallback", "diff", "--unified=3")


async def git_diff_nonzero_exit_raises() -> None:
    # M2: a git failure (not a repo / bad ref) must RAISE, not be returned as the
    # "diff" — otherwise the reviewer reviews git's error text.
    src = GitDiffSource(spawn=spawner([FakeProc(returncode=128, stdout=b"fatal: not a git repo")], []))
    try:
        await src.fetch("HEAD", cwd="/nope")
        raise AssertionError("expected RuntimeError on non-zero git exit")
    except RuntimeError as e:
        assert "git diff failed" in str(e) and "128" in str(e), e


async def git_diff_intent_to_add_nonzero_raises() -> None:
    # M2: the `add -N -A` step's failure must also raise (before the diff runs).
    src = GitDiffSource(intent_to_add=True,
                        spawn=spawner([FakeProc(returncode=1, stdout=b"add failed")], []))
    try:
        await src.fetch(cwd="/r")
        raise AssertionError("expected RuntimeError on non-zero git add")
    except RuntimeError as e:
        assert "git add -N -A failed" in str(e), e


async def git_diff_timeout_kills_and_swallows_lookup_error() -> None:
    calls = []
    proc = FakeProc(raise_timeout=True, wait_raises=ProcessLookupError())
    src = GitDiffSource(spawn=spawner([proc], calls), timeout=0.01)
    try:
        await src.fetch(cwd="/r")
        raise AssertionError("expected TimeoutError")
    except asyncio.TimeoutError:
        pass
    assert proc.killed  # killed; the ProcessLookupError from wait() is swallowed


async def claude_binary_and_flag_trust() -> None:
    # BUG-629: config-named executable + permission-bypass flags are trust
    # seams — allowlist bare names, exists+executable for absolute paths,
    # bypass flags only by explicit opt-in
    import sys
    for bad in ("claude; rm -rf /", "-claude", "evil", "relative/path/claude", ""):
        try:
            ClaudeCliBackend(binary=bad)
            raise AssertionError("binary {!r} should have been rejected".format(bad))
        except ValueError:
            pass
    ClaudeCliBackend(binary="claude")                 # allow-listed bare name
    ClaudeCliBackend(binary=sys.executable)           # absolute existing executable
    try:
        ClaudeCliBackend(extra_args=["--dangerously-skip-permissions"])
        raise AssertionError("bypass flag should require explicit opt-in")
    except ValueError as e:
        assert "allow_dangerous_flags" in str(e), e
    ClaudeCliBackend(extra_args=["--dangerously-skip-permissions"],
                     allow_dangerous_flags=True)      # explicit, greppable opt-in


async def main() -> None:
    for fn in [
        claude_binary_and_flag_trust,
        claude_complete_builds_argv_pipes_prompt_returns_stdout,
        claude_cost_bridge_parses_json_usage,
        claude_cost_path_missing_result_returns_empty,
        claude_cost_path_non_json_passes_through,
        claude_no_cost_no_json_format,
        claude_nonzero_exit_raises_with_stderr,
        claude_timeout_kills_process_and_reraises,
        claude_build_args_covers_mcp_perm_and_tools,
        git_diff_builds_argv_with_ref_paths_and_context,
        git_diff_intent_to_add_runs_add_first,
        git_diff_uses_constructor_repo_when_no_cwd,
        git_diff_rejects_leading_dash_in_key,
        git_diff_nonzero_exit_raises,
        git_diff_intent_to_add_nonzero_raises,
        git_diff_timeout_kills_and_swallows_lookup_error,
    ]:
        await fn()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
