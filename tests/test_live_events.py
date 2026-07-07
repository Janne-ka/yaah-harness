"""live_events — the StreamEvent→llm_progress bridge (monitoring seam, MED-002 wiring).

The bridge turns a provider's live StreamEvents into point-in-time `llm_progress`
spans on the EXISTING tracer (no second channel), gated by the `live` capture.
These FALSIFY: exact event→span mapping, the heartbeat throttle (a fake clock),
never-breaks-the-model-call (a raising tracer), off-by-default (no `live` capture
→ zero llm_progress spans), and cost-report immunity (aggregate keys on
`model_call`, so progress spans must not leak into invocation counts).

Run: cd yaah && PYTHONPATH=src python3 tests/test_live_events.py
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict

from yaah import Envelope, NodeConfig
from yaah.agents import Agent
from yaah.agents.live_events import make_live_bridge
from yaah.trace import RecordingTracer
from yaah.trace.contributors import LiveContributor, PhaseContributor


# --- doubles -------------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class StreamingBackend:
    """A backend double that streams a scripted event sequence (the plain,
    non-tool path — Agent routes it through `complete()` → `stream_of`)."""
    def __init__(self, events) -> None:
        self._events = events

    def stream(self, context: Dict[str, Any], **opts: Any) -> AsyncIterator[dict]:
        async def _iter() -> AsyncIterator[dict]:
            for ev in self._events:
                yield dict(ev)
        return _iter()


class RaisingTracer(RecordingTracer):
    async def emit(self, span: Any) -> None:  # type: ignore[override]
        raise RuntimeError("sink exploded")


SCRIPT = [
    {"type": "start"},
    {"type": "text_delta", "delta": "hel"},
    {"type": "text_delta", "delta": "lo"},
    {"type": "toolcall_end", "id": "t1", "name": "read_file", "args": {}},
    {"type": "done", "stop_reason": "end_turn"},
]


def _progress(tracer: RecordingTracer):
    return [r for r in tracer.records if r.get("name") == "llm_progress"]


# --- bridge unit: mapping ------------------------------------------------------------------

async def scenario_bridge_maps_semantic_events() -> None:
    tracer = RecordingTracer([LiveContributor()])
    bridge = make_live_bridge(tracer, "c1", parent="p1", stage="verify",
                              clock=FakeClock(), min_interval_s=2.0)
    for ev in SCRIPT:
        await bridge(ev)
    got = _progress(tracer)
    events = [r["event"] for r in got]
    assert events == ["start", "progress", "tool_call", "done"], got
    by = {r["event"]: r for r in got}
    assert by["tool_call"]["tool"] == "read_file", by["tool_call"]
    assert by["done"]["stop_reason"] == "end_turn", by["done"]
    assert all(r["stage"] == "verify" for r in got), got
    assert all(r["corr"] == "c1" for r in got), got
    # point-in-time: emitted spans have t0 == t1 (a pulse, not a duration)
    assert all(s.t_start == s.t_end for s in tracer.spans), tracer.spans
    # cost-report immunity: no token fields on a progress record
    assert all("tokens_in" not in r or not r["tokens_in"] for r in got), got


async def scenario_bridge_error_event_maps() -> None:
    tracer = RecordingTracer([LiveContributor()])
    bridge = make_live_bridge(tracer, "c1", clock=FakeClock())
    await bridge({"type": "error", "message": "boom"})
    got = _progress(tracer)
    assert len(got) == 1 and got[0]["event"] == "error" and got[0]["message"] == "boom", got


# --- bridge unit: heartbeat throttle -------------------------------------------------------

async def scenario_heartbeat_throttles_deltas() -> None:
    # FIRST delta emits immediately (liveness), then deltas inside the interval are
    # coalesced; the next emit after the interval carries the CUMULATIVE char count.
    clock = FakeClock()
    tracer = RecordingTracer([LiveContributor()])
    bridge = make_live_bridge(tracer, "c1", clock=clock, min_interval_s=2.0)
    await bridge({"type": "text_delta", "delta": "aaaa"})       # t=100 → emits (chars=4)
    clock.now = 101.0
    await bridge({"type": "text_delta", "delta": "bb"})         # +1s  → suppressed
    clock.now = 101.9
    await bridge({"type": "text_delta", "delta": "c"})          # +1.9s → suppressed
    clock.now = 102.5
    await bridge({"type": "text_delta", "delta": "dd"})         # +2.5s → emits (chars=9)
    got = _progress(tracer)
    assert [r["event"] for r in got] == ["progress", "progress"], got
    assert got[0]["chars"] == 4 and got[1]["chars"] == 9, got


# --- bridge unit: passive `notice` events (claude_cli tool activity) -----------------------

async def scenario_bridge_maps_notice_to_tool_pulse() -> None:
    # claude_cli surfaces its INTERNAL tool activity as passive `notice` events
    # (never toolcall_end — claude runs its own loop). The bridge maps them onto
    # the existing tool_call pulse vocabulary so sinks need zero changes.
    tracer = RecordingTracer([LiveContributor()])
    bridge = make_live_bridge(tracer, "c1", stage="s1", clock=FakeClock())
    await bridge({"type": "notice", "kind": "tool_use", "tool": "Read"})
    await bridge({"type": "notice", "kind": "tool_result", "tool": "Read"})
    got = _progress(tracer)
    assert [(r["event"], r.get("tool")) for r in got] == \
        [("tool_call", "Read"), ("tool_result", "Read")], got


async def scenario_notice_is_inert_to_the_tool_loop() -> None:
    # a notice in the stream must NEVER become a call the engine executes: the
    # tool loop's collector ignores unknown event types (research-verified; this
    # pins it). The backend streams a notice + text + done; no tool runs.
    from yaah.agents.tool import Tool
    from yaah.agents.tool_loop import run_tool_loop
    calls = []

    async def _impl(**kw):                       # would be hit if a call leaked
        calls.append(kw)
        return "leaked"

    script = [{"type": "start"},
              {"type": "notice", "kind": "tool_use", "tool": "Read"},
              {"type": "text_delta", "delta": "answer"},
              {"type": "done", "stop_reason": "end_turn"}]
    out = await run_tool_loop(StreamingBackend(script), "prompt",
                              [Tool(name="t", impl=_impl)])
    assert out == "answer", out
    assert calls == [], "a passive notice leaked into tool execution: {}".format(calls)


# --- bridge unit: never breaks the model call ----------------------------------------------

async def scenario_bridge_swallows_tracer_failure() -> None:
    # A broken sink/tracer must NOT kill the model call: the bridge swallows and
    # the caller (the stream loop) proceeds. Monitoring is optional, the run is not.
    bridge = make_live_bridge(RaisingTracer([LiveContributor()]), "c1", clock=FakeClock())
    for ev in SCRIPT:
        await bridge(ev)     # must not raise
    malformed = [{"no_type": True}, {"type": 7}, {"type": "text_delta", "delta": None}]
    for ev in malformed:
        await bridge(ev)     # must not raise either


# --- contributor ---------------------------------------------------------------------------

def contributor_projects_only_progress_spans() -> None:
    from yaah.trace.span import Span
    c = LiveContributor()
    prog = Span.timed("llm_progress", corr="c", t0=1.0, t1=1.0,
                      attrs={"event": "tool_call", "tool": "grep", "stage": "s1"})
    other = Span.timed("model_call", corr="c", t0=1.0, t1=2.0, attrs={"stage": "s1"})
    got = c.contribute(prog)
    assert got == {"event": "tool_call", "tool": "grep", "stage": "s1"}, got
    assert c.contribute(other) == {}, "live contributes nothing to non-progress spans"


def live_capture_is_registered_and_config_reachable() -> None:
    # `capture: ["live"]` must resolve at _build_tracer (BUILTIN_CONTRIBUTORS) —
    # else the flag raises at load and the feature is code-only, not config.
    from yaah.trace.contributors import BUILTIN_CONTRIBUTORS
    assert BUILTIN_CONTRIBUTORS.get("live") is LiveContributor, BUILTIN_CONTRIBUTORS


# --- agent integration: the actual wiring --------------------------------------------------

async def scenario_agent_emits_progress_when_live_captured() -> None:
    tracer = RecordingTracer([PhaseContributor(), LiveContributor()])
    agent = Agent(StreamingBackend(SCRIPT), "go", parse=False, tracer=tracer)
    out = await agent.invoke(Envelope("task", {}, {"correlation_id": "c9"}),
                             NodeConfig(model="fake:x"))
    assert out.payload["raw"] == "hello", out.payload      # reply unaffected
    got = _progress(tracer)
    assert [r["event"] for r in got] == ["start", "progress", "tool_call", "done"], got
    assert all(r["corr"] == "c9" for r in got), got
    # the model_call span still exists alongside — progress spans ADD, not replace
    assert any(r.get("name") == "model_call" for r in tracer.records), tracer.records
    # parent wiring on the REAL path: pulses parent onto the INPUT envelope's id,
    # exactly like the model_call span (so a trace waterfall nests them together)
    mc = next(s for s in tracer.spans if s.name == "model_call")
    pulses = [s for s in tracer.spans if s.name == "llm_progress"]
    assert pulses and all(s.parent == mc.parent for s in pulses), \
        [(s.name, s.parent) for s in tracer.spans]


async def scenario_agent_silent_without_live_capture() -> None:
    # phase-only tracer (today's default): NO llm_progress spans, behavior identical.
    tracer = RecordingTracer([PhaseContributor()])
    agent = Agent(StreamingBackend(SCRIPT), "go", parse=False, tracer=tracer)
    out = await agent.invoke(Envelope("task", {}, {"correlation_id": "c9"}),
                             NodeConfig(model="fake:x"))
    assert out.payload["raw"] == "hello", out.payload
    assert _progress(tracer) == [], _progress(tracer)


async def scenario_agent_default_tracer_still_works() -> None:
    # the optionality litmus: NO tracer at all (NullTracer default) → runs fine.
    agent = Agent(StreamingBackend(SCRIPT), "go", parse=False)
    out = await agent.invoke(Envelope("task", {}, {"correlation_id": "c9"}),
                             NodeConfig(model="fake:x"))
    assert out.payload["raw"] == "hello", out.payload


# --- console sink renders the pulses --------------------------------------------------------

async def scenario_console_sink_renders_progress_lines() -> None:
    # The monitoring UX is an operator watching stderr/console: the sink must render
    # llm_progress records as compact lines (stage spans stay in their existing format,
    # and unrelated span kinds stay silent).
    import io
    from yaah.adapters.trace import ConsoleTraceSink
    out = io.StringIO()
    sink = ConsoleTraceSink(stream=out)
    for rec in ({"name": "llm_progress", "stage": "verify", "event": "start"},
                {"name": "llm_progress", "stage": "verify", "event": "progress", "chars": 240},
                {"name": "llm_progress", "stage": "verify", "event": "tool_call", "tool": "read_file"},
                {"name": "llm_progress", "stage": "verify", "event": "done", "stop_reason": "end_turn"},
                {"name": "llm_progress", "stage": "verify", "event": "error", "message": "boom"},
                {"name": "model_call", "stage": "verify"}):          # non-live → silent
        await sink.handle(Envelope("trace", dict(rec)))
    lines = out.getvalue().strip().splitlines()
    assert len(lines) == 5, lines                       # 5 pulses, model_call silent
    assert all("verify" in ln for ln in lines), lines
    assert "240" in lines[1], lines[1]                  # heartbeat shows chars
    assert "read_file" in lines[2], lines[2]
    assert "end_turn" in lines[3], lines[3]
    assert "boom" in lines[4], lines[4]


# --- invocation-report immunity -------------------------------------------------------------

async def scenario_progress_spans_do_not_inflate_invocation_report() -> None:
    from yaah.trace.aggregate import count_by_stage_model
    tracer = RecordingTracer([PhaseContributor(), LiveContributor()])
    agent = Agent(StreamingBackend(SCRIPT), "go", parse=False, tracer=tracer)
    await agent.invoke(Envelope("task", {}, {"correlation_id": "c9"}),
                       NodeConfig(model="fake:x"))
    rep = count_by_stage_model(tracer.records)
    # exactly the ONE model_call is counted, none of the 4 llm_progress spans
    assert sum(row["calls"] for row in rep) == 1, rep


def main() -> None:
    contributor_projects_only_progress_spans()
    live_capture_is_registered_and_config_reachable()
    for scen in (scenario_bridge_maps_semantic_events,
                 scenario_bridge_error_event_maps,
                 scenario_heartbeat_throttles_deltas,
                 scenario_bridge_swallows_tracer_failure,
                 scenario_bridge_maps_notice_to_tool_pulse,
                 scenario_notice_is_inert_to_the_tool_loop,
                 scenario_agent_emits_progress_when_live_captured,
                 scenario_agent_silent_without_live_capture,
                 scenario_agent_default_tracer_still_works,
                 scenario_console_sink_renders_progress_lines,
                 scenario_progress_spans_do_not_inflate_invocation_report):
        asyncio.run(scen())
    print("ok")


if __name__ == "__main__":
    main()
