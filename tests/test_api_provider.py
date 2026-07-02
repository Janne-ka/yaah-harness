"""Tests for the ApiProvider protocol (B1) + module helpers.

After MED-001 the LegacyBackendAdapter was removed (its migration purpose
was complete — every backend implements stream() natively post-B2, and a
new backend author implements stream(), not the legacy turn()). These tests
now exercise the LIVE surface: the module-level complete()/turn() helpers
and assemble_message(), driven by native streaming backends.

Cases:
 1. complete() collects text from a native streaming backend
 2. turn() projects tool_use blocks into the {text, calls} shape
 3. assemble_message merges adjacent text deltas into one text block
 4. assemble_message raises on an error event
 5. a native backend structurally satisfies ApiProvider
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yaah.agents.api_provider import (  # noqa: E402
    ApiProvider, SupportsTurn, assemble_message, complete, stream_of, turn,
)
from yaah.agents.fake_provider import FakeProvider  # noqa: E402
from yaah.adapters.providers import ClaudeCliProvider  # noqa: E402
from yaah.adapters.providers.fake_tool_provider import FakeToolProvider  # noqa: E402


# --- A minimal native streaming tool backend (emits StreamEvents directly) ---
class _StreamingToolBackend:
    """ApiProvider that yields a scripted single turn's events. Replaces the
    old _CannedToolBackend + LegacyBackendAdapter pairing for the turn() test."""

    def __init__(self, *, text=None, calls=None):
        self._text = text
        self._calls = calls or []

    def stream(self, context, **opts):
        return self._iter()

    async def _iter(self):
        yield {"type": "start"}
        if self._text:
            yield {"type": "text_delta", "delta": self._text}
        for c in self._calls:
            yield {"type": "toolcall_end", "id": c["id"], "name": c["name"],
                   "args": c.get("args", {})}
        yield {"type": "done", "stop_reason": "tool_use" if self._calls else "end_turn"}


class _CollectedOnlyToolBackend:
    """A collected-only tool provider: has turn() but NO stream(). Exercises
    stream_of's fallback branch that wraps turn() into a one-shot stream — the
    forward-compat path RoutingProvider.stream documents for legacy/external tool
    backends (no shipped backend hits it; all have native stream())."""

    def __init__(self, *, text=None, calls=None):
        self._text, self._calls = text, calls or []

    async def turn(self, messages, tools, *, model=None, **opts):
        return {"text": self._text, "calls": self._calls}


def test_stream_of_wraps_collected_only_tool_backend():
    be = _CollectedOnlyToolBackend(
        text="done", calls=[{"id": "c1", "name": "read", "args": {"path": "/x"}}])
    ctx = {"messages": [{"role": "user", "content": "go"}],
           "tools": [{"name": "read", "description": "", "input_schema": {}}]}

    async def _drain():
        return [ev async for ev in stream_of(be, ctx)]

    events = asyncio.run(_drain())
    assert [e["type"] for e in events] == \
        ["start", "toolcall_end", "text_delta", "done"], events
    assert events[1] == {"type": "toolcall_end", "id": "c1", "name": "read",
                         "args": {"path": "/x"}}
    assert events[2]["delta"] == "done"


def test_stream_of_turn_only_provider_without_tools_still_calls_the_model():
    # REGRESSION (bug hunt): a turn-only collected provider + NO tools used to
    # fall through both fallback arms -> empty "success" WITHOUT calling the
    # model. turn() must be called whenever present, tools or not.
    calls = []
    class TurnOnly:
        async def turn(self, messages, tools, *, model=None, **opts):
            calls.append((messages, tools))
            return {"text": "answered"}
    out = asyncio.run(complete(TurnOnly(), "the prompt"))
    assert out == "answered" and len(calls) == 1, (out, calls)
    assert calls[0][1] == [], "no tools -> turn still called with an empty list"


def test_stream_of_fails_loud_on_capability_mismatch():
    class CompleteOnly:
        async def complete(self, prompt, *, model=None, **opts):
            return "prose"
    ctx = {"messages": [{"role": "user", "content": "go"}],
           "tools": [{"name": "read", "description": "", "input_schema": {}}]}
    async def _drain(be):
        return [e async for e in stream_of(be, ctx)]
    try:
        asyncio.run(_drain(CompleteOnly()))   # tools need turn(); dropping them = wrong prose
        raise AssertionError("complete-only + tools must fail loud")
    except TypeError as e:
        assert "turn" in str(e), e
    class NoVerbs:
        pass
    try:
        asyncio.run(_drain(NoVerbs()))
        raise AssertionError("a verb-less provider must fail loud")
    except TypeError as e:
        assert "stream" in str(e), e


def test_module_complete_collects_text_from_native_stream():
    # FakeProvider is a native ApiProvider (B2.1); complete() drains its stream
    # into a single string — same result as the backend's own complete().
    out = asyncio.run(complete(FakeProvider(responses=["the answer"]), "ignored"))
    assert out == "the answer"


def test_module_turn_roundtrips_tool_calls():
    backend = _StreamingToolBackend(
        text="I'll read it",
        calls=[{"id": "c1", "name": "read", "args": {"path": "/x"}},
               {"id": "c2", "name": "read", "args": {"path": "/y"}}])
    result = asyncio.run(turn(backend, [{"role": "user", "content": "go"}],
                              [{"name": "read", "description": "", "input_schema": {}}]))
    assert result["text"] == "I'll read it"
    assert result["calls"] == [
        {"id": "c1", "name": "read", "args": {"path": "/x"}},
        {"id": "c2", "name": "read", "args": {"path": "/y"}},
    ]


def test_assemble_message_merges_text_deltas():
    async def _events():
        yield {"type": "start"}
        yield {"type": "text_delta", "delta": "hel"}
        yield {"type": "text_delta", "delta": "lo "}
        yield {"type": "text_delta", "delta": "world"}
        yield {"type": "toolcall_end", "id": "c", "name": "n", "args": {}}
        yield {"type": "text_delta", "delta": " after"}
        yield {"type": "done", "stop_reason": "end_turn"}
    msg = asyncio.run(assemble_message(_events()))
    # Adjacent deltas merge; the toolcall splits the run into two text blocks.
    assert msg["content"] == [
        {"type": "text", "text": "hello world"},
        {"type": "tool_use", "id": "c", "name": "n", "input": {}},
        {"type": "text", "text": " after"},
    ]
    assert msg["stop_reason"] == "end_turn"


def test_assemble_message_raises_on_error_event():
    async def _events():
        yield {"type": "start"}
        yield {"type": "error", "message": "boom"}
    try:
        asyncio.run(assemble_message(_events()))
    except RuntimeError as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_native_backend_satisfies_apiprovider_protocol():
    assert isinstance(FakeProvider(responses=["x"]), ApiProvider), \
        "FakeProvider should structurally satisfy ApiProvider (native stream())"
    assert isinstance(_StreamingToolBackend(text="x"), ApiProvider)


def test_supports_turn_is_a_distinct_optional_capability():
    # The tool-loop capability the engine keys on (isinstance = structural: is
    # `turn` present?). claude_cli deliberately has none — it runs its own tool
    # loop. The DECLARATION side (explicit bases, via __mro__) lives in test_ports.py.
    assert isinstance(FakeToolProvider(turns=[]), SupportsTurn)      # has turn()
    assert isinstance(FakeToolProvider(turns=[]), ApiProvider)
    claude = ClaudeCliProvider()
    assert isinstance(claude, ApiProvider)
    assert not isinstance(claude, SupportsTurn), \
        "claude_cli has no native turn() — the capability must read as absent"
    assert not isinstance(FakeProvider(responses=["x"]), SupportsTurn)


if __name__ == "__main__":
    test_stream_of_wraps_collected_only_tool_backend()
    test_stream_of_turn_only_provider_without_tools_still_calls_the_model()
    test_stream_of_fails_loud_on_capability_mismatch()
    test_module_complete_collects_text_from_native_stream()
    test_module_turn_roundtrips_tool_calls()
    test_assemble_message_merges_text_deltas()
    test_assemble_message_raises_on_error_event()
    test_native_backend_satisfies_apiprovider_protocol()
    test_supports_turn_is_a_distinct_optional_capability()
    print("PASS")
