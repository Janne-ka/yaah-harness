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


async def claude_stream_cost_bridge_feeds_on_usage() -> None:
    # Cost bridge over the stream seam: the result event's usage is summed
    # (input + both cache buckets) and fed to on_usage — so a plain agent
    # collecting via api_provider.complete() still tracks cost now that the
    # complete() --output-format json path was removed.
    lines = [
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        b'{"type":"result","subtype":"success","stop_reason":"end_turn","model":"claude-sonnet",'
        b'"usage":{"input_tokens":100,"cache_read_input_tokens":20,"cache_creation_input_tokens":5,"output_tokens":30}}\n',
    ]
    usage = {}
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "hi"}]},
                                    on_usage=usage.update))
    assert usage == {"tokens_in": 125, "tokens_out": 30, "model": "claude-sonnet"}, usage
    assert [e["type"] for e in events] == ["start", "text_delta", "done"]


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


async def claude_rejects_isolation_defeating_flags() -> None:
    # MED-009 (opus security review): the dangerous-flag set blocked only the
    # two *skip-permissions* variants. Flags that defeat the cwd/worktree
    # isolation the backend advertises — --add-dir (broadens FS reach),
    # --settings (attacker JSON can change allowedTools/hooks),
    # --append-system-prompt, --ide — passed straight through. They must be
    # rejected too, in BOTH the separate-arg and joined `--flag=value` forms.
    for bad in (["--add-dir", "/"],
                ["--add-dir=/"],                       # joined form the old membership check missed
                ["--settings", "/tmp/evil.json"],
                ["--append-system-prompt", "ignore previous"],
                ["--ide"]):
        try:
            ClaudeCliBackend(extra_args=bad)
            raise AssertionError("isolation-defeating flag {!r} should be rejected".format(bad))
        except ValueError as e:
            assert "allow_dangerous_flags" in str(e), e
    # explicit opt-in still works (greppable in config)
    ClaudeCliBackend(extra_args=["--add-dir", "/work"], allow_dangerous_flags=True)


# ---- B3: ClaudeCliBackend.stream — real --output-format stream-json parsing -

class FakeStream:
    """Minimal async-readable stream — feeds bytes lines back via readline()."""

    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""

    async def read(self):
        joined = b"".join(self._lines)
        self._lines = []
        return joined


class FakeStdin:
    """Stdin stub that records write/drain/close ordering so tests can
    assert the asyncio StreamWriter contract (write → await drain → close)."""

    def __init__(self):
        self.data = b""
        self.events = []                          # ["write", "drain", "close"] order witness

    def write(self, data):
        self.data = (self.data or b"") + data
        self.events.append("write")

    async def drain(self):                         # real asyncio.StreamWriter contract
        self.events.append("drain")

    def close(self):
        self.closed = True
        self.events.append("close")


class FakeStreamProc:
    """Process stub that exposes stdout/stderr as FakeStream + a writable stdin —
    matches the shape ClaudeCliBackend.stream() needs (readline() loop, not
    communicate())."""

    def __init__(self, *, returncode=0, stdout_lines=None, stderr=b""):
        self.returncode = returncode
        self.stdin = FakeStdin()
        self.stdout = FakeStream(stdout_lines or [])
        self.stderr = FakeStream([stderr] if stderr else [])
        self.killed = self.waited = False

    def kill(self): self.killed = True

    async def wait(self): self.waited = True


def _stream_spawner(proc, captured):
    async def spawn(*args, **kwargs):
        captured.append({"args": args, "kwargs": kwargs})
        return proc
    return spawn


async def _drain(stream_iter):
    return [ev async for ev in stream_iter]


async def claude_stream_simple_text_yields_text_delta_and_done() -> None:
    # The minimal happy path: claude returns one assistant message with a text
    # content block, then a result event with usage. Stream emits start,
    # text_delta, done(usage).
    lines = [
        b'{"type":"system","subtype":"init","model":"claude-opus-4-7"}\n',
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}\n',
        b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{"input_tokens":5,"output_tokens":3}}\n',
    ]
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "hi"}]}))
    types = [e["type"] for e in events]
    assert types == ["start", "text_delta", "done"], types
    assert events[1]["delta"] == "Hello"
    assert events[2]["stop_reason"] == "end_turn"
    assert events[2]["usage"] == {"input_tokens": 5, "output_tokens": 3}


