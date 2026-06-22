"""Tests for the new ApiProvider protocol (B1).

Cases:
 1. LegacyBackendAdapter wraps FakeBackend (complete-only) correctly
 2. LegacyBackendAdapter wraps a tool-capable backend correctly
 3. Module-level complete() matches FakeBackend.complete() output
 4. Module-level turn() round-trips tool calls through adapter -> assembly
 5. Adapter rejects non-backends
 6. Adapter's tool path errors when wrapping a complete-only backend
 7. assemble_message merges adjacent text deltas into one text block
 8. Errors during streaming surface as `error` events (not raised through)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yaah.agents.api_provider import (  # noqa: E402
    ApiProvider, LegacyBackendAdapter, assemble_message, complete, turn,
)
from yaah.agents.fake_backend import FakeBackend  # noqa: E402


# --- Minimal tool-capable backend for test 2 (no production import needed) ---
class _CannedToolBackend:
    def __init__(self, response):
        self._response = response

    async def complete(self, prompt, *, model=None, **opts):
        return ""

    async def turn(self, messages, tools, *, model=None, **opts):
        return self._response


async def _drain(it):
    return [ev async for ev in it]


def test_adapter_wraps_complete_only_backend():
    backend = FakeBackend(responses=["hello world"])
    adapter = LegacyBackendAdapter(backend)
    events = asyncio.run(_drain(adapter.stream({"messages": [{"role": "user", "content": "hi"}]})))
    types = [e["type"] for e in events]
    assert types == ["start", "text_delta", "done"], types
    assert events[1]["delta"] == "hello world"
    assert events[2]["stop_reason"] == "end_turn"


def test_adapter_wraps_tool_backend_with_calls():
    backend = _CannedToolBackend({
        "calls": [{"id": "c1", "name": "read", "args": {"path": "/a"}}],
    })
    adapter = LegacyBackendAdapter(backend)
    ctx = {"messages": [{"role": "user", "content": "go"}],
           "tools": [{"name": "read", "description": "", "input_schema": {}}]}
    events = asyncio.run(_drain(adapter.stream(ctx)))
    types = [e["type"] for e in events]
    assert types == ["start", "toolcall_end", "done"], types
    tc = events[1]
    assert tc["id"] == "c1" and tc["name"] == "read" and tc["args"] == {"path": "/a"}
    assert events[2]["stop_reason"] == "tool_use"


def test_module_complete_matches_legacy_output():
    backend = FakeBackend(responses=["the answer"])
    legacy_out = asyncio.run(backend.complete("ignored"))
    # NEW backend instance so the canned-response cursor starts fresh.
    new_out = asyncio.run(complete(LegacyBackendAdapter(FakeBackend(responses=["the answer"])), "ignored"))
    assert legacy_out == new_out == "the answer"


def test_module_turn_roundtrips_tool_calls():
    backend = _CannedToolBackend({
        "text": "I'll read it",
        "calls": [{"id": "c1", "name": "read", "args": {"path": "/x"}},
                  {"id": "c2", "name": "read", "args": {"path": "/y"}}],
    })
    provider = LegacyBackendAdapter(backend)
    result = asyncio.run(turn(provider, [{"role": "user", "content": "go"}],
                              [{"name": "read", "description": "", "input_schema": {}}]))
    assert result["text"] == "I'll read it"
    assert result["calls"] == [
        {"id": "c1", "name": "read", "args": {"path": "/x"}},
        {"id": "c2", "name": "read", "args": {"path": "/y"}},
    ]


def test_adapter_rejects_non_backend():
    try:
        LegacyBackendAdapter(object())
    except TypeError as e:
        assert "ModelBackend" in str(e)
    else:
        raise AssertionError("expected TypeError")


def test_adapter_tool_path_errors_on_complete_only_backend():
    adapter = LegacyBackendAdapter(FakeBackend(responses=["x"]))
    ctx = {"messages": [{"role": "user", "content": "go"}],
           "tools": [{"name": "read", "description": "", "input_schema": {}}]}
    events = asyncio.run(_drain(adapter.stream(ctx)))
    # The adapter caught the TypeError and emitted an error event — the stream
    # is still a clean (start, error) sequence, not a raised exception.
    types = [e["type"] for e in events]
    assert types == ["start", "error"], types
    assert "no .turn()" in events[1]["message"]


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


def test_adapter_satisfies_apiprovider_protocol():
    adapter = LegacyBackendAdapter(FakeBackend(responses=["x"]))
    assert isinstance(adapter, ApiProvider), \
        "LegacyBackendAdapter should structurally satisfy ApiProvider"


if __name__ == "__main__":
    test_adapter_wraps_complete_only_backend()
    test_adapter_wraps_tool_backend_with_calls()
    test_module_complete_matches_legacy_output()
    test_module_turn_roundtrips_tool_calls()
    test_adapter_rejects_non_backend()
    test_adapter_tool_path_errors_on_complete_only_backend()
    test_assemble_message_merges_text_deltas()
    test_assemble_message_raises_on_error_event()
    test_adapter_satisfies_apiprovider_protocol()
    print("PASS")
