"""Agent tool-loop: a model-initiated tool call, run offline.

A ScriptedToolBackend "decides" to call a tool, the loop executes the tool's
impl (fn: or node:), feeds the result back, and the model "answers". No network.

Run: cd yaah && PYTHONPATH=src python3 tests/test_agent_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah import Envelope, InProcessComms, Kind, NodeConfig
from yaah.agents import Agent, RoutingBackend, ScriptedToolBackend, Tool

# fn: tool impl — records its args to a file (shared across module instances; the
# resolver imports this module by name, which is a different object than __main__
# when run as a script, so a module-global wouldn't be visible to the test).
_MARKER = os.path.join(tempfile.gettempdir(), "yaah_tool_marker.json")


def record(args: dict) -> dict:
    with open(_MARKER, "w") as f:
        json.dump(args, f)
    return {"recorded": True}


class Adder:  # node: tool impl — a tool that IS another node over Comms
    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        return input.reply(Kind.RESULT, sum=input.payload["a"] + input.payload["b"])


async def scenario_fn_tool() -> None:
    if os.path.exists(_MARKER):
        os.remove(_MARKER)
    backend = RoutingBackend({"tool": ScriptedToolBackend([
        {"calls": [{"id": "c1", "name": "rec", "args": {"x": 1}}]},  # model calls the tool
        {"text": "done"},                                            # then answers
    ])}, default="tool")
    agent = Agent(backend, template="ignored", parse=False,
                  tools=[Tool("rec", "fn:test_agent_tools:record", "record args")])
    out = await agent.invoke(Envelope(Kind.TASK, {}), NodeConfig(model="tool:m"))

    assert os.path.exists(_MARKER) and json.load(open(_MARKER)) == {"x": 1}  # tool executed
    assert out.payload["raw"] == "done", out.payload
    os.remove(_MARKER)


async def scenario_node_tool() -> None:
    comms = InProcessComms()
    comms.register("role:adder", Adder())
    backend = RoutingBackend({"tool": ScriptedToolBackend([
        {"calls": [{"id": "c1", "name": "add", "args": {"a": 2, "b": 3}}]},
        {"text": "the sum is 5"},
    ])}, default="tool")
    # events=comms so the loop can resolve the node: tool impl over Comms
    agent = Agent(backend, template="ignored", events=comms, parse=False,
                  tools=[Tool("add", "node:role:adder", "add two ints")])
    out = await agent.invoke(Envelope(Kind.TASK, {}), NodeConfig(model="tool:m"))
    assert out.payload["raw"] == "the sum is 5", out.payload


async def scenario_no_tools_still_completes() -> None:
    """An agent without tools (or a backend without turn) uses plain complete."""
    backend = RoutingBackend({"tool": ScriptedToolBackend([], default="plain")}, default="tool")
    agent = Agent(backend, template="hi", parse=False)
    out = await agent.invoke(Envelope(Kind.TASK, {}), NodeConfig(model="tool:m"))
    assert out.payload["raw"] == "plain", out.payload


async def scenario_multiturn_wire_shape_and_usage() -> None:
    """H3 + M3: across a 2-turn tool loop, (a) the assistant tool_calls recorded in
    history must be in OpenAI/litellm WIRE shape (so turn 2 isn't malformed), and
    (b) per-turn token usage must ACCUMULATE into the one model_call span."""
    from yaah.trace import RecordingTracer
    from yaah.trace.contributors import CostContributor

    seen: dict = {}

    class TwoTurn:  # a tool-capable backend that reports usage each turn
        def __init__(self) -> None:
            self.n = 0

        async def complete(self, prompt, *, model=None, **opts):
            return ""                                          # ToolBackend Protocol requires this

        async def turn(self, messages, tools, *, model=None, **opts):
            self.n += 1
            on_usage = opts.get("on_usage")
            if self.n == 1:
                if on_usage:
                    on_usage({"tokens_in": 5, "tokens_out": 2, "model": model})
                return {"calls": [{"id": "c1", "name": "rec", "args": {"x": 1}}]}
            # turn 2: the assistant tool_calls entry from turn 1 is now in history
            seen["asst"] = [m for m in messages
                            if m.get("role") == "assistant" and m.get("tool_calls")]
            if on_usage:
                on_usage({"tokens_in": 3, "tokens_out": 1, "model": model})
            return {"text": "done"}

    tracer = RecordingTracer([CostContributor()])  # cost capture on -> on_usage flows
    agent = Agent(TwoTurn(), template="ignored", tracer=tracer, parse=False,
                  tools=[Tool("rec", "fn:test_agent_tools:record", "rec args")])
    if os.path.exists(_MARKER):
        os.remove(_MARKER)
    out = await agent.invoke(Envelope(Kind.TASK, {}), NodeConfig(model="m"))
    os.remove(_MARKER)
    assert out.payload["raw"] == "done"

    # H3: the assistant tool_calls entry is wire-shaped (type/function/arguments-as-string)
    tc = seen["asst"][0]["tool_calls"][0]
    assert tc["type"] == "function" and tc["function"]["name"] == "rec"
    assert isinstance(tc["function"]["arguments"], str)         # JSON string, not a dict
    assert json.loads(tc["function"]["arguments"]) == {"x": 1}

    # M3: the model_call span summed both turns' tokens (not just the last)
    mc = [r for r in tracer.records if r["name"] == "model_call"][0]
    assert mc["tokens_in"] == 8 and mc["tokens_out"] == 3, mc


# ---- R11: tool manifest -----------------------------------------------------

async def scenario_manifest_renders_from_tool_specs() -> None:
    # render_tool_manifest is pure — deterministic Markdown from the Tool list.
    from yaah.agents.manifest import render_tool_manifest
    tools = [
        Tool(name="fetch", impl=lambda a: a, description="Read changed code",
             schema={"type": "object"}, usage="Run: bash $REPO/tools/fetch.sh"),
        Tool(name="grep", impl="fn:tools:grep", description="Search the tree"),
        Tool(name="ghost", impl=lambda a: a, description="usage-less closure"),
    ]
    out = render_tool_manifest(tools)
    assert "## Tools you can call" in out
    assert "**fetch**" in out and "Read changed code" in out
    assert "Run: bash $REPO/tools/fetch.sh" in out               # author's usage rendered verbatim
    assert "**grep**" in out
    # a call_target-impl tool with no explicit usage keeps the default
    # "output JSON tool_call ..." hint
    assert "tool_call" in out and "grep" in out
    # assessment #10: a usage-less CLOSURE tool is omitted — nothing on the
    # prompt-side path can execute it, so advertising it would be a lie
    assert "ghost" not in out, out
    # empty list -> empty string (so {{tool_manifest}} cleanly vanishes)
    assert render_tool_manifest([]) == ""


class _CompleteOnlyBackend:
    """Records the prompt complete() was called with — no `turn` method, so the
    agent's complete-only path runs (the claude_cli shape)."""
    def __init__(self) -> None:
        self.last_prompt = None

    async def complete(self, prompt, *, model=None, **opts):
        self.last_prompt = prompt
        return "done"


class _TurnCapableBackend(_CompleteOnlyBackend):
    """Same as above + a turn() so the agent takes the function-calling path."""
    async def turn(self, prompt, tools, *, model=None, **opts):
        self.last_prompt = prompt
        return {"text": "done"}


async def scenario_manifest_injected_when_backend_lacks_turn() -> None:
    # complete-only backend + tools + a prompt that uses {{tool_manifest}} →
    # the manifest replaces the placeholder; the model sees how to call the tool.
    be = _CompleteOnlyBackend()
    a = Agent(be, template="Review the code.\n\n{{tool_manifest}}", stage="r", parse=False,
              tools=[Tool(name="fetch", impl=lambda a: a,
                          description="Read changed code",
                          usage="Run: bash $REPO/tools/fetch.sh")])
    await a.invoke(Envelope(Kind.TASK, {}, {"correlation_id": "c1"}), NodeConfig())
    assert "Review the code." in be.last_prompt
    assert "## Tools you can call" in be.last_prompt
    assert "Run: bash $REPO/tools/fetch.sh" in be.last_prompt


async def scenario_manifest_empty_when_backend_has_turn() -> None:
    # turn-capable backend: schema is delivered via function-calling, the
    # manifest is redundant. {{tool_manifest}} renders to empty so the prompt
    # body stays clean. run_tool_loop normalizes prompt into a messages list,
    # so we assert on the stringified shape.
    be = _TurnCapableBackend()
    a = Agent(be, template="Review.\n{{tool_manifest}}END", stage="r", parse=False,
              tools=[Tool(name="fetch", impl=lambda a: a, description="x")])
    await a.invoke(Envelope(Kind.TASK, {}, {"correlation_id": "c1"}), NodeConfig())
    captured = str(be.last_prompt)
    assert "## Tools you can call" not in captured
    assert "Review.\\nEND" in captured or "Review.\nEND" in captured  # placeholder vanished


async def scenario_manifest_injected_behind_router_without_turn() -> None:
    """H4 regression (assessment 2026-06-10): RoutingBackend defines `turn`
    itself, so the old isinstance(backend, ToolBackend) check on the ROUTER was
    always true — a complete-only provider (the claude_cli shape) routed through
    it skipped the R11 manifest AND crashed in run_tool_loop with 'does not
    support tool-use'. Capability is now resolved per-route: the manifest
    renders, complete() runs, no crash."""
    leaf = _CompleteOnlyBackend()
    router = RoutingBackend({"cli": leaf}, default="cli")
    # closure tool WITH an author usage line — renderable on the prompt side
    # (a usage-less closure would be skipped, assessment #10)
    a = Agent(router, template="Review.\n{{tool_manifest}}", stage="r", parse=False,
              tools=[Tool(name="fetch", impl=lambda a: a, description="Read changed code",
                          usage="Run: bash fetch-changed-code.sh")])
    out = await a.invoke(Envelope(Kind.TASK, {}, {"correlation_id": "c1"}),
                         NodeConfig(model="cli:m"))
    assert out.payload["raw"] == "done", out.payload
    assert "## Tools you can call" in leaf.last_prompt, "manifest fallback must render"

    # and a TURN-capable leaf behind the same router still takes the tool loop
    leaf2 = ScriptedToolBackend([{"text": "looped"}])
    router2 = RoutingBackend({"tool": leaf2}, default="tool")
    a2 = Agent(router2, template="Go.\n{{tool_manifest}}", stage="r", parse=False,
               tools=[Tool(name="fetch", impl=lambda a: a, description="x")])
    out2 = await a2.invoke(Envelope(Kind.TASK, {}, {"correlation_id": "c2"}),
                           NodeConfig(model="tool:m"))
    assert out2.payload["raw"] == "looped", out2.payload


async def scenario_manifest_omits_uncallable_closure_tools() -> None:
    # assessment #10 (revises the old R9+R11 expectation): envelope_get is a
    # closure with no author `usage` — on the prompt-side path NOTHING parses
    # the generic `output '{"tool_call"...}'` fallback back, so advertising it
    # to a complete-only model invites calls into a void. It must NOT render;
    # behind a turn-capable backend it still flows via function-calling.
    be = _CompleteOnlyBackend()
    a = Agent(be, template="{{tool_manifest}}", stage="r",
              expose={"payload": ["diff"], "header": []}, parse=False)
    await a.invoke(Envelope(Kind.TASK, {"diff": "x"}, {"correlation_id": "c1"}),
                   NodeConfig())
    assert "envelope_get" not in be.last_prompt, be.last_prompt
    assert "## Tools you can call" not in be.last_prompt, be.last_prompt


async def main() -> None:
    await scenario_fn_tool()
    await scenario_node_tool()
    await scenario_no_tools_still_completes()
    await scenario_multiturn_wire_shape_and_usage()
    await scenario_manifest_renders_from_tool_specs()
    await scenario_manifest_injected_when_backend_lacks_turn()
    await scenario_manifest_empty_when_backend_has_turn()
    await scenario_manifest_injected_behind_router_without_turn()
    await scenario_manifest_omits_uncallable_closure_tools()
    await scenario_tool_loop_skips_malformed_calls()
    await scenario_tool_loop_survives_raising_tool()
    # B8 — new kwargs on run_tool_loop
    await scenario_b8_return_meta_completed_returns_tuple()
    await scenario_b8_return_meta_exhausted_does_not_raise()
    await scenario_b8_return_meta_empty_response_outcome()
    await scenario_b8_messages_kwarg_skips_prompt_arg()
    await scenario_b8_system_prepends_as_role_message()
    await scenario_b8_default_return_shape_unchanged()
    # MED-002 — run_tool_loop consumes provider.stream(); on_event seam
    await scenario_med2_on_event_receives_stream_events()
    await scenario_med2_turn_only_backend_falls_back_no_events()
    await scenario_med2_on_event_can_be_async()
    await scenario_med2_streaming_backend_drives_full_loop()
    print("ok")


async def scenario_tool_loop_skips_malformed_calls() -> None:
    # assessment cluster 3 B3: a backend that returns calls without `name`
    # (or non-dict garbage) must NOT crash the loop with KeyError; malformed
    # calls are filtered, real ones run.
    from yaah.agents.tool_loop import run_tool_loop

    class _StubBackend:
        def __init__(self) -> None:
            self._turn = 0

        async def turn(self, messages, schemas, *, model=None, **opts):
            self._turn += 1
            if self._turn == 1:
                return {"calls": [
                    {"name": "real", "args": {}, "id": "ok"},  # legit
                    {"args": {}},                              # no name -> skip
                    "garbage",                                  # not dict -> skip
                ]}
            return {"text": "done"}

    def real(args):
        return {"ok": True}

    tool = Tool(name="real", impl=real)
    out = await run_tool_loop(_StubBackend(), "p", [tool])
    assert out == "done"


async def scenario_tool_loop_survives_raising_tool() -> None:
    # assessment #10: a raising tool impl must NOT abort the agent's invoke —
    # the model receives the error as the tool result and decides what to do.
    from yaah.agents.tool_loop import run_tool_loop

    class _StubBackend:
        def __init__(self) -> None:
            self._turn = 0
            self.last_messages = None

        async def turn(self, messages, schemas, *, model=None, **opts):
            self._turn += 1
            self.last_messages = messages
            if self._turn == 1:
                return {"calls": [{"name": "boom", "args": {}, "id": "c1"}]}
            return {"text": "recovered"}

    def boom(args):
        raise RuntimeError("tool exploded")

    be = _StubBackend()
    out = await run_tool_loop(be, "p", [Tool(name="boom", impl=boom)])
    assert out == "recovered"
    # the error went back to the model as the tool-result message
    tool_msgs = [m for m in be.last_messages if m.get("role") == "tool"]
    assert tool_msgs and "tool exploded" in tool_msgs[-1]["content"], tool_msgs


# ---- B8: new kwargs on run_tool_loop --------------------------------------

class _RecordingBackend:
    """A scripted ToolBackend that records every messages list it sees so the
    B8 tests can assert on prompt construction. Each turn pops the next
    scripted response; out-of-script returns {} (signals empty_response)."""

    def __init__(self, scripted_responses):
        self._responses = list(scripted_responses)
        self._i = 0
        self.seen_messages = []  # one list per turn

    async def turn(self, messages, tools, *, model=None, **opts):
        self.seen_messages.append([dict(m) for m in messages])
        if self._i < len(self._responses):
            out = self._responses[self._i]
            self._i += 1
            return out
        return {}                                            # exhausted script


async def scenario_b8_return_meta_completed_returns_tuple() -> None:
    # return_meta=True: instead of returning bare str, return (text, meta)
    # where meta describes turns + outcome. completion outcome on a 1-turn
    # final-text response.
    from yaah.agents.tool_loop import run_tool_loop
    be = _RecordingBackend([{"text": "answer"}])
    out = await run_tool_loop(be, "go", [], return_meta=True)
    assert isinstance(out, tuple), "return_meta=True must return tuple"
    text, meta = out
    assert text == "answer"
    assert meta["outcome"] == "completed", meta
    assert meta["turns"] == 1, meta


async def scenario_b8_return_meta_exhausted_does_not_raise() -> None:
    # Pre-B8: max_iters exhaustion raised RuntimeError.
    # Post-B8 with return_meta=True: returns ("", meta) with outcome
    # "max_turns_exhausted". The raise stays for legacy callers (no
    # return_meta) — verified separately in test_default_return_shape.
    from yaah.agents.tool_loop import run_tool_loop

    class _NeverFinishes:
        async def turn(self, messages, tools, *, model=None, **opts):
            return {"calls": [{"id": "c1", "name": "noop", "args": {}}]}

    def noop(args):
        return {}

    out = await run_tool_loop(
        _NeverFinishes(), "go", [Tool(name="noop", impl=noop)],
        max_iters=2, return_meta=True,
    )
    text, meta = out
    assert text == "", text
    assert meta["outcome"] == "max_turns_exhausted", meta
    assert meta["turns"] == 2, meta


async def scenario_b8_return_meta_empty_response_outcome() -> None:
    # A turn that returns neither text nor calls: pre-B8 inline loop in
    # AgentLoopNode surfaced this as outcome="empty_response". Post-B8 the
    # canonical loop emits the same.
    from yaah.agents.tool_loop import run_tool_loop
    be = _RecordingBackend([{}])  # explicit empty turn
    out = await run_tool_loop(be, "go", [], return_meta=True)
    text, meta = out
    assert text == "", text
    assert meta["outcome"] == "empty_response", meta
    assert meta["turns"] == 1, meta


async def scenario_b8_messages_kwarg_skips_prompt_arg() -> None:
    # When messages=[...] is passed, run_tool_loop must use it directly and
    # ignore the prompt arg. AgentLoopNode relies on this to construct its
    # own user message from the input envelope's goal.
    from yaah.agents.tool_loop import run_tool_loop
    be = _RecordingBackend([{"text": "ok"}])
    custom = [{"role": "user", "content": "USE_THIS_NOT_THE_PROMPT"}]
    await run_tool_loop(be, "IGNORED_PROMPT", [], messages=custom)
    assert be.seen_messages[0] == custom, be.seen_messages
    # the prompt arg was NOT injected as a separate user message
    assert all("IGNORED_PROMPT" not in m.get("content", "")
               for m in be.seen_messages[0]), be.seen_messages


async def scenario_b8_system_prepends_as_role_message() -> None:
    # The B8 plan moves system from `backend.turn(system=...)` kwarg into
    # the messages list as a system-role message — the OpenAI/Anthropic
    # convention. Verify the system message lands at index 0 with the
    # provided content.
    from yaah.agents.tool_loop import run_tool_loop
    be = _RecordingBackend([{"text": "ok"}])
    await run_tool_loop(be, "user prompt", [], system="be terse")
    first_turn = be.seen_messages[0]
    assert first_turn[0] == {"role": "system", "content": "be terse"}, first_turn
    # user message follows the system message
    user_msgs = [m for m in first_turn if m.get("role") == "user"]
    assert user_msgs and user_msgs[0]["content"] == "user prompt", first_turn


async def scenario_b8_default_return_shape_unchanged() -> None:
    # Legacy callers (no return_meta kwarg) MUST still see:
    #   - bare str return on completion
    #   - RuntimeError on max_iters exhaustion
    # Otherwise B8 breaks Agent.invoke (which calls run_tool_loop without
    # the new kwargs and unpacks the result as a plain string).
    from yaah.agents.tool_loop import run_tool_loop

    # completion path
    be1 = _RecordingBackend([{"text": "plain string"}])
    out = await run_tool_loop(be1, "go", [])
    assert isinstance(out, str), type(out).__name__
    assert out == "plain string"

    # exhaustion path
    class _NeverFinishes:
        async def turn(self, messages, tools, *, model=None, **opts):
            return {"calls": [{"id": "c1", "name": "noop", "args": {}}]}

    def noop(args):
        return {}

    try:
        await run_tool_loop(_NeverFinishes(), "go", [Tool(name="noop", impl=noop)],
                            max_iters=2)
    except RuntimeError as e:
        assert "max_iters" in str(e), e
        return
    raise AssertionError("legacy callers must still see RuntimeError on exhaustion "
                         "(only return_meta=True suppresses the raise)")


# ---- MED-002: run_tool_loop consumes provider.stream(); on_event seam -------

class _StreamingToolBackend:
    """A streaming backend (ApiProvider shape) scripted turn-by-turn. Unlike
    the turn-only stubs, this one implements stream() so run_tool_loop's
    event-forwarding path is exercised. Each call to stream() emits the next
    scripted turn's events: start -> [text_delta] -> [toolcall_end...] -> done."""

    def __init__(self, turns):
        self._turns = list(turns)        # each: {"text": str?, "calls": [{id,name,args}]?}
        self._i = 0

    def stream(self, context, **opts):
        return self._iter()

    async def _iter(self):
        yield {"type": "start"}
        spec = self._turns[self._i] if self._i < len(self._turns) else {"text": ""}
        self._i += 1
        text = spec.get("text")
        if text:
            yield {"type": "text_delta", "delta": text}
        calls = spec.get("calls") or []
        for c in calls:
            yield {"type": "toolcall_end", "id": c.get("id", c["name"]),
                   "name": c["name"], "args": c.get("args", {})}
        yield {"type": "done", "stop_reason": "tool_use" if calls else "end_turn"}


async def scenario_med2_on_event_receives_stream_events() -> None:
    # The marquee MED-002 behavior: run_tool_loop forwards every StreamEvent
    # to on_event during consumption. This is the H7-advisory / per-token-trace
    # seam the whole ApiProvider protocol was built for. Before MED-002 the
    # events were assembled inside backend.turn() and discarded — unreachable.
    from yaah.agents.tool_loop import run_tool_loop

    seen = []
    backend = _StreamingToolBackend([
        {"text": "reading", "calls": [{"id": "c1", "name": "rf", "args": {"p": "x"}}]},
        {"text": "all set"},
    ])

    def rf(args):
        return {"ok": True}

    out = await run_tool_loop(backend, "go", [Tool(name="rf", impl=rf)],
                              on_event=lambda ev: seen.append(ev["type"]))
    assert out == "all set"
    # turn 1: start, text_delta, toolcall_end, done ; turn 2: start, text_delta, done
    assert "toolcall_end" in seen, seen
    assert seen.count("text_delta") == 2, seen
    assert seen.count("start") == 2 and seen.count("done") == 2, seen


async def scenario_med2_turn_only_backend_falls_back_no_events() -> None:
    # A turn-only backend (no .stream()) must STILL drive the loop via the
    # backend.turn() fallback. on_event simply never fires (documented).
    from yaah.agents.tool_loop import run_tool_loop

    class _TurnOnly:
        def __init__(self): self._t = 0
        async def turn(self, messages, schemas, *, model=None, **opts):
            self._t += 1
            if self._t == 1:
                return {"calls": [{"id": "c1", "name": "rf", "args": {}}]}
            return {"text": "done via turn"}

    seen = []
    out = await run_tool_loop(_TurnOnly(), "go", [Tool(name="rf", impl=lambda a: {"ok": 1})],
                              on_event=lambda ev: seen.append(ev))
    assert out == "done via turn"
    assert seen == [], "on_event must not fire for a turn-only backend (no stream)"


async def scenario_med2_on_event_can_be_async() -> None:
    # on_event may be an async callable — run_tool_loop awaits it.
    from yaah.agents.tool_loop import run_tool_loop

    seen = []

    async def sink(ev):
        seen.append(ev["type"])

    backend = _StreamingToolBackend([{"text": "answer"}])
    out = await run_tool_loop(backend, "go", [], on_event=sink)
    assert out == "answer"
    assert seen == ["start", "text_delta", "done"], seen


async def scenario_med2_streaming_backend_drives_full_loop() -> None:
    # Regression: a streaming backend drives a multi-turn tool loop end-to-end
    # (tool dispatched, result fed back, final text) WITHOUT on_event — the
    # default path post-MED-002 must behave exactly as the old turn() path.
    from yaah.agents.tool_loop import run_tool_loop

    calls_made = []

    def rf(args):
        calls_made.append(args)
        return {"content": "file body"}

    backend = _StreamingToolBackend([
        {"calls": [{"id": "c1", "name": "rf", "args": {"p": "a.txt"}}]},
        {"text": "the file says: file body"},
    ])
    out, meta = await run_tool_loop(backend, "read a.txt", [Tool(name="rf", impl=rf)],
                                    return_meta=True)
    assert out == "the file says: file body"
    assert meta["outcome"] == "completed" and meta["turns"] == 2, meta
    assert calls_made == [{"p": "a.txt"}], calls_made


if __name__ == "__main__":
    asyncio.run(main())
