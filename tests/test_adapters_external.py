"""Unit tests for the external-binding adapters, via INJECTED stubs.

These adapters normally reach an SDK / network (litellm, langfuse, httpx). Each
takes its one external dependency as a constructor arg defaulting to the real
lazy import; here we inject a stub and assert the adapter's OWN logic — argument
shaping, defaults, option merging, response parsing — without any external call.

Run: cd yaah && PYTHONPATH=src python3 tests/test_adapters_external.py
"""
from __future__ import annotations

import asyncio

from yaah.adapters.providers import LiteLLMProvider
from yaah.agents import api_provider as _ap
from yaah.adapters.prompts import HttpPromptSource, LangfusePromptSource
from yaah.adapters.transports.nats_comms import NatsComms, _NatsSubscription
from yaah.core import Envelope, Kind


# ---- LiteLLMProvider ---------------------------------------------------------

def _resp(message: dict) -> dict:
    return {"choices": [{"message": message}]}


async def litellm_complete_shapes_request_and_returns_content() -> None:
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _resp({"content": "hello"})

    be = LiteLLMProvider(acompletion=stub, temperature=0.2)  # default opt
    out = await _ap.complete(be, "ask me", model="gpt-4o", max_tokens=10)  # per-call opt

    assert out == "hello"
    assert seen["model"] == "gpt-4o"
    assert seen["messages"] == [{"role": "user", "content": "ask me"}]
    # default opts AND per-call opts both reach the SDK
    assert seen["temperature"] == 0.2 and seen["max_tokens"] == 10


async def litellm_defaults_model_when_unset() -> None:
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _resp({"content": "x"})

    await _ap.complete(LiteLLMProvider(acompletion=stub), "p")
    assert seen["model"] == "gpt-4o-mini"  # the documented fallback


async def litellm_strips_agent_only_opts() -> None:
    # assessment #9: cwd/mcp/allowed_tools/permission_mode are Agent plumbing for
    # claude-native backends — forwarded to litellm they 400 the API and leak
    # host paths / infra endpoints to an external provider. Must never reach the SDK.
    agent_opts = {"cwd": "/Users/someone/secret-repo", "mcp": {"srv": {}},
                  "allowed_tools": ["Edit"], "permission_mode": "acceptEdits"}
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _resp({"content": "ok"})

    be = LiteLLMProvider(acompletion=stub)
    await _ap.complete(be, "p", temperature=0.1, **agent_opts)
    assert not (set(agent_opts) & set(seen)), seen
    assert seen["temperature"] == 0.1  # real SDK opts still pass through

    seen.clear()
    await be.turn([{"role": "user"}], [], **agent_opts)
    assert not (set(agent_opts) & set(seen)), seen


async def litellm_arms_request_timeout_by_default() -> None:
    # Network-cut / stall protection: with no timeout configured, the provider
    # must inject the finite _DEFAULT_REQUEST_TIMEOUT so an internet cut can't
    # hang the calling stage forever (the sibling of the claude readline
    # watchdog). A regression to no-timeout would refreeze on an internet cut.
    from yaah.adapters.providers.litellm_provider import _DEFAULT_REQUEST_TIMEOUT
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _resp({"content": "ok"})

    await _ap.complete(LiteLLMProvider(acompletion=stub), "p")
    assert seen["timeout"] == _DEFAULT_REQUEST_TIMEOUT, seen


async def litellm_explicit_timeout_wins_including_none() -> None:
    # An explicit timeout wins over the default — from the provider default_opts
    # or a per-call opt, INCLUDING None (opt out to litellm's own default).
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _resp({"content": "ok"})

    # per-call explicit value wins
    await _ap.complete(LiteLLMProvider(acompletion=stub), "p", timeout=5.0)
    assert seen["timeout"] == 5.0, seen

    # explicit None wins (opt-out) — not overwritten by the finite default
    seen.clear()
    await _ap.complete(LiteLLMProvider(acompletion=stub), "p", timeout=None)
    assert "timeout" in seen and seen["timeout"] is None, seen

    # provider-level default_opts timeout is respected
    seen.clear()
    await _ap.complete(LiteLLMProvider(acompletion=stub, timeout=12.0), "p")
    assert seen["timeout"] == 12.0, seen


