"""ClaudeCliBackend — an ApiProvider that shells out to the local `claude -p`.

Used by: the runtime's `claude` provider (and apps) when running real local
Claude (e.g. an app's code-editing stages).
Where: local hosts that have the Claude Code CLI.
Why: invoke a local agent the simple way — prompt on stdin, text on stdout,
model via --model, MCP stripped by default (the project .mcp.json can stall
agent init).

A read-only (text) agent leaves permission_mode/allowed_tools unset → no tools.
A repo-bound agent (an app's code/edit stage) is configured with edit tools and a
permission mode, and is handed a per-call `cwd` (the task's worktree) so its
file edits land in isolation. The cwd is a call opt, not constructor state,
because it is per-run payload data.

After B2.6 (provider unification): native ApiProvider — `stream()` exists,
satisfying the new protocol shape, but unlike LiteLLM/Fake migrations
`complete()` remains the source of truth. `stream()` calls complete() and
yields one text_delta + done. The reason: complete()'s subprocess handling
(timeout, kill-on-timeout, exit-code, JSON usage extraction) is well-tested;
flipping which method is canonical would require refactoring all of that
into stream() with no test coverage of the new path.

Tools are NOT exposed through the YAAH tool-loop — claude handles its own
tool execution internally via --allowedTools / --permission-mode. So
tool-call events do not flow through stream(); a context with tools would
be passed to claude as configuration, not surfaced as agent-emitted calls.

WIRE-LEVEL STREAMING IS DEFERRED: real `claude --output-format stream-json`
parsing (line-by-line stdout, partial tool_use deltas, usage reconciliation)
is the eval-flagged hard work. Not in this commit. When a consumer demands
token-level deltas from claude, that's the upgrade.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence

from ...agents import api_provider as _ap

# Config-named-executable trust (BUG-629: an env-var-named binary was executed
# with --allow-dangerously-skip-permissions). The binary is config — and config
# is code-equivalent — so it gets the same treatment as any other trust seam:
# an allowlist for bare names, an exists+executable check for explicit paths,
# and permission-bypass flags rejected unless explicitly opted in (greppable
# in config, never implicit).
_ALLOWED_BARE_BINARIES = frozenset({"claude"})
_UNSAFE_CHARS_RE = re.compile(r"[\s\x00-\x1f]")  # whitespace/control — never in a binary name
# Flags rejected unless explicitly opted in. Two families:
#  - permission bypass (BUG-629): skip the permission system entirely;
#  - isolation defeat (MED-009, opus security review): broaden claude's
#    filesystem reach or change its trust config past the cwd/worktree
#    isolation this backend advertises. --add-dir grants extra dirs;
#    --settings can swap allowedTools/hooks via attacker JSON;
#    --append-system-prompt injects into the system prompt; --ide connects
#    to an external IDE. An author who genuinely needs one opts in
#    EXPLICITLY (allow_dangerous_flags: true — greppable in config).
_DANGEROUS_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--add-dir",
    "--settings",
    "--append-system-prompt",
    "--ide",
})


def _validate_binary(binary: str) -> str:
    if not binary or _UNSAFE_CHARS_RE.search(binary) or binary.startswith("-"):
        raise ValueError(
            "claude_cli binary {!r} fails the safe-name check "
            "(non-empty, no whitespace/control chars, no leading '-')".format(binary))
    if binary in _ALLOWED_BARE_BINARIES:
        return binary
    if os.path.isabs(binary):
        # exec'd directly (no shell), so the real guard for a path is that it
        # names an existing executable FILE the config author chose explicitly
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            raise ValueError(
                "claude_cli binary {!r} is not an existing executable file".format(binary))
        return binary
    raise ValueError(
        "claude_cli binary {!r} is neither an allow-listed name ({}) nor an "
        "absolute path to an executable — name the binary explicitly in config"
        .format(binary, ", ".join(sorted(_ALLOWED_BARE_BINARIES))))


def _validate_extra_args(extra_args: Sequence[str], allow_dangerous: bool) -> List[str]:
    args = list(extra_args or [])
    if not allow_dangerous:
        # Match both the separate-arg form (`--add-dir`, `/`) and the joined
        # form (`--add-dir=/`) — the bare membership check missed the latter
        # (MED-009). An arg is dangerous if it equals a flag or starts with
        # `<flag>=`.
        def _is_dangerous(a: str) -> bool:
            if a in _DANGEROUS_FLAGS:
                return True
            head = a.split("=", 1)[0]
            return head in _DANGEROUS_FLAGS
        bad = [a for a in args if _is_dangerous(a)]
        if bad:
            raise ValueError(
                "claude_cli extra_args carry permission-bypass / isolation-defeating "
                "flag(s) {} — set allow_dangerous_flags: true in the provider config "
                "to opt in EXPLICITLY (BUG-629 / MED-009: this must never happen "
                "implicitly)".format(bad))
    return args


class ClaudeCliBackend:
    def __init__(
        self,
        *,
        binary: str = "claude",
        extra_args: Optional[Sequence[str]] = None,
        strip_mcp: bool = True,
        timeout: Optional[float] = None,
        permission_mode: Optional[str] = None,   # e.g. "acceptEdits" for a code agent
        allowed_tools: Optional[Sequence[str]] = None,  # e.g. ["Read", "Edit", "Write"]
        allow_dangerous_flags: bool = False,     # explicit opt-in for bypass flags (BUG-629)
        spawn: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._binary = _validate_binary(binary)
        self._extra_args = _validate_extra_args(extra_args or [], allow_dangerous_flags)
        self._strip_mcp = strip_mcp
        self._timeout = timeout
        self._permission_mode = permission_mode
        self._allowed_tools = list(allowed_tools or [])
        # `spawn` is the external dependency, injected for testability: an async
        # (*argv, stdin=, stdout=, stderr=, cwd=) -> process callable. Defaults to
        # asyncio.create_subprocess_exec. Tests pass a fake-process spawner so the
        # whole run path (argv, exit handling, timeout->kill) is covered without a
        # real `claude` binary.
        self._spawn = spawn or asyncio.create_subprocess_exec

    def _build_args(self, model: Optional[str], opts: dict, *,
                    json_output: bool = False, stream_json: bool = False) -> List[str]:
        # per-call opts (from the agent's config) override the constructor defaults,
        # so tool permissions / permission-mode are PER-AGENT, not per-provider.
        permission_mode = opts.get("permission_mode", self._permission_mode)
        allowed_tools = opts.get("allowed_tools", self._allowed_tools)
        mcp = opts.get("mcp")  # a servers map resolved from the agent's mcp config
        args: List[str] = [self._binary, "-p"]
        if stream_json:
            # claude requires --verbose alongside --output-format stream-json
            # (the CLI rejects stream-json without it).
            args += ["--output-format", "stream-json", "--verbose"]
        elif json_output:  # cost capture on -> ask claude for usage (bug review L8)
            args += ["--output-format", "json"]
        if model:
            args += ["--model", model]
        if mcp:
            # give the model these MCP servers (model-initiated tools); strict so
            # the project's own .mcp.json is ignored — only what we configured.
            args += ["--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": mcp})]
        elif self._strip_mcp:
            args += ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if permission_mode:
            args += ["--permission-mode", permission_mode]
        if allowed_tools:
            args += ["--allowedTools", ",".join(allowed_tools)]
        args += self._extra_args
        return args

    async def complete(self, prompt: str, *, model: Optional[str] = None, **opts: Any) -> str:
        on_usage = opts.pop("on_usage", None)  # cost bridge (R4/L8); not a CLI arg
        cwd = opts.get("cwd")  # per-run worktree for repo-bound agents
        timeout = opts.get("timeout", self._timeout)  # per-node deadline overrides default
        args = self._build_args(model, opts, json_output=on_usage is not None)
        proc = await self._spawn(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode != 0:
            raise RuntimeError(
                "claude -p failed (exit {}): {}".format(
                    proc.returncode, err.decode(errors="replace")[:500]
                )
            )
        text = out.decode(errors="replace")
        if on_usage is not None:  # --output-format json: extract result + feed usage
            return _extract_result_and_usage(text, on_usage, model)
        return text

    def stream(self, context: _ap.Context, **opts: Any) -> AsyncIterator[_ap.StreamEvent]:
        return self._iter(context, opts)

    async def _iter(self, context: _ap.Context, opts: Dict[str, Any]) -> AsyncIterator[_ap.StreamEvent]:
        """B3 (2026-06-23): real `--output-format stream-json` parsing. Spawns
        claude with --verbose --output-format stream-json, writes the prompt
        to stdin, reads stdout line-by-line as JSONL, maps each claude event
        to a StreamEvent.

        What surfaces as YAAH events:
          - assistant.content[text]   → text_delta
          - result                    → done(stop_reason, usage)
          - process exit != 0         → error
        What does NOT surface (claude handles its own tool loop internally):
          - assistant.content[tool_use], user.content[tool_result] are
            claude-internal. Emitting them as toolcall_end would mislead
            consumers into thinking they need to dispatch.
          - assistant.content[thinking] is internal reasoning, not the
            user-facing answer.
        Other claude event types (system init, system api_retry, user,
        rate_limit_event) are ignored — they're transport/diagnostic
        noise, not user-visible content.

        claude -p is single-prompt-shaped (no conversation history through
        stdin); the most-recent user message becomes the prompt. Tool
        definitions in context.tools are not surfaced — claude handles its
        own tool loop natively via --allowedTools / --permission-mode.
        """
        yield {"type": "start"}
        prompt = _prompt_from_messages(context.get("messages") or [],
                                       context.get("system"))
        model = context.get("model")
        cwd = opts.get("cwd")
        args = self._build_args(model, opts, stream_json=True)
        proc = await self._spawn(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        # CRIT-001 (opus bugs review, 2026-06-23): if the spawned process died
        # before its stdin/stdout pipes opened (misconfigured binary, immediate
        # exit, OS resource exhaustion), the pipe attributes are None and the
        # bare `.write()` / `.readline()` calls below crash with AttributeError.
        # Surface as in-stream error events so consumers get the same shape on
        # success and failure paths.
        if proc.stdin is None:
            yield {"type": "error",
                   "message": "claude subprocess stdin pipe unavailable "
                              "(process likely exited before pipe opened)"}
            return
        if proc.stdout is None:
            yield {"type": "error",
                   "message": "claude subprocess stdout pipe unavailable "
                              "(process likely exited before pipe opened)"}
            return
        # Send the prompt on stdin, drain to flush, then close so claude sees
        # EOF and starts. CRIT-002 (opus bugs review): the synchronous
        # `write()` only buffers up to the OS pipe high-water mark (~64KB);
        # large prompts deadlock if drain() isn't awaited before close()
        # (claude blocks writing to stdin while we block waiting for its
        # stdout, mutual deadlock).
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # Read stream-json line by line. JSONL means one event per line;
        # an empty `readline()` return signals EOF. Parse each line ONCE;
        # text_delta events surface immediately; result captures terminator
        # data; everything else is diagnostic noise.
        # CRIT-003 (opus bugs review): wrap each readline in wait_for so a
        # wedged claude (mid-stream silence, infinite api_retry loop, MCP
        # stall) doesn't hang the pipeline indefinitely. self._timeout=None
        # preserves the legacy "wait forever" behavior.
        timeout = opts.get("timeout", self._timeout)
        usage: Optional[Dict[str, Any]] = None
        stop_reason = "end_turn"
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(),
                                              timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                yield {"type": "error",
                       "message": "claude stream-json timeout after {}s "
                                  "(no output received from subprocess)".format(timeout)}
                return
            if not line:
                break
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Garbage line (blank, plain text notice from claude, partial
                # buffer). Skip silently — the stream should not crash on
                # noise outside the JSONL envelope.
                continue
            event_type = obj.get("type")
            if event_type == "assistant":
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text") or ""
                        if text:
                            yield {"type": "text_delta", "delta": text}
                    # thinking / tool_use: deliberately NOT surfaced
                    # (claude-internal — see method docstring)
            elif event_type == "result":
                stop_reason = obj.get("stop_reason") or stop_reason
                u = obj.get("usage")
                if isinstance(u, dict):
                    usage = u
            # system / user / rate_limit_event: ignored (diagnostic noise)

        # CRIT-004 (opus bugs review): drain stderr BEFORE wait(). If the
        # process filled its stderr pipe buffer (>64KB) it can't exit while
        # blocked on the write, so wait() would deadlock. We've already drained
        # stdout (the readline loop hit EOF); reading stderr to EOF unblocks the
        # process, then wait() returns immediately. Reading empty stderr is
        # cheap (returns b"" at EOF). stderr may be absent on some stubs.
        err_bytes = b""
        if proc.stderr is not None:
            err_bytes = await proc.stderr.read()
        await proc.wait()
        if proc.returncode != 0:
            err_text = err_bytes.decode(errors="replace")[:500] if err_bytes else ""
            yield {"type": "error",
                   "message": "claude exit {}{}".format(
                       proc.returncode, ": " + err_text if err_text else "")}
            return

        done: Dict[str, Any] = {"type": "done", "stop_reason": stop_reason}
        if usage is not None:
            done["usage"] = usage
        yield done


def _prompt_from_messages(messages: List[Dict[str, Any]], system: Optional[str]) -> str:
    """Stitch a context.messages list into a single prompt string for claude -p.
    Picks the most recent user-role string content (matching the LegacyBackendAdapter
    convention) and prepends a system preamble if present. Multi-turn conversation
    history isn't passed through — claude -p has no stdin format for it; the
    --output-format stream-json upgrade is where real conversations become possible."""
    user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content")
            if isinstance(c, str):
                user_text = c
                break
            if isinstance(c, list):
                user_text = "".join(b.get("text", "") for b in c
                                    if isinstance(b, dict) and b.get("type") == "text")
                break
    if system:
        return system + "\n\n" + user_text if user_text else system
    return user_text


def _extract_result_and_usage(raw: str, on_usage: Callable[..., Any], model: Optional[str]) -> str:
    """Parse `claude -p --output-format json` (a single JSON object with `result`
    + `usage`): feed token usage to the cost bridge and return the result text.
    Defensive — if the output isn't JSON at all, return it unchanged (claude
    likely failed to honor --output-format). If it's JSON but the expected
    `result` key is missing, return "" — downstream stages get a well-defined
    string contract (assessment cluster 3 B6: previously returned the whole
    JSON envelope as the agent's text, which then poisoned the next stage)."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw                                     # not JSON: pass through (claude misbehaved)
    if not isinstance(obj, dict):
        return raw if not isinstance(obj, (dict, list)) else ""
    u = obj.get("usage") or {}
    tokens_in = sum(int(u.get(k, 0) or 0) for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    on_usage({"tokens_in": tokens_in,
              "tokens_out": int(u.get("output_tokens", 0) or 0),
              "model": obj.get("model") or model})
    result = obj.get("result")
    return result if isinstance(result, str) else ""    # missing/non-string -> "" (assessment B6)
