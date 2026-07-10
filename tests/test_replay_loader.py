"""test_replay_loader — the counterfactual-replay loader (yaah.replay).

Falsify-first coverage for turning a stored run's completion into a
`fake_scripted` script and running a CHANGED config against it, zero model calls:

- refusal fires (loud, named) for the structurally-invalid classes: prompt-edit
  risk not asserted, `{{!key}}` untrusted fencing, and any shape that would make
  more model calls than the single stored completion can answer (>1 agent,
  agent_loop, escalate_model, fork/fanout/foreach);
- the provider swap keys the stored completion under the model the changed
  pipeline will ask for, drops live_config, applies the input override;
- END-TO-END: run a real one-shot campaign through the row store, then replay the
  stored completion against a CHANGED config and assert the replay both REPRODUCES
  the downstream routing and DIVERGES where a knob change (a tightened schema)
  should break it.

Run: cd yaah && PYTHONPATH=src python3 tests/test_replay_loader.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah.replay import (
    ReplayRefused,
    build_replay_root,
    completion_from_row,
    replay_over_rows,
)


# ---- fixtures -------------------------------------------------------------

_RAW = '{"verdict": "SKIP", "score": 3}'


def _single_agent_pipeline(*, template="Judge {{text}}. JSON: {\"verdict\": \"...\"}",
                           schema=None, extra_nodes=None, extra_stages=None,
                           model="fake:j"):
    node = {"type": "agent", "model": model, "stage": "judge", "template": template,
            "parse": True}
    if schema is not None:
        node["output_schema"] = schema
    nodes = {"judge": node}
    stages = {"judge": {"node": "judge", "then": None}}
    if extra_nodes:
        nodes.update(extra_nodes)
    if extra_stages:
        stages = extra_stages
    return {"nodes": nodes, "graph": {"start": "judge", "stages": stages}}


def _root(pipeline, *, input_val=None, live=False):
    root = {
        "transport": {"type": "inproc"},
        "providers": {"fake": {"type": "fake_scripted",
                               "by_model": {"j": [_RAW]}}},
        "default_provider": "fake",
        "state": {"type": "memory"},
        "pipeline": pipeline,
        "input": input_val if input_val is not None else {"text": "hello"},
        "run": True,
    }
    if live:
        root["live_config"] = True
    return root


# ---- refusals (loud, named) ----------------------------------------------

def refuses_without_prompt_assertion() -> None:
    root = _root(_single_agent_pipeline())
    try:
        build_replay_root(root, ".", completion=_RAW, assume_prompts_unchanged=False)
    except ReplayRefused as e:
        assert "prompt" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused when prompts-unchanged not asserted")


def refuses_untrusted_fence() -> None:
    root = _root(_single_agent_pipeline(
        template="Judge {{text}} against {{!secret}}. JSON: {}"))
    try:
        build_replay_root(root, ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "fenc" in str(e).lower() or "{{!" in str(e), e
        return
    raise AssertionError("expected ReplayRefused for a {{!key}} template")


def refuses_two_model_calls() -> None:
    pipe = _single_agent_pipeline(
        extra_nodes={"j2": {"type": "agent", "model": "fake:j", "stage": "j2",
                            "template": "again", "parse": True}},
        extra_stages={"judge": {"node": "judge", "then": "j2"},
                      "j2": {"node": "j2", "then": None}})
    try:
        build_replay_root(_root(pipe), ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "model call" in str(e).lower() or "single" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused for >1 model-calling node")


def refuses_one_node_reused_by_two_stages() -> None:
    # ONE agent node, but wired into TWO stages -> TWO model calls; the 2nd would
    # draw the ScriptedProvider's silent "" default. Counting node DEFINITIONS
    # would miss this; counting model-calling STAGES catches it.
    pipe = _single_agent_pipeline(extra_stages={
        "judge": {"node": "judge", "then": "again"},
        "again": {"node": "judge", "then": None}})
    try:
        build_replay_root(_root(pipe), ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "stages call a model" in str(e) or "single" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused: one node reused by two stages = 2 calls")


def refuses_loop_back_edge() -> None:
    # a branch routes back into the model stage -> the back-edge re-invokes it.
    pipe = _single_agent_pipeline(
        schema={"required": ["verdict"], "properties": {"verdict": {"type": "string"}}},
        extra_stages={
            "judge": {"node": "judge",
                      "branch": {"on": "verdict",
                                 "routes": {"AGAIN": "judge"}, "default": "end"}},
            "end": {"node": "judge", "then": None}})
    # NOTE: 'end' also references judge to keep the fixture minimal; the loop check
    # is what we assert, so use a non-model terminal to isolate it:
    pipe = _single_agent_pipeline(
        extra_nodes={"stop": {"type": "transform", "target": "fn:x:y", "call": "envelope"}},
        extra_stages={
            "judge": {"node": "judge",
                      "branch": {"on": "verdict",
                                 "routes": {"AGAIN": "judge"}, "default": "end"}},
            "end": {"node": "stop", "then": None}})
    try:
        build_replay_root(_root(pipe), ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "cycle" in str(e).lower() or "back-edge" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused for a loop routing back into the model stage")


def refuses_agent_loop() -> None:
    pipe = _single_agent_pipeline()
    pipe["nodes"]["judge"]["type"] = "agent_loop"
    try:
        build_replay_root(_root(pipe), ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "agent_loop" in str(e) or "loop" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused for an agent_loop node")


def refuses_escalate_model() -> None:
    pipe = _single_agent_pipeline()
    pipe["nodes"]["judge"]["escalate_model"] = "fake:big"
    try:
        build_replay_root(_root(pipe), ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "escalate" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused for escalate_model")


def refuses_fanout_shape() -> None:
    pipe = _single_agent_pipeline(
        extra_nodes={"t": {"type": "transform", "target": "fn:x:y", "call": "envelope"}},
        extra_stages={"judge": {"node": "judge", "fork": ["a"], "then": None},
                      "a": {"node": "t", "then": None}})
    try:
        build_replay_root(_root(pipe), ".", completion=_RAW, assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "parallel" in str(e).lower() or "fork" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused for a fork/fanout/foreach shape")


# ---- the provider swap ----------------------------------------------------

def swaps_providers_and_keys_by_model() -> None:
    root = _root(_single_agent_pipeline(model="fake:j"), live=True)
    rr = build_replay_root(root, ".", completion="CANNED",
                           assume_prompts_unchanged=True,
                           input_override={"text": "world"})
    # every provider is now fake_scripted carrying the injected completion, keyed
    # by the leaf model name (post 'provider:' prefix) the pipeline will ask for.
    for spec in rr["providers"].values():
        assert spec["type"] == "fake_scripted", rr["providers"]
        assert spec["by_model"] == {"j": ["CANNED"]}, spec
    assert "live_config" not in rr, "live_config must be dropped (fingerprint honesty)"
    assert rr["input"] == {"text": "world"}, rr["input"]
    assert rr["run"] is True
    # the original root must be untouched (pure build)
    assert root["providers"]["fake"]["by_model"] == {"j": [_RAW]}


def keys_bare_model_without_prefix() -> None:
    root = _root(_single_agent_pipeline(model="j"))
    root["providers"] = {"fake": {"type": "fake_scripted", "by_model": {"j": [_RAW]}}}
    rr = build_replay_root(root, ".", completion="X", assume_prompts_unchanged=True)
    for spec in rr["providers"].values():
        assert spec["by_model"] == {"j": ["X"]}, spec


def refuses_completion_in_template() -> None:
    # eval #2: if the stored completion is a substring of the model template, the
    # ScriptedProvider content-cursor would skip it -> silent "" default.
    root = _root(_single_agent_pipeline(template="Classify {{text}}. Reply SKIP or FIX."))
    try:
        build_replay_root(root, ".", completion="SKIP", assume_prompts_unchanged=True)
    except ReplayRefused as e:
        assert "content-cursor" in str(e).lower() or "verbatim" in str(e).lower(), e
        return
    raise AssertionError("expected ReplayRefused when completion is in the template")


def refuses_multi_agent_source() -> None:
    # eval #1: the SOURCE campaign was multi-agent, so a row's terminal output.raw
    # is the LAST agent's completion -> wrong provenance for the replayed node.
    with tempfile.TemporaryDirectory() as tmp:
        two_agent = {
            "nodes": {"a": {"type": "agent", "model": "fake:a", "stage": "a",
                            "template": "one", "parse": True},
                      "b": {"type": "agent", "model": "fake:b", "stage": "b",
                            "template": "two", "parse": True}},
            "graph": {"start": "a", "stages": {"a": {"node": "a", "then": "b"},
                                               "b": {"node": "b", "then": None}}}}
        _write(tmp, "src.local.json", _root(two_agent))
        _write(tmp, "changed.local.json", _root(_single_agent_pipeline()))
        exp = {"id": "prov", "variants": {"base": "src.local.json"},
               "inputs": [{"text": "x"}], "repetitions": 1,
               "price_map": {"fake:a": {"input": 0, "output": 0},
                             "fake:b": {"input": 0, "output": 0}},
               "store": {"dir": "."}}
        try:
            asyncio.run(replay_over_rows(exp, tmp, os.path.join(tmp, "changed.local.json"),
                                         assume_prompts_unchanged=True))
        except ReplayRefused as e:
            assert "provenance" in str(e).lower() or "source campaign" in str(e).lower(), e
            return
        raise AssertionError("expected ReplayRefused for a multi-agent source campaign")


def completion_from_row_reads_raw() -> None:
    assert completion_from_row({"output": {"raw": _RAW, "verdict": "SKIP"}}) == _RAW
    assert completion_from_row({"output": None}) is None
    assert completion_from_row({"output": {"verdict": "SKIP"}}) is None
    assert completion_from_row({"output": {"text": _RAW}}, raw_key="text") == _RAW


# ---- END-TO-END: real campaign -> stored row -> replay changed config ------

def _write(dirpath, name, obj):
    p = os.path.join(dirpath, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


async def _end_to_end(tmp) -> None:
    from yaah.adapters.experiment_stores import JsonlExperimentStore
    from yaah.experiment import run_experiment

    # ORIGINAL: single agent emits a known JSON completion; run one real campaign
    # row through the store (the only place a completion is persisted).
    orig_pipe = _single_agent_pipeline()
    orig_root = _root(orig_pipe)
    _write(tmp, "orig.local.json", orig_root)
    exp = {"id": "e2e", "variants": {"base": "orig.local.json"},
           "inputs": [{"text": "hi"}], "repetitions": 1,
           "price_map": {"fake:j": {"input": 0, "output": 0}},
           "store": {"dir": "."}}
    store = JsonlExperimentStore(tmp)
    await run_experiment(exp, tmp, store=store)
    rows = await store.rows("e2e")
    assert len(rows) == 1 and rows[0]["outcome"] == "done", rows
    assert completion_from_row(rows[0]) == _RAW, rows[0]["output"]

    # CHANGED (survives): add a branch AFTER the recorded agent that routes on the
    # parsed verdict. Replay must reproduce the SKIP lane from the stored raw —
    # downstream topology the original run never had.
    with open(os.path.join(tmp, "transforms.py"), "w", encoding="utf-8") as f:
        f.write("def skip_lane(env, config):\n    return {'routed': 'skip_lane'}\n"
                "def fix_lane(env, config):\n    return {'routed': 'fix_lane'}\n")
    routed_pipe = _single_agent_pipeline(
        schema={"required": ["verdict"], "properties": {"verdict": {"type": "string"}}},
        extra_nodes={
            "skip_lane": {"type": "transform", "target": "fn:transforms:skip_lane",
                          "call": "envelope"},
            "fix_lane": {"type": "transform", "target": "fn:transforms:fix_lane",
                         "call": "envelope"}},
        extra_stages={
            "judge": {"node": "judge",
                      "branch": {"on": "verdict",
                                 "routes": {"SKIP": "skip", "FIX": "fix"},
                                 "default": "skip"}},
            "skip": {"node": "skip_lane", "then": None},
            "fix": {"node": "fix_lane", "then": None}})
    changed_root = _root(routed_pipe)
    changed_path = _write(tmp, "changed.local.json", changed_root)
    summary = await replay_over_rows(exp, tmp, changed_path,
                                     assume_prompts_unchanged=True, store=store)
    assert summary["replayed"] == 1, summary
    out = summary["results"][0]
    assert out["done"], summary
    # the injected completion's parsed verdict drove the NEW downstream branch
    assert out["payload"].get("routed") == "skip_lane", out["payload"]

    # REPRODUCE (byte-for-byte): replay the UNCHANGED original config; the terminal
    # payload must carry the injected completion verbatim + its parsed keys.
    repro = await replay_over_rows(exp, tmp, os.path.join(tmp, "orig.local.json"),
                                   assume_prompts_unchanged=True, store=store)
    rout = repro["results"][0]
    assert rout["done"] and rout["payload"].get("raw") == _RAW, rout["payload"]
    assert rout["payload"].get("verdict") == "SKIP", rout["payload"]

    # CHANGED (diverges): tighten the schema to REQUIRE a key the stored completion
    # lacks. The injected raw now fails validation on every attempt — the replay
    # must NOT quietly reach the skip lane; it diverges (fails/parks).
    tightened = _single_agent_pipeline(
        schema={"required": ["approved"],
                "properties": {"approved": {"type": "boolean"}}})
    diverge_root = _root(tightened)
    diverge_path = _write(tmp, "diverge.local.json", diverge_root)
    summary2 = await replay_over_rows(exp, tmp, diverge_path,
                                      assume_prompts_unchanged=True, store=store)
    out2 = summary2["results"][0]
    assert not out2["done"] or out2["payload"].get("routed") != "skip_lane", (
        "a tightened schema the stored completion fails should DIVERGE, not "
        "silently reproduce the original: {}".format(summary2))


def end_to_end_replay() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_end_to_end(tmp))


def main() -> None:
    refuses_without_prompt_assertion()
    refuses_untrusted_fence()
    refuses_two_model_calls()
    refuses_one_node_reused_by_two_stages()
    refuses_loop_back_edge()
    refuses_agent_loop()
    refuses_escalate_model()
    refuses_fanout_shape()
    refuses_completion_in_template()
    refuses_multi_agent_source()
    swaps_providers_and_keys_by_model()
    keys_bare_model_without_prefix()
    completion_from_row_reads_raw()
    end_to_end_replay()
    print("ok")


if __name__ == "__main__":
    main()
