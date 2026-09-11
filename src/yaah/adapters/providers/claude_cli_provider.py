"""ClaudeCliProvider — an ApiProvider that shells out to the local `claude -p`.

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

Native ApiProvider: `stream()` is the one seam. It parses `claude -p
--output-format stream-json` line-by-line (see `_iter`) — text blocks become
text_delta events; the result event carries stop_reason + usage (fed to the
`on_usage` cost bridge via `_map_usage`). There is no separate `complete()`; a
caller wanting the collected string uses `api_provider.complete(this, ...)`,
which drains the stream.

Tools are NOT exposed through the YAAH tool-loop — claude handles its own
tool execution internally via --allowedTools / --permission-mode. So
tool-call events do not flow through stream(); a context with tools is passed
to claude as configuration, not surfaced as agent-emitted calls.

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence

from ...agents.api_provider import ApiProvider, Context, StreamEvent, Usage
from ...trace.bounded_text import bounded

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

# asyncio's StreamReader defaults to a 64 KiB line buffer. A single
# `claude -p --output-format stream-json` line can carry an ENTIRE authored
# file in ONE jsonl event (a Write tool_use embeds the whole file body), so a
# write-heavy agent overruns the default and `readline()` raises ValueError
# ("Separator is found, but chunk is longer than limit") — which ERRORs the
# node even when every edit already landed and only the tail of the output
# stream is being drained (a completed-correct stage reported as ERROR). Give
# the reader an 8 MiB per-line buffer so a whole-file line fits: one jsonl
# line == one whole authored file.
_STREAM_LINE_LIMIT = 8 * 2 ** 20  # 8 MiB per stream-json line

# How much of a failing child's stderr rides the error event. Sized for
# diagnosability (M44: a fan-out's parallel calls all died exit 1 x3 attempts
# and the trace could not say why — a rate-limit storm needs the CLI's actual
# complaint, not a 400-char sliver), but deliberately BELOW
# PhaseContributor.ERROR_MAX (2600), because that is where this text ends up:
# the harness notes the whole message as the failure detail and the phase
# capture re-bounds it at ERROR_MAX. Budget it AT the outer bound and the
# "claude exit N: " prefix plus the harness's own wrapping push the message
# over, so the downstream bound eats this one's truncation marker AND the tail
# end of stderr — the part that holds the diagnosis. Stack two bounds and the
# inner one must leave the outer some room (here: ~600 for prefix, wrapping,
# and the result-event error below).
_STDERR_MAX = 2000

# Bound on the result-event error text (see the read loop): claude -p in
# stream-json mode reports SOME failures — rate limits among them — as an
# error-subtyped `result` event on STDOUT and then exits nonzero with an EMPTY
# stderr, so without this capture the failure reads "claude exit 1" and nothing
# else (the M44 undiagnosable storm). Head-kept: it is a structured message
# whose code leads.
_RESULT_ERROR_MAX = 400

# Inactivity watchdog default — network-cut / stall protection. stream()'s read
# loop wraps every `proc.stdout.readline()` in asyncio.wait_for(timeout=...): on
# TimeoutError it kills the child and emits a clean error event so the chain
# doesn't hang. This is an INACTIVITY timeout PER readline, NOT a total-run
# deadline — a healthy agent can be legitimately silent for several minutes while
# a single long tool call runs (e.g. a whole test suite executing inside the
# agent), so the default must be generous. What it must NEVER be is infinite:
# on an internet cut the `claude` child retries silently forever, readline never
# returns, the node never errors, and the whole run freezes with no park and no
# notification (the incident this guards — "guard exists, unconfigured =
# unguarded"). TRADE-OFF, stated honestly: a single tool call CAN exceed 15
# minutes (an agent running a large test suite in one shell call) — that node
# gets false-killed and transient-retried (recoverable, but the agent restarts).
# Such nodes must set an explicit larger node `timeout`; the default optimizes
# for "a cut never freezes the chain", not "no long call is ever interrupted".
_DEFAULT_STALL_TIMEOUT = 900.0  # seconds of silence per readline before error+kill

# Sentinel distinct from None so __init__ can tell "caller omitted timeout"
# (→ arm the finite default above) from an EXPLICIT `timeout=None` (→ keep the
# legacy wait-forever behavior — an explicit, deliberate opt-out). None cannot
# serve double duty here, since None is itself the wait-forever request.
_UNSET: Any = object()


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


class ClaudeCliProvider(ApiProvider):
    def __init__(
        self,
        *,
        binary: str = "claude",
        extra_args: Optional[Sequence[str]] = None,
        strip_mcp: bool = True,
        # Caller-facing contract is Optional[float] (a finite inactivity timeout,
        # or None for wait-forever). The default is the _UNSET sentinel — "not
        # passed" — resolved below to the finite _DEFAULT_STALL_TIMEOUT so an
        # unconfigured consumer is armed against a network cut, while an explicit
        # None still means wait-forever.
        timeout: Optional[float] = _UNSET,  # type: ignore[assignment]
        permission_mode: Optional[str] = None,   # e.g. "acceptEdits" for a code agent
        allowed_tools: Optional[Sequence[str]] = None,  # e.g. ["Read", "Edit", "Write"]
        allow_dangerous_flags: bool = False,     # explicit opt-in for bypass flags (BUG-629)
        spawn: Optional[Callable[..., Awaitable[Any]]] = None,
    ) -> None:
        self._binary = _validate_binary(binary)
        self._extra_args = _validate_extra_args(extra_args or [], allow_dangerous_flags)
        self._strip_mcp = strip_mcp
        # Resolve the inactivity watchdog. The _UNSET sentinel (NOT None) marks
        # "caller omitted timeout" → arm the finite default (network-cut
        # protection). An explicit `timeout=None` falls through unchanged → the
        # legacy wait-forever behavior, a deliberate opt-out.
        self._timeout = _DEFAULT_STALL_TIMEOUT if timeout is _UNSET else timeout
        self._permission_mode = permission_mode
        self._allowed_tools = list(allowed_tools or [])
        # `spawn` is the external dependency, injected for testability: an async
        # (*argv, stdin=, stdout=, stderr=, cwd=) -> process callable. Defaults to
        # asyncio.create_subprocess_exec. Tests pass a fake-process spawner so the
        # whole run path (argv, exit handling, timeout->kill) is covered without a
        # real `claude` binary.
        self._spawn = spawn or asyncio.create_subprocess_exec

    def _build_args(self, model: Optional[str], opts: dict, *,
                    stream_json: bool = False) -> List[str]:
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

    def stream(self, context: Context, **opts: Any) -> AsyncIterator[StreamEvent]:
        return self._iter(context, opts)

    async def _iter(self, context: Context, opts: Dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """B3 (2026-06-23): real `--output-format stream-json` parsing. Spawns
        claude with --verbose --output-format stream-json, writes the prompt
        to stdin, reads stdout line-by-line as JSONL, maps each claude event
        to a StreamEvent.

        What surfaces as YAAH events:
          - assistant.content[text]      → text_delta
          - assistant.content[tool_use]  → notice(kind=tool_use, tool=name) — a
            PASSIVE observation, NOT toolcall_end: claude runs its own tool
            loop internally, so emitting the executable kind would mislead
            consumers into thinking they must dispatch. The notice is inert to
            every collector; the live-monitoring bridge turns it into a pulse.
          - user.content[tool_result]    → notice(kind=tool_result, tool=name)
            — the closing bracket of the tool window (the id is resolved to
            the name recorded at tool_use time).
          - result                       → done(stop_reason, usage)
          - process exit != 0            → error
        What does NOT surface:
          - assistant.content[thinking] is internal reasoning, not the
            user-facing answer (content capture is a separate concern).
        Other claude event types (system init, system api_retry,
        rate_limit_event) are ignored — they're transport/diagnostic
        noise, not user-visible content.

        claude -p is single-prompt-shaped (no conversation history through
        stdin); the most-recent user message becomes the prompt. Tool
        definitions in context.tools are not surfaced — claude handles its
        own tool loop natively via --allowedTools / --permission-mode.
        """
        on_usage = opts.pop("on_usage", None)  # cost bridge (R4/L8); not a CLI arg
        yield {"type": "start"}
        prompt = _prompt_from_messages(context.get("messages") or [],
                                       context.get("system"))
        model = context.get("model")
        result_model: Optional[str] = None  # model named in the result event (cost bridge)
        cwd = opts.get("cwd")
        args = self._build_args(model, opts, stream_json=True)
        proc = await self._spawn(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            # Raise the stdout StreamReader line buffer past the 64 KiB default:
            # one stream-json line can be a whole authored file (see
            # _STREAM_LINE_LIMIT). Without this, readline() raises ValueError on
            # a large Write event and the node ERRORs on a completed stage.
            limit=_STREAM_LINE_LIMIT,
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
        # stall) doesn't hang the pipeline indefinitely. The instance value is
        # now the finite _DEFAULT_STALL_TIMEOUT unless the caller explicitly
        # passed timeout=None (→ wait forever). Per-call resolution: an explicit
        # opts["timeout"] (INCLUDING None) wins; absent → the instance value.
        timeout = opts.get("timeout", self._timeout)
        usage: Optional[Dict[str, Any]] = None
        stop_reason = "end_turn"
        result_error = ""  # error text from an error-subtyped result event (M44)
        tool_names: Dict[str, str] = {}  # tool_use id → name, to label tool_result notices
        # The whole read+drain sequence is wrapped so `finally` can GUARANTEE
        # the subprocess is reaped on every exit path (below). Without it, a
        # read-loop exception (an over-limit line, an unexpected error) escaped
        # PAST the timeout branch's kill and left claude orphaned at PPID 1,
        # still streaming to a dead pipe long after the engine exited — a cost
        # leak observed in the field.
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(),
                                                  timeout=timeout)
                except asyncio.TimeoutError:
                    # Guarded like the finally's kill: if the child died in the
                    # race window between readline timing out and the kill,
                    # ProcessLookupError must not propagate out of the generator
                    # — the consumer still gets the clean timeout error event.
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass  # already gone between the timeout and the kill
                    await proc.wait()
                    # The child is dead, so its buffered stderr — if it wrote
                    # any before wedging — is the only witness to WHY it went
                    # silent. Attach the bounded tail like the exit-nonzero path
                    # does (M44: an undiagnosed kill costs a full paid retry).
                    tail = await _stderr_tail_of_dead_child(proc)
                    yield {"type": "error",
                           "message": "claude stream-json INACTIVITY timeout after {}s "
                                      "with no output from the subprocess — likely a "
                                      "network outage, a wedged CLI, or an MCP stall. The "
                                      "child was killed; the run can be resumed once "
                                      "connectivity returns (the engine's error path owns "
                                      "retry/park).{}".format(
                                          timeout,
                                          " stderr tail: " + tail if tail else "")}
                    return
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    # A single stream-json line exceeded even the 8 MiB
                    # _STREAM_LINE_LIMIT buffer (readline() raises ValueError on
                    # overrun; LimitOverrunError guards the readuntil form).
                    # Surface as an in-stream error rather than letting the
                    # exception crash the node — the `finally` reaps the child.
                    yield {"type": "error",
                           "message": "claude stream-json line exceeded the "
                                      "{}-byte read buffer ({})".format(
                                          _STREAM_LINE_LIMIT, exc)}
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
                        elif btype == "tool_use":
                            # claude runs its OWN tool loop, so this must NEVER be
                            # a `toolcall_end` (the engine would think IT must
                            # execute it). Surface it as a PASSIVE `notice`
                            # instead — inert to every collector, mapped to a
                            # monitoring pulse by the live bridge ("what is
                            # claude doing right now").
                            name = str(block.get("name") or "")
                            block_id = block.get("id")
                            if isinstance(block_id, str) and block_id:
                                tool_names[block_id] = name
                            yield {"type": "notice", "kind": "tool_use", "tool": name}
                        # thinking: deliberately NOT surfaced (claude-internal
                        # reasoning — content capture is a separate concern)
                elif event_type == "user":
                    # claude's tool RESULTS come back as user-role tool_result
                    # blocks — the closing bracket of the tool-activity window.
                    # The block carries only the tool_use_id; resolve it to the
                    # NAME recorded at tool_use time so the pulse reads
                    # "tool Read returned", not an opaque id.
                    for block in (obj.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            ref = block.get("tool_use_id")
                            yield {"type": "notice", "kind": "tool_result",
                                   "tool": tool_names.get(ref, "") if isinstance(ref, str) else ""}
                elif event_type == "result":
                    stop_reason = obj.get("stop_reason") or stop_reason
                    result_model = obj.get("model") or result_model
                    u = obj.get("usage")
                    if isinstance(u, dict):
                        usage = u
                    # An error-subtyped result is the CLI's own failure report
                    # (rate limit, execution error) — delivered on STDOUT, often
                    # with an EMPTY stderr and a nonzero exit. Capture the text
                    # so the exit-nonzero event below can carry the diagnosis
                    # (M44: without it a parallel-call storm traced as bare
                    # "claude exit 1" x12 and was unexplainable).
                    if obj.get("is_error") or str(obj.get("subtype") or "").startswith("error"):
                        for key in ("result", "error"):
                            val = obj.get(key)
                            if isinstance(val, str) and val.strip():
                                result_error = val.strip()
                                break
                # system / rate_limit_event: ignored (diagnostic noise)

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
                # The TAIL of a failing CLI's stderr — the startup banner and any
                # warnings scroll past, the error that actually killed it is the
                # last thing written. This message rides into the harness's
                # failure detail and from there into the trace, so it uses the
                # one shared bound + marker (see trace.bounded_text) and leaves
                # ERROR_MAX headroom for the prefix (see _STDERR_MAX). When the
                # CLI reported its failure as an error-subtyped result event
                # instead (stderr can be EMPTY then — the M44 storm), that text
                # rides too, so "claude exit 1" is never the whole story the
                # child told.
                err_text = bounded(err_bytes.decode(errors="replace"),
                                   _STDERR_MAX, keep="tail") if err_bytes else ""
                detail = ": " + err_text if err_text else ""
                if result_error:
                    detail += "; result: " + bounded(result_error, _RESULT_ERROR_MAX)
                yield {"type": "error",
                       "message": "claude exit {}{}".format(proc.returncode, detail)}
                return

            # Cost bridge (R4/L8): feed the result-event usage to on_usage — the
            # streaming seam's equivalent of the removed --output-format json path, so
            # a plain agent collecting via api_provider.complete() still tracks cost.
            if on_usage is not None and usage is not None:
                on_usage(_map_usage(usage, result_model or model))
            done: Dict[str, Any] = {"type": "done", "stop_reason": stop_reason}
            if usage is not None:
                done["usage"] = usage
            yield done
        finally:
            # Reap the child on EVERY exit path — normal EOF, timeout, an
            # over-limit line, an unexpected parse-loop exception, or the
            # consumer closing the generator. Kill only if it is still alive:
            # the normal and timeout paths already awaited proc.wait() (so
            # returncode is set) and this is a no-op there — it never
            # double-kills a process that already terminated.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass  # already gone between the check and the kill
                await proc.wait()


async def _stderr_tail_of_dead_child(proc: Any) -> str:
    """Best-effort bounded stderr tail from an already-killed/reaped child —
    the timeout-kill path's counterpart of the exit-nonzero stderr capture.
    Called ONLY after kill()+wait() (a dead child cannot block on a full pipe,
    so reading to EOF here cannot deadlock — the live-child ordering concern
    of CRIT-004 does not apply). Best-effort by design: the caller is already
    reporting a timeout, and a stderr salvage that raised or hung would
    replace a clean diagnostic with a worse failure — so any read problem
    yields "" and the timeout event goes out as before."""
    if proc.stderr is None:
        return ""
    try:
        data = await asyncio.wait_for(proc.stderr.read(), timeout=5.0)
    except Exception:
        return ""
    if not data:
        return ""
    return bounded(data.decode(errors="replace"), _STDERR_MAX, keep="tail")


def _prompt_from_messages(messages: List[Dict[str, Any]], system: Optional[str]) -> str:
    """Stitch a context.messages list into a single prompt string for claude -p.
    Picks the most recent user-role string content and prepends a system
    preamble if present. Multi-turn conversation
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