async def litellm_turn_parses_tool_calls() -> None:
    async def stub(**kwargs):
        assert kwargs["tools"] == [{"name": "t"}]
        return _resp({"tool_calls": [
            {"id": "c1", "function": {"name": "lookup", "arguments": '{"q": 5}'}},
            {"id": "c2", "function": {"name": "noargs"}},  # missing arguments -> {}
        ]})

    out = await LiteLLMProvider(acompletion=stub).turn([{"role": "user"}], [{"name": "t"}])
    assert out == {"calls": [
        {"id": "c1", "name": "lookup", "args": {"q": 5}},
        {"id": "c2", "name": "noargs", "args": {}},
    ]}


async def litellm_turn_returns_text_when_no_calls() -> None:
    async def stub(**kwargs):
        return _resp({"content": "final answer"})

    out = await LiteLLMProvider(acompletion=stub).turn([], [])
    assert out == {"text": "final answer"}


# ---- Defensive parsing (assessment cluster 3 B3 / B4 / B5) -----------------

async def litellm_complete_degrades_on_empty_choices() -> None:
    # B5: an empty / filtered response must not raise IndexError mid-pipeline.
    async def empty(**kwargs):
        return {"choices": []}
    out = await _ap.complete(LiteLLMProvider(acompletion=empty), "x")
    assert out == ""

    async def none_content(**kwargs):
        return {"choices": [{"message": {"content": None}}]}
    out = await _ap.complete(LiteLLMProvider(acompletion=none_content), "x")
    assert out == ""

    async def missing_message(**kwargs):
        return {"choices": [{}]}
    out = await _ap.complete(LiteLLMProvider(acompletion=missing_message), "x")
    assert out == ""


async def litellm_turn_degrades_on_bad_arguments_json() -> None:
    # B4: a streamed/partial `arguments` string that won't parse must degrade
    # to args={} rather than aborting the agent with JSONDecodeError.
    async def stub(**kwargs):
        return {"choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "lookup", "arguments": "{not-json"}},
        ]}}]}
    out = await LiteLLMProvider(acompletion=stub).turn([{}], [{}])
    assert out == {"calls": [{"id": "c1", "name": "lookup", "args": {}}]}


async def litellm_turn_skips_malformed_calls() -> None:
    # B3 (via litellm): a tool_call missing `function.name` is SKIPPED, not
    # crashed-on. If only malformed calls came back, fall through to text.
    async def stub(**kwargs):
        return {"choices": [{"message": {"content": "fallback", "tool_calls": [
            {"id": "c1", "function": {}},                  # no name -> skip
            "not-a-dict",                                  # garbage -> skip
            {"function": {"name": "ok"}},                  # no id -> skip
        ]}}]}
    out = await LiteLLMProvider(acompletion=stub).turn([{}], [{}])
    assert out == {"text": "fallback"}

    async def stub_none(**kwargs):
        return _resp({})  # no content, no tool_calls

    out2 = await LiteLLMProvider(acompletion=stub_none).turn([], [])
    assert out2 == {"text": ""}  # None content coerced to ""


# ---- LangfusePromptSource ---------------------------------------------------

class _Prompt:
    def __init__(self, text):
        self.prompt = text


class _LangfuseStub:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def get_prompt(self, key, **kwargs):
        self.calls.append((key, kwargs))
        return self._result