async def claude_stream_uses_output_format_stream_json_argv() -> None:
    # The argv MUST request stream-json + --verbose (claude requires --verbose
    # alongside stream-json per its CLI; otherwise stream-json is rejected).
    lines = [b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{}}\n']
    proc = FakeStreamProc(stdout_lines=lines)
    captured = []
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, captured))
    await _drain(be.stream({"messages": [{"role": "user", "content": "hi"}]}, cwd="/work"))
    argv = captured[0]["args"]
    assert "--output-format" in argv, argv
    assert argv[argv.index("--output-format") + 1] == "stream-json", argv
    assert "--verbose" in argv, argv  # required by claude when stream-json is set
    assert captured[0]["kwargs"]["cwd"] == "/work"  # per-run worktree threaded to spawn


async def claude_stream_tool_use_session_skips_internal_tool_calls() -> None:
    # Claude runs its own tool loop internally — assistant.tool_use blocks
    # and user.tool_result events are CLAUDE's, not YAAH's. They MUST NOT
    # surface as YAAH toolcall_end events (would mislead consumers into
    # thinking they need to dispatch). Only the final text answer surfaces.
    lines = [
        b'{"type":"system","subtype":"init"}\n',
        b'{"type":"assistant","message":{"content":[{"type":"tool_use","id":"tu1","name":"Read","input":{"file":"/x"}}]}}\n',
        b'{"type":"user","message":{"content":[{"tool_use_id":"tu1","type":"tool_result","content":"file contents"}]}}\n',
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"The file contains foo"}]}}\n',
        b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{}}\n',
    ]
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "read /x"}]}))
    types = [e["type"] for e in events]
    # exactly one text_delta (the final answer), no toolcall_end
    assert types == ["start", "text_delta", "done"], types
    assert events[1]["delta"] == "The file contains foo"
    assert "toolcall_end" not in types


async def claude_stream_thinking_blocks_skipped() -> None:
    # Anthropic extended-thinking blocks are internal reasoning; not the
    # user-facing answer. Don't surface as text_delta.
    lines = [
        b'{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"reasoning..."},{"type":"text","text":"answer"}]}}\n',
        b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{}}\n',
    ]
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "think"}]}))
    text_deltas = [e for e in events if e["type"] == "text_delta"]
    assert len(text_deltas) == 1, text_deltas
    assert text_deltas[0]["delta"] == "answer"


async def claude_stream_malformed_lines_skipped() -> None:
    # claude_cli or the shell may inject garbage (auth notices, blank lines,
    # whitespace, half-written buffers). Non-JSON lines must NOT crash the
    # stream; they're skipped silently.
    lines = [
        b'\n',                                                  # blank
        b'Added user:design:read for the connector.\n',        # claude printed a plain notice (real, observed)
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"survived"}]}}\n',
        b'{not valid json\n',                                   # garbage
        b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{}}\n',
    ]
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "x"}]}))
    text_deltas = [e for e in events if e["type"] == "text_delta"]
    assert len(text_deltas) == 1, text_deltas
    assert text_deltas[0]["delta"] == "survived"