def _map_usage(u: Dict[str, Any], model: Optional[str]) -> Usage:
    """Map claude's raw usage dict to the engine's api_provider.Usage shape.

    The three INPUT classes stay SEPARATE because they bill at different rates
    (see yaah.trace.aggregate). Summing them into one `tokens_in` — what this did
    until 2026-08 — then pricing the sum at the full input rate inflated long
    agentic stages several-fold, worst exactly where caching works best.
    `tokens_in` keeps its back-compat meaning: the tokens priced at the plain
    input rate. Claude's `input_tokens` is already cache-EXCLUSIVE, so no
    subtraction is needed here (litellm's dialect differs — see that provider).

    The field names are PROVIDER-AGNOSTIC (`tokens_cache_read`/`_write`, not
    claude's `cache_read_input_tokens`/`cache_creation_input_tokens`) — the same
    contract litellm_provider reports and yaah.trace.aggregate prices."""
    u = u or {}
    return {"tokens_in": int(u.get("input_tokens", 0) or 0),
            "tokens_cache_read": int(u.get("cache_read_input_tokens", 0) or 0),
            "tokens_cache_write": int(u.get("cache_creation_input_tokens", 0) or 0),
            "tokens_out": int(u.get("output_tokens", 0) or 0),
            "model": model}