async def langfuse_passes_label_and_version_and_returns_text() -> None:
    stub = _LangfuseStub(_Prompt("YOU ARE A {{role}}"))
    src = LangfusePromptSource(client=stub, label="production")

    out = await src.get("spec", version=3)
    assert out == "YOU ARE A {{role}}"
    key, kwargs = stub.calls[0]
    assert key == "spec"
    assert kwargs == {"label": "production", "version": 3}  # instance label + call version


async def langfuse_call_label_overrides_instance() -> None:
    stub = _LangfuseStub(_Prompt("p"))
    src = LangfusePromptSource(client=stub, label="production")
    await src.get("spec", label="staging")
    assert stub.calls[0][1] == {"label": "staging"}  # call-level wins, no version key


async def langfuse_no_label_no_version_sends_empty_kwargs() -> None:
    stub = _LangfuseStub(_Prompt("p"))
    await LangfusePromptSource(client=stub).get("spec")
    assert stub.calls[0][1] == {}


async def langfuse_falls_back_to_str_when_no_prompt_attr() -> None:
    stub = _LangfuseStub("a bare string, no .prompt")  # getattr -> None -> str()
    out = await LangfusePromptSource(client=stub).get("spec")
    assert out == "a bare string, no .prompt"


# ---- HttpPromptSource -------------------------------------------------------

async def http_joins_url_strips_slash_and_merges_opts() -> None:
    seen = {}

    async def fetch(url, **opts):
        seen["url"] = url
        seen["opts"] = opts
        return "BODY"

    src = HttpPromptSource("https://prompts.example.com/", fetch=fetch,
                           headers={"x": "1"})  # instance opt
    out = await src.get("spec/v2", timeout=5)   # per-call opt

    assert out == "BODY"
    assert seen["url"] == "https://prompts.example.com/spec/v2"  # trailing slash stripped
    assert seen["opts"] == {"headers": {"x": "1"}, "timeout": 5}  # merged


async def http_applies_default_timeout_when_none_passed() -> None:
    # assessment cluster 5 security #1: httpx defaults to NO timeout — a
    # misbehaving prompt server would hang the agent forever. We inject a
    # 30s default that the call can still override.
    seen = {}

    async def fetch(url, **opts):
        seen.update(opts)
        return "B"

    src = HttpPromptSource("https://x.example.com", fetch=fetch)
    await src.get("p")
    assert seen.get("timeout") == 30.0                  # the default kicked in


# ---- NatsComms.subscribe handle ---------------------------------------------
# Regression guard: harness `_run_clearable` / fork-clear paths call sub.cancel()
# in a `finally:` — synchronously. NATS's native subscription has async
# `.unsubscribe()` and NO `.cancel()`. The adapter MUST normalize: wrap the
# native handle in `_NatsSubscription` so the harness can drop subscriptions
# uniformly across transports. Failing this contract crashes every clearable
# stage + fork-wait over NATS (harness.py:444 / :503).

class _FakeNativeSub:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeNc:
    def __init__(self) -> None:
        self.subs: list = []
        self.published: list = []

    async def subscribe(self, topic, cb, queue=None):
        s = _FakeNativeSub()
        self.subs.append((topic, cb, s))
        return s

    async def publish(self, topic, data):
        self.published.append((topic, data))


class _FakeMsg:
    def __init__(self, data: bytes, reply: str = "_INBOX.1") -> None:
        self.data = data
        self.reply = reply


async def nats_subscribe_returns_handle_with_sync_cancel() -> None:
    comms = NatsComms()
    comms._nc = _FakeNc()  # bypass real connect — only the subscribe path under test

    async def handler(_env):
        return None

    sub = await comms.subscribe("clear", handler)
    # The whole point of the bug: harness does `sub.cancel()` synchronously.
    assert hasattr(sub, "cancel"), type(sub)
    assert callable(sub.cancel)
    # Elegance lever #2 (assessment): the return type now satisfies the
    # promoted `Subscription` Protocol — same shape every transport returns.
    from yaah.comms import Subscription
    assert isinstance(sub, Subscription)


