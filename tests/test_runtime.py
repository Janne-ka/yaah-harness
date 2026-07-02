"""Runtime helpers: serve-placement resolution and the gate-driver decider.

Covers the pure config->runtime helpers added for distribution (serve by
placement) and gates (the decider that backs drive()), without spinning a
transport.

Run: cd yaah && PYTHONPATH=src python3 tests/test_runtime.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile

from yaah import Kind
from yaah import runtime as r

PIPELINE = {
    "nodes": {
        "role:spec":   {"type": "agent", "placement": "local"},
        "role:review": {"type": "agent", "placement": "cloud"},
        "role:eval":   {"type": "agent", "placement": "cloud"},
        "role:flex":   {"type": "agent", "placement": "either"},
        "role:plain":  {"type": "transform"},  # non-agent node, no placement tag
    }
}


def scenario_resolve_serve() -> None:
    assert r._resolve_serve("all", PIPELINE) is None  # serve everything

    assert r._resolve_serve(["role:spec", "role:eval"], PIPELINE) == {"role:spec", "role:eval"}

    # a bare role STRING is one role, not a char iterable ("role:eval" != {'r','o',...})
    assert r._resolve_serve("role:eval", PIPELINE) == {"role:eval"}

    # by single placement tag
    assert r._resolve_serve({"placement": "cloud"}, PIPELINE) == {"role:review", "role:eval"}
    assert r._resolve_serve({"placement": "local"}, PIPELINE) == {"role:spec"}

    # by a list of placements (union); "either" is matched literally
    assert r._resolve_serve({"placement": ["local", "either"]}, PIPELINE) == {"role:spec", "role:flex"}

    # an untagged node is never picked by a placement selector
    assert "role:plain" not in r._resolve_serve({"placement": ["cloud", "local", "either"]}, PIPELINE)

    # a placement that matches nothing fails fast (typo guard)
    try:
        r._resolve_serve({"placement": "gpu"}, PIPELINE)
        raise AssertionError("expected ValueError for an unmatched placement")
    except ValueError as e:
        assert "matched no nodes" in str(e), e


def scenario_validate_root() -> None:
    # known keys + an "_about" comment pass (use a real typed-block for transport
    # since the shape validator now also checks `{"type": ...}` shape)
    r._validate_root({"pipeline": "p.json", "transport": {"type": "inproc"},
                      "serve": "all", "_about": "doc"})
    # an unknown / misspelled top-level key fails fast with a "did you mean" hint
    try:
        r._validate_root({"default_provder": "x", "pipeline": "p.json"})
        raise AssertionError("expected ValueError for an unknown top-level key")
    except ValueError as e:
        assert "default_provder" in str(e) and "did you mean" in str(e), e
    # the bare-string trap on a typed-block key gets a JSON-shaped rewrite suggestion
    try:
        r._validate_root({"transport": "inproc", "pipeline": "p.json"})
        raise AssertionError("expected ValueError for a bare-string transport")
    except ValueError as e:
        assert "transport" in str(e) and '"type": "inproc"' in str(e), e


def scenario_decider() -> None:
    # nothing configured -> None (run-once default, no auto-drive)
    assert r._build_decider({}) is None

    decide = r._build_decider({"decisions": {"data-audit": {"approved": True}}})
    assert decide is not None

    class S:  # stand-in Suspended
        def __init__(self, awaiting):
            self.awaiting = awaiting
            self.concerns = []

    # matched whole, after-':' and before-':'
    for tag in ("data-audit", "human:data-audit"):
        env = asyncio.run(decide(S(tag)))
        assert env.kind == Kind.RESUME and env.payload == {"approved": True}, (tag, env)

    # no match, not interactive -> clear RuntimeError (not a silent stop)
    try:
        asyncio.run(decide(S("spec-review")))
        raise AssertionError("expected RuntimeError for an unmatched gate")
    except RuntimeError as e:
        assert "no matching decision" in str(e), e


def scenario_trace_file_sink() -> None:
    # run a one-stage pipeline in-proc with the file trace sink; assert JSONL has
    # the stage + model_call records (the runtime wired the tracer end to end).
    with tempfile.TemporaryDirectory() as d:
        pipeline = {
            "nodes": {"echo": {"type": "agent", "template": "hi", "model": "fake:x", "parse": False}},
            "graph": {"start": "s", "stages": {"s": {"node": "echo"}}},
        }
        with open(os.path.join(d, "pipe.json"), "w") as f:
            json.dump(pipeline, f)
        trace_path = os.path.join(d, "trace.jsonl")
        root = {
            "transport": {"type": "inproc"},
            "providers": {"fake": {"type": "fake", "default": "done"}},
            "default_provider": "fake",
            "trace": {"mode": "tracer", "capture": ["phase", "cost"],
                      "sinks": [{"type": "file", "path": trace_path}]},
            "pipeline": "pipe.json",
            "run": True,  # no input file -> empty payload
        }
        asyncio.run(r.run_root(root, d))
        with open(trace_path) as f:
            records = [json.loads(line) for line in f if line.strip()]
        names = {rec["name"] for rec in records}
        assert "stage" in names and "model_call" in names, names
        stage = next(rec for rec in records if rec["name"] == "stage")
        assert stage["stage"] == "s" and stage["status"] == "ok"


def scenario_trace_off() -> None:
    # mode: none -> NullTracer, no sink writes
    tracer = asyncio.run(r._build_tracer({"trace": {"mode": "none"}}, comms=None, base=""))
    assert tracer.captures == frozenset()


def scenario_trace_config_errors() -> None:
    # unknown mode / capture / sink type all fail fast with actionable messages;
    # the legacy singular `sink` key is rejected loudly (the silent-no-op fix)
    for spec, needle in (
        ({"mode": "tracor"}, "trace.mode"),
        ({"capture": ["phaze"]}, "trace capture"),
        ({"sinks": {"type": "kafka"}}, "trace sink type"),
        ({"sink": {"type": "console"}}, "use trace.sinks"),
    ):
        try:
            asyncio.run(r._build_tracer({"trace": spec}, comms=None, base=""))
            raise AssertionError("expected ValueError for {}".format(spec))
        except ValueError as e:
            assert needle in str(e), (spec, str(e))


def scenario_trace_async_subscribe() -> None:
    # the transport-seam fix: a transport whose subscribe is ASYNC (like NATS) must
    # have its coroutine awaited, else the sink never attaches (the dropped-coroutine
    # bug). Stub an async-subscribe comms and assert the subscription happened.
    class AsyncSubComms:
        def __init__(self):
            self.subscribed = []
        async def subscribe(self, topic, handler):  # async, like NatsComms
            self.subscribed.append(topic)
            return object()

    comms = AsyncSubComms()
    tracer = asyncio.run(r._build_tracer(
        {"trace": {"capture": ["phase"], "sinks": [{"type": "console"}]}}, comms, base=""))
    assert comms.subscribed == ["trace"], comms.subscribed  # awaited, sink attached
    assert tracer.captures == frozenset({"phase"})


def scenario_stats_sink_price_map_file() -> None:
    # `price_map` as a JSON file path (config-dir relative): one rate card shared
    # by every root instead of pasting the rates into each (theme F).
    rates = {"m": {"input": 1.0, "output": 2.0}}
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "prices.json"), "w") as f:
            json.dump(rates, f)
        from yaah.runtime_factories import _TRACE_SINK_TYPES
        factory = _TRACE_SINK_TYPES["stats_file"][0]
        sink = factory({"path": os.path.join(d, "s.json"), "price_map": "prices.json"}, d)
        assert sink._price_map == rates, sink._price_map
        inline = factory({"path": os.path.join(d, "s.json"), "price_map": rates}, d)
        assert inline._price_map == rates  # inline dict still works


def scenario_cli_parser() -> None:
    # Elegance #4 (assessment): the hand-rolled parser silently fell through to
    # "run" on any unknown flag. The new `_parse_cli` errors out loud and
    # documents itself via -h/--help.
    from yaah.runtime import _parse_cli

    assert _parse_cli(["root.json"]) == {"action": "run", "root": "root.json", "fake": False, "debug": False}
    assert _parse_cli(["root.json", "--list"]) == {"action": "list", "root": "root.json", "fake": False, "debug": False, "json": False}
    assert _parse_cli(["root.json", "--clear"]) == {"action": "clear", "root": "root.json", "fake": False, "debug": False}
    assert _parse_cli(["root.json", "--resume", "B"]) == {
        "action": "resume", "root": "root.json", "fake": False, "debug": False,
        "baton_id": "B", "decision_file": None}
    assert _parse_cli(["root.json", "--resume", "B", "d.json"]) == {
        "action": "resume", "root": "root.json", "fake": False, "debug": False,
        "baton_id": "B", "decision_file": "d.json"}

    # --fake / --debug are order-independent and compose with each action
    assert _parse_cli(["root.json", "--fake"]) == {"action": "run", "root": "root.json", "fake": True, "debug": False}
    assert _parse_cli(["root.json", "--debug"]) == {"action": "run", "root": "root.json", "fake": False, "debug": True}
    assert _parse_cli(["root.json", "--fake", "--list"]) == {"action": "list", "root": "root.json", "fake": True, "debug": False, "json": False}
    assert _parse_cli(["root.json", "--list", "--fake"]) == {"action": "list", "root": "root.json", "fake": True, "debug": False, "json": False}
    assert _parse_cli(["root.json", "--debug", "--fake", "--resume", "B"]) == {
        "action": "resume", "root": "root.json", "fake": True, "debug": True,
        "baton_id": "B", "decision_file": None}

    # unknown flag, missing root, extra args, --resume without id — all exit
    for bad in [[], ["root.json", "--bogus"],
                ["root.json", "--list", "extra"],
                ["root.json", "--clear", "extra"],
                ["root.json", "--resume"],
                ["root.json", "--resume", "B", "d.json", "extra"]]:
        try:
            _parse_cli(bad)
        except SystemExit as e:
            assert e.code == 2, bad
            continue
        raise AssertionError("expected SystemExit for {!r}".format(bad))

    # -h / --help exit with code 0 (the documented help path)
    for ok in [["-h"], ["--help"], ["root.json", "-h"], ["root.json", "--help"]]:
        try:
            _parse_cli(ok)
        except SystemExit as e:
            assert e.code == 0, ok
            continue
        raise AssertionError("expected SystemExit(0) for {!r}".format(ok))


def scenario_read_json_extends() -> None:
    """`_read_json` resolves `_extends` with deep-merge + JSON-Merge-Patch
    delete semantics (the example app assessment #6b). Verifies: a thin overlay over
    a canonical base produces the same merged result as the previous full file;
    a `null` overlay value deletes the base key; lists REPLACE; cycles raise.
    """
    from yaah.runtime_factories import _read_json

    with tempfile.TemporaryDirectory() as td:
        base_path = os.path.join(td, "base.json")
        with open(base_path, "w") as f:
            json.dump({"a": 1, "b": {"x": 10, "y": 20, "drop_me": "gone"},
                       "c": [1, 2, 3]}, f)
        overlay_path = os.path.join(td, "overlay.json")
        with open(overlay_path, "w") as f:
            json.dump({"_extends": "base.json",
                       "b": {"y": 99, "z": 100, "drop_me": None},
                       "c": [9]}, f)
        got = _read_json(overlay_path)
        assert got == {"a": 1, "b": {"x": 10, "y": 99, "z": 100}, "c": [9]}, got

    # 3-level chain.
    with tempfile.TemporaryDirectory() as td:
        a, b, c = (os.path.join(td, n) for n in ("a.json", "b.json", "c.json"))
        with open(a, "w") as f: json.dump({"x": 1, "y": 2, "z": 3}, f)
        with open(b, "w") as f: json.dump({"_extends": "a.json", "y": 20}, f)
        with open(c, "w") as f: json.dump({"_extends": "b.json", "z": 30}, f)
        assert _read_json(c) == {"x": 1, "y": 20, "z": 30}

    # Cycle.
    with tempfile.TemporaryDirectory() as td:
        a, b = (os.path.join(td, n) for n in ("a.json", "b.json"))
        with open(a, "w") as f: json.dump({"_extends": "b.json", "x": 1}, f)
        with open(b, "w") as f: json.dump({"_extends": "a.json", "y": 2}, f)
        try:
            _read_json(a)
        except ValueError as e:
            assert "cycle" in str(e), str(e)
        else:
            raise AssertionError("expected cycle to raise")

    # Passthrough (no _extends).
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "x.json")
        with open(p, "w") as f: json.dump({"q": "p"}, f)
        assert _read_json(p) == {"q": "p"}


def scenario_read_json_bad_json_names_file() -> None:
    """Usability-gap §7 fix: a JSONDecodeError surfaces with the file path
    prefixed so the operator knows WHICH file is malformed. Without this, a
    multi-file pipeline gives a bare `Expecting value: line 1 column 1` with
    no hint which of root / pipeline / decision file the user mistyped."""
    from yaah.runtime_factories import _read_json

    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "broken.json")
        with open(p, "w") as f:
            f.write("this is not json")
        try:
            _read_json(p)
        except ValueError as e:
            msg = str(e)
            assert "broken.json" in msg, msg              # the file is named
            assert "invalid JSON" in msg, msg              # operator-readable framing
            assert "Expecting value" in msg, msg           # decoder detail preserved
        else:
            raise AssertionError("expected bad JSON to raise ValueError")


def scenario_data_flow_contract_load_time() -> None:
    """The data-flow contract (agent reply is a STRING in payload['raw']) is
    now enforced at LOAD time, not just at runtime via the
    `render_unfilled_placeholders` failure mid-run. This converts the project's
    biggest documented authoring footgun (CHECK 8 in the pre-submission rubric)
    from 'fails when you run it' to 'fails when you save it'."""
    from yaah.validate import validate_pipeline

    # BAD: agent → render with no parse between. parse=False is the explicit
    # opt-out; without it the agent self-parses (ADR-0004) and the contract
    # holds. The check fires only when the user actively opts out.
    bad_render = {
        "nodes": {
            "role:agent":  {"type": "agent", "template": "x", "model": "fake:x",
                            "parse": False},
            "role:render": {"type": "render", "template_text": "{{summary}}"},
        },
        "graph": {"start": "a", "stages": {
            "a": {"node": "role:agent", "then": "r"},
            "r": {"node": "role:render", "then": None},
        }},
    }
    try:
        validate_pipeline(bad_render)
    except ValueError as e:
        msg = str(e)
        assert "agent" in msg and "render" in msg, msg
        assert "transform" in msg and "envelope" in msg, msg
        assert "allow_unfilled" in msg, msg
    else:
        raise AssertionError("agent → render without parse must fail validation")

    # OPT-OUT: agent (parse=False) → render with allow_unfilled is permitted
    # (the explicit "the unparsed payload is intentional" escape hatch).
    opt_out_render = {
        "nodes": {
            "role:agent":  {"type": "agent", "template": "x", "model": "fake:x",
                            "parse": False},
            "role:render": {"type": "render", "template_text": "{{raw}}",
                            "allow_unfilled": True},
        },
        "graph": {"start": "a", "stages": {
            "a": {"node": "role:agent", "then": "r"},
            "r": {"node": "role:render", "then": None},
        }},
    }
    validate_pipeline(opt_out_render)  # no raise

    # GOOD: agent → transform (parse) → render is the canonical shape.
    good = {
        "nodes": {
            "role:agent":  {"type": "agent", "template": "x", "model": "fake:x"},
            "role:parse":  {"type": "transform", "target": "fn:t:parse",
                            "call": "envelope"},
            "role:render": {"type": "render", "template_text": "{{summary}}"},
        },
        "graph": {"start": "a", "stages": {
            "a": {"node": "role:agent",  "then": "p"},
            "p": {"node": "role:parse",  "then": "r"},
            "r": {"node": "role:render", "then": None},
        }},
    }
    validate_pipeline(good)  # no raise

    # BAD: agent (parse=False) → stage-with-branch where the merging stage's
    # node is NOT one of the two exceptions (transform/human_gate).
    # expect_field doesn't merge keys; branching off it after an opted-out
    # agent reads as missing.
    bad_branch = {
        "nodes": {
            "role:agent":  {"type": "agent", "template": "x", "model": "fake:x",
                            "parse": False},
            "role:check":  {"type": "expect_field", "key": "ok", "equals": True},
        },
        "graph": {"start": "a", "stages": {
            "a": {"node": "role:agent", "then": "c"},
            "c": {"node": "role:check",
                  "branch": {"on": "ok", "routes": {True: "a"}, "default": None}},
        }},
    }
    try:
        validate_pipeline(bad_branch)
    except ValueError as e:
        msg = str(e)
        # ADR-0006 §D5: the check is now consumer-centric and graph-wide — it names the branch
        # stage + the provably-absent key + the tag (not the specific upstream agent). The
        # expect_field passthrough keeps the parse=false agent's CLOSED set, so `ok` is absent.
        assert "branch" in msg and "'ok'" in msg, msg
        assert "branch-key-absent" in msg, msg
    else:
        raise AssertionError(
            "agent → expect_field-with-branch must fail (the validator doesn't merge)")

    # EXCEPTION: agent → human_gate-with-branch is OK — the human gate
    # merges the operator's decision.json into the payload during resume.
    agent_to_human_gate = {
        "nodes": {
            "role:writer": {"type": "agent", "template": "draft", "model": "fake:writer"},
            "role:gate":   {"type": "human_gate", "ask": "Approve?", "awaiting": "review"},
        },
        "graph": {"start": "w", "stages": {
            "w": {"node": "role:writer", "then": "g"},
            "g": {"node": "role:gate",
                  "branch": {"on": "decision", "routes": {"revise": "w"}, "default": None}},
        }},
    }
    validate_pipeline(agent_to_human_gate)  # no raise — human_gate IS a merge point

    # EXCEPTION: agent → transform-with-branch is OK — the transform IS the
    # parse step (the whole point of having a branch right after).
    agent_to_transform_branch = {
        "nodes": {
            "role:agent": {"type": "agent", "template": "x", "model": "fake:x"},
            "role:parse_and_route": {"type": "transform",
                                     "target": "fn:t:parse", "call": "envelope"},
        },
        "graph": {"start": "a", "stages": {
            "a": {"node": "role:agent", "then": "p"},
            "p": {"node": "role:parse_and_route",
                  "branch": {"on": "route", "routes": {"x": "a"}, "default": None}},
        }},
    }
    validate_pipeline(agent_to_transform_branch)  # no raise


def scenario_main_catches_import_error() -> None:
    """Y1 — an `ImportError` raised on an eager build path (e.g. attach: [fn:...]
    whose module isn't on PYTHONPATH) used to escape main() as a 30-line
    ModuleNotFoundError traceback because the handler caught only ValueError/
    OSError. Now ImportError is in the same except clause: the operator gets
    one actionable line + exit 2, matching the existing config-error UX."""
    # Monkey-patch the dispatcher to raise the exact exception class — tests
    # main()'s except clause directly, independent of WHERE in the engine the
    # eager import lives. The CLI plumbing lives in yaah.cli since the B3.1b
    # refactor; patch there. (The lazy `fn:` path inside a stage gets wrapped
    # as StageFailed instead, which is a different code path; this test pins
    # the clean-exit contract for the eager build path.)
    from yaah import cli as yc
    prev_dispatch = yc._dispatch
    prev_argv = sys.argv
    sys.argv = ["yaah", "irrelevant.json"]
    yc._dispatch = lambda _spec: (_ for _ in ()).throw(
        ModuleNotFoundError("No module named 'yaah_app_demo'"))
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            yc.main()
    except SystemExit as e:
        assert e.code == 2, "expected clean exit 2 for ImportError, got {}".format(e.code)
    else:
        raise AssertionError("expected SystemExit; ImportError escaped")
    finally:
        yc._dispatch = prev_dispatch
        sys.argv = prev_argv
    # the operator sees ONE line, not a traceback; the module name is in it
    # so they know what to add to PYTHONPATH.
    msg = stderr.getvalue()
    assert msg.startswith("error: "), msg
    assert "yaah_app_demo" in msg, msg


def main() -> None:
    scenario_resolve_serve()
    scenario_validate_root()
    scenario_decider()
    scenario_trace_file_sink()
    scenario_trace_off()
    scenario_trace_config_errors()
    scenario_trace_async_subscribe()
    scenario_stats_sink_price_map_file()
    scenario_cli_parser()
    scenario_read_json_extends()
    scenario_read_json_bad_json_names_file()
    scenario_data_flow_contract_load_time()
    scenario_main_catches_import_error()
    print("ok")


if __name__ == "__main__":
    main()
