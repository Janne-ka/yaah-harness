"""test_replay_recording — per-call completion RECORDING -> multi-call replay.

Falsify-first coverage for closing v1 replay's single-model-call refusal by
RECORDING every model call during a live run (RecordingProvider) and scripting
them ALL back through a changed config (yaah.replay --recording):

- the recorder captures all THREE provider shapes (stream / turn / complete) in
  call order, keyed by the bare (post-prefix) model name;
- OFF by default: no `record_to` -> no file written;
- a 3-stage LINEAR pipeline records 3 completions and replays byte-for-byte
  through the changed config; a knob change (tightened schema) DIVERGES;
- escalate_model's two calls (primary + escalated rung) both record and replay;
- concurrent topology (fork/fanout/foreach), agent_loop and cyclic model stages
  stay REFUSED with an honest message (per-model FIFO is not deterministic under
  concurrent scheduling; a text-only script can't reproduce a tool loop);
- a missing / corrupt recording fails LOUD.

Run: cd yaah && PYTHONPATH=src python3 tests/test_replay_recording.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah.agents import api_provider as _ap
from yaah.agents.fake_provider import FakeProvider
from yaah.agents.recording_provider import RecordingProvider
from yaah.replay import (
    ReplayRefused,
    build_replay_root_from_recording,
    by_model_from_recording,
    load_recording,
    replay_recording,
)


# ---- helpers --------------------------------------------------------------

def _read_jsonl(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class _TurnOnly:
    """A collected-only backend with turn() and NO native stream() — an external
    legacy backend. stream_of adapts it; the recorder must still capture it."""
    def __init__(self, text):
        self._text = text

    async def turn(self, messages, tools, *, model=None, **opts):
        return {"text": self._text}


class _CompleteOnly:
    """A collected-only backend with only complete() — the oldest shape."""
    def __init__(self, text):
        self._text = text

    async def complete(self, prompt, *, model=None, **opts):
        return self._text


# ---- the recorder captures all three provider shapes ----------------------

def records_stream_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.jsonl")
        rp = RecordingProvider(FakeProvider(responses=["HELLO"]), path)
        out = asyncio.run(_ap.complete(rp, "q", model="m"))
        assert out == "HELLO", out
        recs = _read_jsonl(path)
        assert recs == [{"order": recs[0]["order"], "model": "m", "completion": "HELLO"}], recs


def records_turn_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.jsonl")
        rp = RecordingProvider(_TurnOnly("VIA_TURN"), path)
        # complete() drives stream_of, which adapts a turn-only leaf into a stream;
        # the recorder tees the resulting text_delta.
        out = asyncio.run(_ap.complete(rp, "q", model="tm"))
        assert out == "VIA_TURN", out
        recs = _read_jsonl(path)
        assert len(recs) == 1 and recs[0]["completion"] == "VIA_TURN", recs
        assert recs[0]["model"] == "tm", recs


def records_complete_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.jsonl")
        rp = RecordingProvider(_CompleteOnly("VIA_COMPLETE"), path)
        out = asyncio.run(_ap.complete(rp, "q", model="cm"))
        assert out == "VIA_COMPLETE", out
        recs = _read_jsonl(path)
        assert len(recs) == 1 and recs[0]["completion"] == "VIA_COMPLETE", recs


def records_call_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rec.jsonl")
        rp = RecordingProvider(FakeProvider(responses=["A", "B", "C"]), path)

        async def _three():
            for _ in range(3):
                await _ap.complete(rp, "q", model="m")
        asyncio.run(_three())
        recs = _read_jsonl(path)
        assert [r["completion"] for r in recs] == ["A", "B", "C"], recs
        orders = [r["order"] for r in recs]
        assert orders == sorted(orders) and len(set(orders)) == 3, orders
        # per-model grouping preserves call order -> ScriptedProvider's by_model shape
        assert by_model_from_recording(recs) == {"m": ["A", "B", "C"]}, recs


def records_two_models_grouped() -> None:
    recs = [
        {"order": 0, "model": "a", "completion": "a0"},
        {"order": 1, "model": "b", "completion": "b0"},
        {"order": 2, "model": "a", "completion": "a1"},
    ]
    assert by_model_from_recording(recs) == {"a": ["a0", "a1"], "b": ["b0"]}, recs


# ---- OFF by default (opt-in only) -----------------------------------------

def off_by_default() -> None:
    from yaah.runtime_factories import _build_provider
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = os.path.join(tmp, "should-not-exist.jsonl")
        cfg = {"providers": {"fake": {"type": "fake_scripted",
                                      "by_model": {"m": ["X"]}}},
               "default_provider": "fake"}
        prov = _build_provider(cfg, tmp)
        out = asyncio.run(_ap.complete(prov, "q", model="fake:m"))
        assert out == "X", out
        assert not os.path.exists(sentinel), "no record_to must write NO file"
        # nothing in the temp dir either
        assert os.listdir(tmp) == [], os.listdir(tmp)


def records_when_opted_in() -> None:
    from yaah.runtime_factories import _build_provider
    with tempfile.TemporaryDirectory() as tmp:
        rec = os.path.join(tmp, "rec.jsonl")
        cfg = {"providers": {"fake": {"type": "fake_scripted",
                                      "by_model": {"m": ["X"]},
                                      "record_to": "rec.jsonl"}},
               "default_provider": "fake"}
        prov = _build_provider(cfg, tmp)
        out = asyncio.run(_ap.complete(prov, "q", model="fake:m"))
        assert out == "X", out
        recs = _read_jsonl(rec)
        # recorded under the BARE (post-prefix) model name, as ScriptedProvider keys
        assert recs == [{"order": recs[0]["order"], "model": "m", "completion": "X"}], recs


# ---- missing / corrupt recording -> loud ----------------------------------

def missing_recording_loud() -> None:
    try:
        load_recording("/no/such/recording.jsonl")
    except (OSError, ValueError) as e:
        assert "recording" in str(e).lower() or "no such" in str(e).lower(), e
        return
    raise AssertionError("expected a loud error for a missing recording file")


def corrupt_recording_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"order": 0, "model": "m", "completion": "ok"}\n')
            f.write("this is not json\n")
        try:
            load_recording(path)
        except ValueError as e:
            assert "recording" in str(e).lower() or "line" in str(e).lower(), e
            return
        raise AssertionError("expected a loud ValueError for a corrupt recording line")


def recording_missing_fields_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"order": 0, "completion": "ok"}\n')  # no model
        try:
            load_recording(path)
        except ValueError as e:
            assert "model" in str(e).lower() or "recording" in str(e).lower(), e
            return
        raise AssertionError("expected a loud ValueError for a record missing 'model'")


# ---- refusals kept: concurrent, agent_loop, cycle -------------------------

def _linear(nodes_stages, model="fake:j"):
    return {"nodes": nodes_stages[0], "graph": {"start": nodes_stages[1],
            "stages": nodes_stages[2]}}


def _agent(model, template="Do {{text}}. JSON: {\"v\":\"x\"}", **extra):
    n = {"type": "agent", "model": model, "stage": "s", "template": template, "parse": True}
    n.update(extra)
    return n


def _root(pipeline, input_val=None):
    return {"transport": {"type": "inproc"},
            "providers": {"fake": {"type": "fake_scripted", "by_model": {"j": ["{}"]}}},
            "default_provider": "fake",
            "state": {"type": "memory"},
            "pipeline": pipeline,
            "input": input_val if input_val is not None else {"text": "hi"},
            "run": True}


def _by_model_one():
    return {"j": ['{"v": "x"}']}


def refuses_fanout_with_recording() -> None:
    pipe = {"nodes": {"j": _agent("fake:j"),
                      "t": {"type": "transform", "target": "fn:x:y", "call": "envelope"}},
            "graph": {"start": "j", "stages": {
                "j": {"node": "j", "fork": ["a"], "then": None},
                "a": {"node": "t", "then": None}}}}
    try:
        build_replay_root_from_recording(_root(pipe), ".", by_model=_by_model_one(),
                                         assume_prompts_unchanged=True)
    except ReplayRefused as e:
        s = str(e).lower()
        assert ("concurrent" in s or "parallel" in s or "fork" in s), e
        return
    raise AssertionError("expected ReplayRefused: concurrent topology stays refused")


def refuses_agent_loop_with_recording() -> None:
    pipe = {"nodes": {"j": _agent("fake:j")},
            "graph": {"start": "j", "stages": {"j": {"node": "j", "then": None}}}}
    pipe["nodes"]["j"]["type"] = "agent_loop"
    try:
        build_replay_root_from_recording(_root(pipe), ".", by_model=_by_model_one(),
                                         assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "loop" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused: agent_loop stays refused (tool structure)")


def refuses_cycle_with_recording() -> None:
    pipe = {"nodes": {"j": _agent("fake:j"),
                      "stop": {"type": "transform", "target": "fn:x:y", "call": "envelope"}},
            "graph": {"start": "j", "stages": {
                "j": {"node": "j", "branch": {"on": "v",
                      "routes": {"AGAIN": "j"}, "default": "end"}},
                "end": {"node": "stop", "then": None}}}}
    try:
        build_replay_root_from_recording(_root(pipe), ".", by_model=_by_model_one(),
                                         assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "cycle" in str(e).lower() or "back-edge" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused: cyclic model stage stays refused")


def refuses_tool_using_agent_with_recording() -> None:
    # eval finding #1: a PLAIN agent with tools/expose/broker drives the engine
    # tool loop on a turn-capable live backend (one recorded completion PER TURN);
    # the replay backend has no turn(), so it would draw only the first turn's
    # text and report green. Must be refused like agent_loop.
    for extra in ({"tools": [{"name": "t", "usage": "x"}]},
                  {"expose": ["text"]},
                  {"broker": {"node": "b"}}):
        pipe = {"nodes": {"j": _agent("fake:j", **extra)},
                "graph": {"start": "j", "stages": {"j": {"node": "j", "then": None}}}}
        try:
            build_replay_root_from_recording(_root(pipe), ".", by_model=_by_model_one(),
                                             assume_prompts_unchanged=True)
        except ReplayRefused as e:
            assert "tool" in str(e).lower(), e
            continue
        raise AssertionError(
            "expected ReplayRefused for a tool-using plain agent ({})".format(extra))


def refuses_model_absent_from_recording() -> None:
    # a stage whose model has NO recorded entries would draw ScriptedProvider's
    # SILENT default (on_exhaustion can't catch an unknown model) — refuse up front.
    pipe = {"nodes": {"j": _agent("fake:other")},
            "graph": {"start": "j", "stages": {"j": {"node": "j", "then": None}}}}
    try:
        build_replay_root_from_recording(_root(pipe), ".", by_model=_by_model_one(),
                                         assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "no entries" in str(e).lower() or "recording" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused for a model absent from the recording")


def escalate_uncovered_refused_only_when_help_fires() -> None:
    # eval finding #2: an escalate_model whose model is absent from the recording
    # is refused iff the rung WOULD fire — i.e. a recorded primary completion
    # carries truthy `help`. No help anywhere -> the rung provably never fires
    # under the prompts-unchanged assertion, so the replay may proceed.
    pipe_of = lambda: {"nodes": {"j": _agent("fake:j", escalate_model="fake:big")},
                       "graph": {"start": "j",
                                 "stages": {"j": {"node": "j", "then": None}}}}
    # (a) primary completion HAS help, 'big' unrecorded -> refused loud
    try:
        build_replay_root_from_recording(
            _root(pipe_of()), ".", by_model={"j": ['{"help": true}']},
            assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "escalate" in str(e).lower() or "help" in str(e).lower(), e
    else:
        raise AssertionError("expected ReplayRefused: help fires but rung unrecorded")
    # (b) NO help in any primary completion, 'big' unrecorded -> allowed
    rr = build_replay_root_from_recording(
        _root(pipe_of()), ".", by_model={"j": ['{"v": "x"}']},
        assume_prompts_unchanged=True)
    assert rr["providers"], rr
    # (c) help fires AND 'big' IS recorded -> allowed (both rungs scripted)
    rr = build_replay_root_from_recording(
        _root(pipe_of()), ".",
        by_model={"j": ['{"help": true}'], "big": ['{"v": "y"}']},
        assume_prompts_unchanged=True)
    assert rr["providers"], rr


def refuses_prompt_edit_without_assertion() -> None:
    pipe = {"nodes": {"j": _agent("fake:j")},
            "graph": {"start": "j", "stages": {"j": {"node": "j", "then": None}}}}
    try:
        build_replay_root_from_recording(_root(pipe), ".", by_model=_by_model_one(),
                                         assume_prompts_unchanged=False)
    except ReplayRefused as e:
        assert "prompt" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused when prompts-unchanged not asserted")


# ---- LIFTED: linear multi-stage records 3 and replays byte-for-byte -------

def _write(dirpath, name, obj):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


def _three_stage_pipeline(schema_c=None):
    # a -> b -> c, three DISTINCT models, c parses JSON and (optionally) validates
    a = {"type": "agent", "model": "fake:a", "stage": "a",
         "template": "Stage A on {{text}}", "parse": True}
    b = {"type": "agent", "model": "fake:b", "stage": "b",
         "template": "Stage B", "parse": True}
    c = {"type": "agent", "model": "fake:c", "stage": "c",
         "template": "Stage C. JSON.", "parse": True}
    if schema_c is not None:
        c["output_schema"] = schema_c
    return {"nodes": {"a": a, "b": b, "c": c},
            "graph": {"start": "a", "stages": {
                "a": {"node": "a", "then": "b"},
                "b": {"node": "b", "then": "c"},
                "c": {"node": "c", "then": None}}}}


async def _e2e_linear(tmp) -> None:
    from yaah.runtime import run_root
    from yaah.harness import Done

    rec = os.path.join(tmp, "rec.jsonl")
    # LIVE run: three agents, each a distinct model with a distinct scripted answer;
    # every provider records to the same JSONL.
    live_root = {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake_scripted",
                            "by_model": {"a": ['{"a": 1}'], "b": ['{"b": 2}'],
                                         "c": ['{"verdict": "SKIP"}']},
                            "record_to": "rec.jsonl"}},
        "default_provider": "fake",
        "state": {"type": "memory"},
        "pipeline": _three_stage_pipeline(),
        "input": {"text": "hi"},
        "run": True,
    }
    _write(tmp, "live.local.json", live_root)
    out = await run_root(live_root, tmp)
    assert isinstance(out, Done), out
    recs = _read_jsonl(rec)
    assert len(recs) == 3, recs
    assert by_model_from_recording(recs) == {
        "a": ['{"a": 1}'], "b": ['{"b": 2}'], "c": ['{"verdict": "SKIP"}']}, recs

    # REPLAY the SAME (unchanged) pipeline against the recording: zero model calls,
    # byte-for-byte terminal completion reproduced.
    changed_path = _write(tmp, "changed.local.json", _three_stage_pipeline_root(tmp))
    summary = await replay_recording(changed_path, rec, assume_prompts_unchanged=True)
    assert summary["done"], summary
    assert summary["payload"].get("raw") == '{"verdict": "SKIP"}', summary
    assert summary["payload"].get("verdict") == "SKIP", summary


def _three_stage_pipeline_root(tmp):
    return {"transport": {"type": "inproc"},
            "providers": {"fake": {"type": "fake", "responses": []}},
            "default_provider": "fake",
            "state": {"type": "memory"},
            "pipeline": _three_stage_pipeline(),
            "input": {"text": "hi"},
            "run": True}


def end_to_end_linear() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_e2e_linear(tmp))


async def _e2e_diverge(tmp) -> None:
    from yaah.runtime import run_root
    from yaah.harness import Done

    rec = os.path.join(tmp, "rec.jsonl")
    live_root = {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake_scripted",
                            "by_model": {"a": ['{"a": 1}'], "b": ['{"b": 2}'],
                                         "c": ['{"verdict": "SKIP"}']},
                            "record_to": "rec.jsonl"}},
        "default_provider": "fake",
        "state": {"type": "memory"},
        "pipeline": _three_stage_pipeline(),
        "input": {"text": "hi"},
        "run": True,
    }
    out = await run_root(live_root, tmp)
    assert isinstance(out, Done), out

    # CHANGED (diverges): tighten stage C's schema to REQUIRE a key the stored
    # completion lacks. The recorded raw fails validation on every attempt; the
    # replay must NOT quietly succeed.
    tightened = _three_stage_pipeline_root(tmp)
    tightened["pipeline"] = _three_stage_pipeline(
        schema_c={"required": ["approved"],
                  "properties": {"approved": {"type": "boolean"}}})
    tightened_path = _write(tmp, "tightened.local.json", tightened)
    summary = await replay_recording(tightened_path, rec, assume_prompts_unchanged=True)
    assert not summary["done"], (
        "a tightened schema the stored completion fails must DIVERGE: {}".format(summary))


def end_to_end_diverge() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_e2e_diverge(tmp))


# ---- LIFTED: escalate_model records + replays both calls ------------------

async def _e2e_escalate(tmp) -> None:
    from yaah.runtime import run_root
    from yaah.harness import Done

    rec = os.path.join(tmp, "rec.jsonl")
    # primary model 'a' emits help -> escalate to model 'big'; both recorded.
    pipe = {"nodes": {"j": {"type": "agent", "model": "fake:a", "stage": "j",
                            "template": "Judge {{text}}. JSON.",
                            "parse": True, "escalate_model": "fake:big"}},
            "graph": {"start": "j", "stages": {"j": {"node": "j", "then": None}}}}
    live_root = {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake_scripted",
                            "by_model": {"a": ['{"help": true}'],
                                         "big": ['{"verdict": "RESOLVED"}']},
                            "record_to": "rec.jsonl"}},
        "default_provider": "fake",
        "state": {"type": "memory"},
        "pipeline": pipe,
        "input": {"text": "hi"},
        "run": True,
    }
    out = await run_root(live_root, tmp)
    assert isinstance(out, Done), out
    recs = _read_jsonl(rec)
    # both the primary (a) and escalated (big) calls recorded, in order
    assert [r["model"] for r in recs] == ["a", "big"], recs

    # REPLAY the escalate-wired pipeline against the recording: both calls scripted.
    changed = {"transport": {"type": "inproc"},
               "providers": {"fake": {"type": "fake", "responses": []}},
               "default_provider": "fake",
               "state": {"type": "memory"},
               "pipeline": pipe,
               "input": {"text": "hi"},
               "run": True}
    changed_path = _write(tmp, "esc.local.json", changed)
    summary = await replay_recording(changed_path, rec, assume_prompts_unchanged=True)
    assert summary["done"], summary
    assert summary["payload"].get("verdict") == "RESOLVED", summary


def end_to_end_escalate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_e2e_escalate(tmp))


def main() -> None:
    records_stream_shape()
    records_turn_shape()
    records_complete_shape()
    records_call_order()
    records_two_models_grouped()
    off_by_default()
    records_when_opted_in()
    missing_recording_loud()
    corrupt_recording_loud()
    recording_missing_fields_loud()
    refuses_fanout_with_recording()
    refuses_agent_loop_with_recording()
    refuses_cycle_with_recording()
    refuses_tool_using_agent_with_recording()
    refuses_model_absent_from_recording()
    escalate_uncovered_refused_only_when_help_fires()
    refuses_prompt_edit_without_assertion()
    end_to_end_linear()
    end_to_end_diverge()
    end_to_end_escalate()
    print("ok")


if __name__ == "__main__":
    main()