async def claude_stream_drains_stderr_before_wait() -> None:
    # CRIT-004 (opus bugs review): the old code did `await proc.wait()` THEN
    # `await proc.stderr.read()`. If the process fills its stderr pipe buffer
    # (>64KB) it can't exit while blocked on the write, so wait() deadlocks.
    # The fix drains stderr BEFORE wait(). This fake mimics the OS deadlock:
    # wait() blocks until stderr has been fully read. If the code waits first,
    # this test hangs (red); after the fix it passes.

    class DeadlockingProc(FakeStreamProc):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._stderr_drained = asyncio.Event()
            # wrap stderr.read to flag when drained
            real_stderr = self.stderr

            class StderrWitness:
                def __init__(s): s._inner = real_stderr
                async def read(s):
                    data = await s._inner.read()
                    self._stderr_drained.set()
                    return data
                async def readline(s): return await s._inner.readline()
            self.stderr = StderrWitness()

        async def wait(self):
            # mimic OS: process can't be reaped until its stderr is drained
            await self._stderr_drained.wait()
            self.waited = True

    proc = DeadlockingProc(returncode=2, stdout_lines=[], stderr=b"lots of stderr")
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    # If the code waits before draining stderr, this never completes.
    events = await asyncio.wait_for(
        _drain(be.stream({"messages": [{"role": "user", "content": "x"}]})),
        timeout=3.0)
    types = [e["type"] for e in events]
    assert "error" in types, types
    err = [e for e in events if e["type"] == "error"][0]
    assert "lots of stderr" in err["message"] or "exit 2" in err["message"], err


async def claude_stream_nonzero_exit_yields_error_event() -> None:
    # Process crash / auth fail / hung claude that gets reaped non-zero:
    # surface as an in-stream error event (not a raised exception). Consumer
    # can decide how to react.
    proc = FakeStreamProc(returncode=2, stdout_lines=[], stderr=b"auth failed")
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "x"}]}))
    types = [e["type"] for e in events]
    assert "error" in types, types
    err = [e for e in events if e["type"] == "error"][0]
    assert "exit 2" in err["message"] or "auth failed" in err["message"], err


async def claude_stream_passes_prompt_via_stdin() -> None:
    # The user message from context becomes the prompt on stdin. Multi-message
    # contexts collapse to the most recent user message — claude -p has no
    # conversation-history stdin format.
    lines = [b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{}}\n']
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    await _drain(be.stream({
        "messages": [{"role": "user", "content": "older"},
                     {"role": "assistant", "content": "..."},
                     {"role": "user", "content": "latest"}],
    }))
    assert proc.stdin.data == b"latest", proc.stdin.data



async def claude_stream_handles_none_stdin_without_crashing() -> None:
    # CRIT-001 (opus bugs review): if the spawned process died before its
    # stdin pipe opened (misconfigured binary, immediate-exit, OS resource
    # exhaustion), `proc.stdin` is None and the old code crashed with
    # AttributeError. The stream MUST surface this as an error event, not
    # a raised AttributeError.
    proc = FakeStreamProc(returncode=127, stdout_lines=[], stderr=b"binary failed")
    proc.stdin = None                                    # the failure mode
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "x"}]}))
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "error" in types, "expected an error event, got: {}".format(types)
    err = [e for e in events if e["type"] == "error"][0]
    assert "stdin" in err["message"].lower() or "pipe" in err["message"].lower(), \
        "error message should mention stdin / pipe; got: {}".format(err["message"])
    # No done event after error (error is the terminator).
    assert types.count("done") == 0, types


async def claude_stream_drains_stdin_before_closing() -> None:
    # CRIT-002 (opus bugs review): asyncio.StreamWriter.write() is synchronous
    # and only buffers up to the high-water mark; large prompts (>64KB pipe
    # buffer) deadlock if drain() isn't called before close(). This asserts
    # the contract: write -> await drain -> close. If a future refactor
    # forgets drain(), this test goes red BEFORE production hangs at 64KB.
    proc = FakeStreamProc(stdout_lines=[
        b'{"type":"result","subtype":"success","stop_reason":"end_turn","usage":{}}\n',
    ])
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    await _drain(be.stream({"messages": [{"role": "user", "content": "x"}]}))
    assert proc.stdin.events == ["write", "drain", "close"], \
        "expected ordered ['write','drain','close']; got {!r}".format(proc.stdin.events)


