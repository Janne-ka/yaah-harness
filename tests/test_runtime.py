"""Runtime helpers: serve-placement resolution and the gate-driver decider.

Covers the pure config->runtime helpers added for distribution (serve by
placement) and gates (the decider that backs drive()), without spinning a
transport.

Run: cd yaah && PYTHONPATH=src python3 tests/test_runtime.py
"""
from __future__ import annotations

import asyncio
import json
import os
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
            "nodes": {"echo": {"type": "agent", "template": "hi", "model": "fake:x"}},
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
    print("ok")


if __name__ == "__main__":
    main()