async def nats_cancel_triggers_unsubscribe() -> None:
    comms = NatsComms()
    comms._nc = _FakeNc()

    async def handler(_env):
        return None

    sub = await comms.subscribe("clear", handler)
    sub.cancel()                       # sync — schedules the async unsubscribe
    await asyncio.sleep(0)             # let the scheduled task run
    native = comms._nc.subs[0][2]
    assert native.unsubscribed, "cancel() should fire-and-forget unsubscribe()"


async def nats_serve_replies_error_on_malformed_wire() -> None:
    # assessment #12: a payload that won't parse used to escape the serve
    # callback into NATS's dispatcher — the caller burned the full
    # request_timeout (default 300s) and got a GENERIC timeout. Now the parse
    # is inside the try and the caller gets an immediate Kind.ERROR reply.
    import json
    comms = NatsComms()
    comms._nc = _FakeNc()

    async def handler(env):
        raise AssertionError("handler must not run on malformed wire")

    await comms.serve("role:x", handler)
    cb = comms._nc.subs[0][1]
    await cb(_FakeMsg(b"not json at all"))
    assert len(comms._nc.published) == 1, comms._nc.published
    topic, data = comms._nc.published[0]
    assert topic == "_INBOX.1"
    reply = json.loads(data.decode())
    assert reply["kind"] == "error" and "error" in reply["payload"], reply


async def nats_serve_lets_cancellation_through() -> None:
    # assessment #12: CancelledError is teardown, not a node failure — turning
    # it into an ERROR reply would both mask shutdown AND fake a node error.
    from yaah.core import Envelope as Env
    comms = NatsComms()
    comms._nc = _FakeNc()

    async def handler(env):
        raise asyncio.CancelledError()

    await comms.serve("role:x", handler)
    cb = comms._nc.subs[0][1]
    try:
        await cb(_FakeMsg(Env("task", {}).to_json().encode()))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError must propagate, not become a reply")
    assert comms._nc.published == [], comms._nc.published


# ---- LiteLLMProvider: real chunk streaming (`stream: true`) -------------------
# The SSE chunk contract these encode (OpenAI/litellm convention): each chunk is
# {"choices": [{"delta": {content?, tool_calls?}, "finish_reason": None|"stop"|
# "tool_calls"}], "usage"?: {...}}; tool_calls arrive as INDEXED FRAGMENTS whose
# function.arguments accumulate across chunks; usage rides the final chunk only
# when stream_options include_usage is honored.

def _chunks(*chunks):
    """An async-iterator the stub returns when called with stream=True."""
    async def _iter():
        for c in chunks:
            yield c
    return _iter()


