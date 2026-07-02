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


async def main() -> None:
    for fn in [
        litellm_complete_shapes_request_and_returns_content,
        litellm_defaults_model_when_unset,
        litellm_strips_agent_only_opts,
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