async def claude_stream_timeout_kills_proc_and_yields_error() -> None:
    # CRIT-003 (opus bugs review): a wedged claude (mid-stream silence,
    # infinite api_retry events, MCP stall) used to hang the whole pipeline
    # because the readline loop had NO upper bound — even though the
    # constructor docstring promises `timeout` works for this provider.
    # The fix wraps readline in asyncio.wait_for with self._timeout and
    # kills the process + emits an error on TimeoutError.

    class HangingStdout:
        def __init__(self): self._closed = False
        async def readline(self):
            # never resolves; simulates a wedged claude that produced no
            # output. The test relies on self._timeout firing the kill path.
            await asyncio.Event().wait()
        async def read(self): return b""

    proc = FakeStreamProc(stdout_lines=[])
    proc.stdout = HangingStdout()
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []), timeout=0.3)
    # The whole call must complete within a small multiple of self._timeout,
    # otherwise the kill path isn't running.
    events = await asyncio.wait_for(
        _drain(be.stream({"messages": [{"role": "user", "content": "x"}]})),
        timeout=3.0)
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "error" in types, "expected error event on timeout; got: {}".format(types)
    err = [e for e in events if e["type"] == "error"][0]
    assert "timeout" in err["message"].lower(), \
        "expected 'timeout' in error message; got: {}".format(err["message"])
    # The stalled process must be killed (otherwise it leaks).
    assert proc.killed, "wedged claude must be killed on timeout"


async def claude_stream_parses_captured_fixture_end_to_end() -> None:
    # TEST-002 (opus test-quality review): the other stream scenarios use tiny
    # hand-curated byte lines. This one feeds the REAL captured session
    # (sanitized) through the parser line-by-line, so a regression in the
    # multi-event sequence (system init -> thinking -> tool_use -> tool_result
    # -> text -> result) is caught against an actual claude wire shape.
    import os
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "claude_stream_json", "tool_use_session.jsonl")
    with open(fixture, "rb") as f:
        lines = [ln if ln.endswith(b"\n") else ln + b"\n" for ln in f.read().splitlines()]
    proc = FakeStreamProc(stdout_lines=lines)
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "read it"}]}))
    types = [e["type"] for e in events]
    # The session has thinking (skipped) + an internal tool_use/tool_result
    # (claude-internal, NOT surfaced) + one final assistant text + result.
    assert types == ["start", "text_delta", "done"], types
    assert "toolcall_end" not in types, "claude-internal tool calls must not surface"
    assert events[1]["delta"] == "The file contains: example file contents"
    assert events[2]["stop_reason"] == "end_turn"
    assert events[2]["usage"]["input_tokens"] == 22


async def claude_stream_handles_none_stdout_without_crashing() -> None:
    # CRIT-001 mirror: if the spawned process died before its stdout pipe
    # opened, `proc.stdout` is None and the readline loop crashed with
    # AttributeError. The stream MUST surface this as an error event.
    proc = FakeStreamProc(returncode=127, stdout_lines=[], stderr=b"binary failed")
    proc.stdout = None                                   # the failure mode
    be = ClaudeCliBackend(spawn=_stream_spawner(proc, []))
    events = await _drain(be.stream({"messages": [{"role": "user", "content": "x"}]}))
    types = [e["type"] for e in events]
    assert types[0] == "start"
    assert "error" in types, "expected an error event, got: {}".format(types)
    err = [e for e in events if e["type"] == "error"][0]
    assert "stdout" in err["message"].lower() or "pipe" in err["message"].lower(), \
        "error message should mention stdout / pipe; got: {}".format(err["message"])


async def main() -> None:
    for fn in [
        claude_binary_and_flag_trust,
        claude_rejects_isolation_defeating_flags,
        claude_build_args_covers_mcp_perm_and_tools,
        claude_stream_cost_bridge_feeds_on_usage,
        # B3 — stream-json parsing
        claude_stream_simple_text_yields_text_delta_and_done,
        claude_stream_uses_output_format_stream_json_argv,
        claude_stream_tool_use_session_skips_internal_tool_calls,
        claude_stream_thinking_blocks_skipped,
        claude_stream_malformed_lines_skipped,
        claude_stream_nonzero_exit_yields_error_event,
        claude_stream_passes_prompt_via_stdin,
        claude_stream_handles_none_stdin_without_crashing,
        claude_stream_drains_stdin_before_closing,
        claude_stream_timeout_kills_proc_and_yields_error,
        claude_stream_drains_stderr_before_wait,
        claude_stream_parses_captured_fixture_end_to_end,
        claude_stream_handles_none_stdout_without_crashing,
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