async def litellm_stream_true_yields_incremental_deltas() -> None:
    # THE monitoring case: text arrives as multiple deltas (the live bridge's
    # heartbeat needs >1 pulse opportunity), usage arrives on the final chunk.
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _chunks(
            {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
        )

    be = LiteLLMProvider(acompletion=stub, stream=True)
    events = [ev async for ev in be.stream({"messages": [{"role": "user", "content": "p"}]})]
    assert seen["stream"] is True, seen
    assert seen["stream_options"] == {"include_usage": True}, seen
    types = [e["type"] for e in events]
    assert types == ["start", "text_delta", "text_delta", "done"], events
    assert [e.get("delta") for e in events[1:3]] == ["Hel", "lo"], events
    assert events[-1]["stop_reason"] == "end_turn", events[-1]
    assert events[-1]["usage"] == {"prompt_tokens": 7, "completion_tokens": 2}, events[-1]


async def litellm_stream_reports_usage_to_cost_bridge() -> None:
    # R4: on the streaming path the cost callback fires from the final chunk's usage.
    got = {}

    async def stub(**kwargs):
        return _chunks(
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    be = LiteLLMProvider(acompletion=stub, stream=True)
    async for _ in be.stream({"messages": [], "model": "gpt-4o"},
                             on_usage=lambda u: got.update(u)):
        pass
    assert got == {"tokens_in": 3, "tokens_out": 1, "model": "gpt-4o"}, got


async def litellm_stream_assembles_chunked_tool_calls() -> None:
    # tool_calls arrive as indexed fragments: id+name on the first fragment, the
    # arguments JSON split across chunks. One assembled toolcall_end must come out —
    # NOT a silent single-shot fallback (the claude_cli underdelivery lesson: a
    # tools call with stream:true keeps streaming, no hidden downgrade).
    async def stub(**kwargs):
        assert kwargs.get("stream") is True, "tools must NOT silently disable streaming"
        return _chunks(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "grep", "arguments": '{"q":'}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"x"}'}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )

    be = LiteLLMProvider(acompletion=stub, stream=True)
    events = [ev async for ev in be.stream(
        {"messages": [], "tools": [{"name": "grep"}]})]
    calls = [e for e in events if e["type"] == "toolcall_end"]
    assert len(calls) == 1, events
    assert calls[0]["id"] == "c1" and calls[0]["name"] == "grep", calls
    assert calls[0]["args"] == {"q": "x"}, calls
    assert events[-1] == {"type": "done", "stop_reason": "tool_use"}, events[-1]


async def litellm_stream_tolerates_malformed_chunks() -> None:
    # defensive parity with the single-shot path: junk chunks are skipped, bad
    # accumulated arguments degrade to {}, the stream still finishes with done.
    async def stub(**kwargs):
        return _chunks(
            "not-a-dict",
            {"choices": []},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "t", "arguments": "{broken"}}]},
                "finish_reason": None}]},
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    be = LiteLLMProvider(acompletion=stub, stream=True)
    events = [ev async for ev in be.stream({"messages": []})]
    # deltas arrive mid-stream; assembled calls flush AFTER the stream ends
    # (fragments may accumulate until the last chunk), hence this order:
    types = [e["type"] for e in events]
    assert types == ["start", "text_delta", "toolcall_end", "done"], events
    call = next(e for e in events if e["type"] == "toolcall_end")
    assert call["args"] == {}, call        # unparseable accumulated JSON degrades


class _PydanticishChunk:
    """A ModelResponseStream stand-in: the REAL SDK yields pydantic objects, not
    dicts — the provider must go through `_as_dict` (model_dump) per chunk. The
    dict-stub tests alone would miss an object-path regression (eval finding)."""
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return dict(self._d)


async def litellm_stream_reports_resolved_model_like_single_shot() -> None:
    # eval finding: the single-shot path labels cost with the PROVIDER-RESOLVED
    # model (resp.model, e.g. "gpt-4o-2024-08-06"); the chunked path must do the
    # same — real chunks carry `model` in model_dump() — not silently fall back
    # to the requested name. Divergence skews the per-model mix + fallback pricing.
    got = {}

    async def stub(**kwargs):
        return _chunks(
            _PydanticishChunk({"model": "gpt-4o-2024-08-06",
                               "choices": [{"delta": {"content": "x"},
                                            "finish_reason": None}]}),
            _PydanticishChunk({"model": "gpt-4o-2024-08-06", "choices": [],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 1}}),
        )

    be = LiteLLMProvider(acompletion=stub, stream=True)
    events = [ev async for ev in be.stream({"messages": [], "model": "gpt-4o"},
                                           on_usage=lambda u: got.update(u))]
    assert got.get("model") == "gpt-4o-2024-08-06", got   # resolved, not requested
    # and the object path works end-to-end: delta extracted through model_dump,
    # the empty-choices usage-only final chunk tolerated
    assert [e["type"] for e in events] == ["start", "text_delta", "done"], events


