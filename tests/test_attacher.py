"""The Attacher port and AttachingAgent wrapper.

What it proves: the base Attacher class shape (name, requires_capture,
attach()); AttachingAgent calls the inner agent, reads the tracer's last
model_call span FOR THIS CORRELATION, runs attachers, merges results;
nested-agent scenario verifies per-corr lookup returns OUTER's span not
INNER's (review S2 regression test); no-span (None) and empty-attacher
({}) cases attach nothing without raising; collision: later attachers
override earlier on same key. ADR-0003.

Run: cd yaah && PYTHONPATH=src python3 tests/test_attacher.py

Targets Python 3.9+.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from yaah.agents.attacher import Attacher
from yaah.agents.attaching_agent import AttachingAgent
from yaah.core import Envelope, Kind, NodeConfig
from yaah.trace.contributors.cost import CostContributor
from yaah.trace.recording_tracer import RecordingTracer
from yaah.trace.span import Span


def _tracer():
    """A RecordingTracer with cost capture on, so projected records include
    tokens_in/tokens_out/model (the shape attachers actually consume)."""
    return RecordingTracer(contributors=[CostContributor()])


class _FakeAgent:
    """A stand-in Agent that returns a canned reply + optionally emits a
    model_call span via the tracer it was given (mirroring real Agent behavior).
    """
    def __init__(self, reply_payload: Dict[str, Any], *,
                 tracer: Any = None, span_corr: Optional[str] = None,
                 span_tokens_in: int = 0, span_tokens_out: int = 0,
                 span_model: Optional[str] = None) -> None:
        self._reply_payload = reply_payload
        self._tracer = tracer
        self._span_corr = span_corr
        self._span_tokens_in = span_tokens_in
        self._span_tokens_out = span_tokens_out
        self._span_model = span_model

    async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
        if self._tracer is not None and self._span_corr is not None:
            span = Span(id="span-" + self._span_corr, corr=self._span_corr,
                        name="model_call",
                        tokens_in=self._span_tokens_in,
                        tokens_out=self._span_tokens_out,
                        model=self._span_model)
            await self._tracer.emit(span)
        return input.reply(Kind.RESULT, **self._reply_payload)


class _CapturingAttacher(Attacher):
    """Attacher that records what (envelope, span) it was called with, and
    returns a configurable result dict."""
    def __init__(self, *, returns: Dict[str, Any], name: str = "capture"):
        self.name = name
        self.requires_capture = ("cost",)
        self._returns = returns
        self.calls = []  # list of (envelope, span) tuples

    def attach(self, envelope: Envelope,
               span: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        self.calls.append((envelope, span))
        return dict(self._returns)


def _drive(coro):
    return asyncio.run(coro)


def main() -> None:
    # ---- the port itself --------------------------------------------------
    assert Attacher.name == ""
    assert Attacher.requires_capture == ()
    base = Attacher()
    raised = False
    try:
        base.attach(Envelope(Kind.RESULT, {}), {})
    except NotImplementedError:
        raised = True
    assert raised, "base Attacher.attach must raise NotImplementedError"

    # ---- AttachingAgent: happy path ---------------------------------------
    tracer = _tracer()  # captures = frozenset()
    agent = _FakeAgent({"raw": "answer"}, tracer=tracer,
                       span_corr="corr-A",
                       span_tokens_in=100, span_tokens_out=50,
                       span_model="claude-sonnet-4-6")
    a = _CapturingAttacher(returns={"usage": {"tokens": 150}})
    wrapped = AttachingAgent(agent, [a], tracer)
    inp = Envelope(Kind.TASK, {"q": "hi"}, headers={"correlation_id": "corr-A"})
    out = _drive(wrapped.invoke(inp, NodeConfig()))
    assert out.payload["raw"] == "answer", out.payload
    assert out.payload["usage"] == {"tokens": 150}, out.payload
    # the attacher was called with the agent's reply envelope + the model_call span
    assert len(a.calls) == 1
    seen_env, seen_span = a.calls[0]
    assert seen_env.payload["raw"] == "answer"
    assert seen_span is not None
    assert seen_span["name"] == "model_call"
    assert seen_span["corr"] == "corr-A"

    # ---- empty attacher result: payload unchanged -------------------------
    tracer2 = _tracer()
    agent2 = _FakeAgent({"raw": "answer2"}, tracer=tracer2,
                        span_corr="corr-B")
    a_empty = _CapturingAttacher(returns={})
    wrapped2 = AttachingAgent(agent2, [a_empty], tracer2)
    inp2 = Envelope(Kind.TASK, {"q": "hi2"}, headers={"correlation_id": "corr-B"})
    out2 = _drive(wrapped2.invoke(inp2, NodeConfig()))
    assert out2.payload == {"raw": "answer2"}, out2.payload  # no extra keys

    # ---- no span emitted: attacher gets None, still runs ------------------
    tracer3 = _tracer()
    agent3 = _FakeAgent({"raw": "answer3"})  # no tracer/span emission
    a_no_span = _CapturingAttacher(returns={"placeholder": "x"})
    wrapped3 = AttachingAgent(agent3, [a_no_span], tracer3)
    inp3 = Envelope(Kind.TASK, {}, headers={"correlation_id": "corr-C"})
    out3 = _drive(wrapped3.invoke(inp3, NodeConfig()))
    assert a_no_span.calls[0][1] is None, "attacher must see None when no span"
    assert out3.payload["placeholder"] == "x", "attacher's result must still merge"

    # ---- multiple attachers compose; later wins on collision --------------
    tracer4 = _tracer()
    agent4 = _FakeAgent({"raw": "answer4"}, tracer=tracer4, span_corr="corr-D")
    a_first = _CapturingAttacher(returns={"shared": "first", "only_first": 1})
    a_second = _CapturingAttacher(returns={"shared": "second", "only_second": 2})
    wrapped4 = AttachingAgent(agent4, [a_first, a_second], tracer4)
    inp4 = Envelope(Kind.TASK, {}, headers={"correlation_id": "corr-D"})
    out4 = _drive(wrapped4.invoke(inp4, NodeConfig()))
    assert out4.payload["shared"] == "second", "later attacher must override on collision"
    assert out4.payload["only_first"] == 1
    assert out4.payload["only_second"] == 2

    # ---- the regression for review S2: nested-agent case ------------------
    # Both inner and outer agents share the tracer. Inner emits its span LAST
    # (later in time), but the outer's `result.correlation_id` is "outer".
    # The attacher MUST see outer's span, not inner's, even though inner's
    # span is the most-recently-emitted globally.
    tracer5 = _tracer()
    # outer agent's reply has corr="outer". We simulate the inner agent's span
    # being emitted (via tracer) AFTER the outer agent's, but with corr="inner".
    outer_agent = _FakeAgent({"raw": "outer-answer"}, tracer=tracer5,
                             span_corr="outer", span_tokens_in=10,
                             span_model="sonnet")
    # manually emit an inner-agent span AFTER outer's invoke, before reading
    inner_span = Span(id="span-inner", corr="inner", name="model_call",
                      tokens_in=999, model="haiku")

    class _OuterSimulator:
        """Simulates the harness wrapping: emit outer's span, then inner's
        (nested call), then return outer's reply for the attacher to read."""
        async def invoke(self, input, config):
            reply = await outer_agent.invoke(input, config)   # emits outer span
            await tracer5.emit(inner_span)                     # then inner span
            return reply

    a_nested = _CapturingAttacher(returns={"saw_corr": "set-below"})
    # Build a custom attacher that records the SPAN's corr for verification
    class _CorrAttacher(Attacher):
        name = "corr_check"
        requires_capture = ()
        def attach(self, envelope, span):
            return {"saw_corr": (span or {}).get("corr"),
                    "saw_tokens_in": (span or {}).get("tokens_in")}
    wrapped5 = AttachingAgent(_OuterSimulator(), [_CorrAttacher()], tracer5)
    inp5 = Envelope(Kind.TASK, {}, headers={"correlation_id": "outer"})
    out5 = _drive(wrapped5.invoke(inp5, NodeConfig()))
    # the bug we're guarding against: parameter-less last_model_call_span
    # would return the inner span (last-emitted globally). The per-corr
    # lookup must return outer's span (corr=="outer").
    assert out5.payload["saw_corr"] == "outer", (
        "per-corr lookup broken: attacher saw {!r} (expected 'outer'). The "
        "S2 regression has reappeared.".format(out5.payload["saw_corr"]))
    assert out5.payload["saw_tokens_in"] == 10, out5.payload

    # ---- pass-through on failed verdict (ADR-0004 interaction) ------------
    # With parse-by-default, an inner agent may emit a Kind.VERDICT envelope
    # (parse failure). The wrapper must pass it through unchanged — merging
    # attacher keys onto a verdict either gets ignored by the harness or
    # collides with the verdict's own contract.
    tracer6 = _tracer()

    class _FailedVerdictAgent:
        async def invoke(self, input: Envelope, config: NodeConfig) -> Envelope:
            return input.reply(Kind.VERDICT, status="fail", severity="hard",
                               failures=[{"code": "not_json",
                                          "message": "bad output",
                                          "fix_hint": "return JSON"}])

    a6 = _CapturingAttacher(returns={"usage": {"tokens": 999}})
    wrapped6 = AttachingAgent(_FailedVerdictAgent(), [a6], tracer6)
    inp6 = Envelope(Kind.TASK, {}, headers={"correlation_id": "corr-F"})
    out6 = _drive(wrapped6.invoke(inp6, NodeConfig()))
    assert out6.kind == Kind.VERDICT, out6.kind
    assert out6.payload["status"] == "fail", out6.payload
    assert "usage" not in out6.payload, "attacher must NOT merge onto failed verdict"
    assert a6.calls == [], "attacher must NOT fire when inner returned a verdict"

    print("PASS attacher port + wrapper: happy, empty, no-span, collision, S2, verdict pass-through")


if __name__ == "__main__":
    main()
