"""End-to-end test for the agent_loop node.

Drives the AgentLoopNode through a scripted FakeToolBackend; verifies:
- The loop dispatches tool calls via call_target (fn: scheme; no Comms needed)
- Tool results are fed back into the next turn's messages
- A final text response terminates the loop
- The result envelope carries {answer, turns, outcome}
- Errors from a tool flow back as observations, not as a stage failure
- max_turns is enforced
- Construction-time rejection of misconfigured backends + tool specs

Dispatch fns live in a sibling module (tests.fixtures_agent_loop_tools) so
that `import_callable` loads the SAME module that the test inspects —
running the test file as `__main__` would create a second module instance.
"""
from __future__ import annotations

import asyncio
import sys

from yaah.adapters.backends.fake_tool_backend import FakeToolBackend
from yaah.core import Envelope, Kind
from yaah.nodes import AgentLoopNode

from tests import fixtures_agent_loop_tools as fx


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if sys.version_info < (3, 10) \
        else asyncio.run(coro)


def _make(backend, tools, **kw):
    return AgentLoopNode(backend=backend, tools=tools, **kw)


def test_loop_completes_with_final_text():
    fx.calls_log.clear()
    backend = FakeToolBackend(turns=[
        {"text": "thinking", "calls": [{"id": "c1", "name": "tool_ok", "args": {"x": 1}}]},
        {"text": "all done"},
    ])
    node = _make(backend, tools={
        "tool_ok": {"description": "ok tool", "input_schema": {},
                    "dispatch": "fn:tests.fixtures_agent_loop_tools:tool_ok"},
    }, max_turns=5)
    env = Envelope(Kind.TASK, payload={"goal": "do the thing"})
    result = _run(node.invoke(env, {}))
    assert result.kind == Kind.RESULT, result
    assert result.payload["answer"] == "all done"
    assert result.payload["turns"] == 2
    assert result.payload["outcome"] == "completed"
    assert fx.calls_log == [("ok", {"x": 1})], fx.calls_log


def test_tool_error_flows_back_as_observation():
    fx.calls_log.clear()
    backend = FakeToolBackend(turns=[
        {"calls": [{"id": "c1", "name": "tool_boom", "args": {}}]},
        {"text": "recovered"},
    ])
    node = _make(backend, tools={
        "tool_boom": {"description": "fails", "input_schema": {},
                      "dispatch": "fn:tests.fixtures_agent_loop_tools:tool_boom"},
    }, max_turns=5)
    env = Envelope(Kind.TASK, payload={"goal": "trigger error"})
    result = _run(node.invoke(env, {}))
    # The stage SUCCEEDS — the tool exception became an observation the agent saw
    # and the agent then emitted a final text. (No stage failure for tool-internal
    # errors; that's the whole point of bounded tool-loop semantics.)
    assert result.kind == Kind.RESULT
    assert result.payload["answer"] == "recovered"
    assert result.payload["turns"] == 2
    assert fx.calls_log == [("boom", {})], fx.calls_log


def test_unknown_tool_is_an_error_observation_not_a_crash():
    backend = FakeToolBackend(turns=[
        {"calls": [{"id": "c1", "name": "not_in_catalog", "args": {}}]},
        {"text": "noted"},
    ])
    node = _make(backend, tools={
        "tool_ok": {"description": "ok", "input_schema": {},
                    "dispatch": "fn:tests.fixtures_agent_loop_tools:tool_ok"},
    }, max_turns=5)
    env = Envelope(Kind.TASK, payload={"goal": "ask for missing tool"})
    result = _run(node.invoke(env, {}))
    assert result.payload["answer"] == "noted"
    assert result.payload["outcome"] == "completed"


def test_max_turns_exhausted():
    backend = FakeToolBackend(turns=[
        {"calls": [{"id": "c{}".format(i), "name": "tool_ok", "args": {"i": i}}]}
        for i in range(5)
    ])
    node = _make(backend, tools={
        "tool_ok": {"description": "ok", "input_schema": {},
                    "dispatch": "fn:tests.fixtures_agent_loop_tools:tool_ok"},
    }, max_turns=3)
    env = Envelope(Kind.TASK, payload={"goal": "loop forever"})
    result = _run(node.invoke(env, {}))
    assert result.payload["outcome"] == "max_turns_exhausted"
    assert result.payload["turns"] == 3
    assert result.payload["answer"] == ""


def test_backend_without_turn_method_rejected_at_construction():
    class BadBackend:
        async def complete(self, prompt, **kw):
            return "irrelevant"

    try:
        _make(BadBackend(), tools={
            "x": {"description": "", "input_schema": {}, "dispatch": "fn:x:y"},
        })
    except TypeError as e:
        assert "ToolBackend" in str(e), e
        return
    raise AssertionError("expected TypeError for backend without .turn()")


def test_tool_spec_missing_dispatch_rejected_at_construction():
    backend = FakeToolBackend(turns=[])
    try:
        _make(backend, tools={"x": {"description": "", "input_schema": {}}})
    except ValueError as e:
        assert "dispatch" in str(e), e
        return
    raise AssertionError("expected ValueError for tool spec missing 'dispatch'")


if __name__ == "__main__":
    test_loop_completes_with_final_text()
    test_tool_error_flows_back_as_observation()
    test_unknown_tool_is_an_error_observation_not_a_crash()
    test_max_turns_exhausted()
    test_backend_without_turn_method_rejected_at_construction()
    test_tool_spec_missing_dispatch_rejected_at_construction()
    print("OK")