async def litellm_stream_merges_author_stream_options() -> None:
    # eval finding: the hardcoded stream_options CLOBBERED an author's value —
    # no way to opt out of the usage chunk (or dodge a provider that rejects the
    # field's contents). The author's dict must win on conflicts.
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _chunks({"choices": [{"delta": {"content": "x"},
                                     "finish_reason": "stop"}]})

    be = LiteLLMProvider(acompletion=stub, stream=True,
                         stream_options={"include_usage": False, "custom": 1})
    async for _ in be.stream({"messages": []}):
        pass
    assert seen["stream_options"] == {"include_usage": False, "custom": 1}, seen


async def litellm_stream_missing_usage_is_tolerated() -> None:
    # a provider that ignores include_usage: done simply has no usage (the cost
    # report shows a zero-token call — the documented forensic signal, not a crash).
    async def stub(**kwargs):
        return _chunks({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})

    be = LiteLLMProvider(acompletion=stub, stream=True)
    events = [ev async for ev in be.stream({"messages": []})]
    assert events[-1]["type"] == "done" and "usage" not in events[-1], events[-1]


async def litellm_default_stays_single_shot() -> None:
    # no `stream: true` → the legacy single-shot call, byte-identical: the SDK
    # must NOT receive a stream kwarg (the battle-tested path stays the default).
    seen = {}

    async def stub(**kwargs):
        seen.update(kwargs)
        return _resp({"content": "hello"})

    out = await _ap.complete(LiteLLMProvider(acompletion=stub), "p")
    assert out == "hello"
    assert "stream" not in seen and "stream_options" not in seen, seen


async def litellm_stream_midstream_raise_propagates() -> None:
    # a mid-stream failure (network drop) must PROPAGATE, not be swallowed into a
    # silent half-reply: the engine's consumers keep partial state local, so a
    # raise fails the call cleanly (same failure surface as the single-shot path).
    async def stub(**kwargs):
        async def _iter():
            yield {"choices": [{"delta": {"content": "par"}, "finish_reason": None}]}
            raise RuntimeError("connection dropped")
        return _iter()

    be = LiteLLMProvider(acompletion=stub, stream=True)
    try:
        async for _ in be.stream({"messages": []}):
            pass
    except RuntimeError as e:
        assert "connection dropped" in str(e)
        return
    raise AssertionError("mid-stream raise must propagate, not be swallowed")


async def main() -> None:
    for fn in [
        litellm_complete_shapes_request_and_returns_content,
        litellm_defaults_model_when_unset,
        litellm_strips_agent_only_opts,
        litellm_arms_request_timeout_by_default,
        litellm_explicit_timeout_wins_including_none,
        litellm_stream_true_yields_incremental_deltas,
        litellm_stream_reports_usage_to_cost_bridge,
        litellm_stream_reports_resolved_model_like_single_shot,
        litellm_stream_merges_author_stream_options,
        litellm_stream_assembles_chunked_tool_calls,
        litellm_stream_tolerates_malformed_chunks,
        litellm_stream_missing_usage_is_tolerated,
        litellm_default_stays_single_shot,
        litellm_stream_midstream_raise_propagates,
        litellm_turn_parses_tool_calls,
        litellm_turn_returns_text_when_no_calls,
        langfuse_passes_label_and_version_and_returns_text,
        langfuse_call_label_overrides_instance,
        langfuse_no_label_no_version_sends_empty_kwargs,
        langfuse_falls_back_to_str_when_no_prompt_attr,
        http_joins_url_strips_slash_and_merges_opts,
        http_applies_default_timeout_when_none_passed,
        nats_subscribe_returns_handle_with_sync_cancel,
        nats_cancel_triggers_unsubscribe,
        nats_serve_replies_error_on_malformed_wire,
        nats_serve_lets_cancellation_through,
        litellm_complete_degrades_on_empty_choices,
        litellm_turn_degrades_on_bad_arguments_json,
        litellm_turn_skips_malformed_calls,
    ]:
        await fn()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
