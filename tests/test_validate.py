"""R15 — one entry for config validation: `yaah.validate.validate_root` and
`yaah.validate.validate_pipeline`. Consolidates what used to live in
`runtime._validate_root` (top-level keys), `runtime._validate_root_shapes`
(typed-block / named-map / string / bool shape), and `build.build.validate_pipeline`
(graph cross-refs); ADDS did-you-mean for the enum sites in `runtime_factories`
(trace.mode / trace.capture / trace.sinks[].type / transport.type / state.type) AND
the cross-field check "capture configured but tracer is off".

The point of R15 is one documented surface the AI skill (R16) can ground on, with
actionable errors at LOAD time instead of mid-build factory ValueErrors.

Run: cd yaah && PYTHONPATH=src python3 tests/test_validate.py
"""
from __future__ import annotations

from typing import Any, Dict

from yaah.validate import validate_pipeline, validate_root


def _valid_root() -> Dict[str, Any]:
    return {
        "transport": {"type": "inproc"},
        "providers": {"claude": {"type": "claude_cli"}},
        "default_provider": "claude",
        "prompt_sources": {"file": {"type": "file", "dir": "prompts"}},
        "default_prompt_source": "file",
        "state": {"type": "memory"},
        "pipeline": "p.json",
        "input": "i.json",
        "run": True,
    }


def _valid_pipeline() -> Dict[str, Any]:
    return {
        "nodes": {"x": {"type": "transform", "target": "fn:m:f"}},
        "graph": {"start": "s1", "stages": {"s1": {"node": "x"}}},
    }


# ----- top-level keys (was runtime._validate_root) -----

def test_valid_root_passes() -> None:
    validate_root(_valid_root())  # must not raise


def test_unknown_top_level_key_with_did_you_mean() -> None:
    root = _valid_root()
    root["transprt"] = {"type": "inproc"}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "transprt" in msg
        assert "transport" in msg, "did-you-mean must surface the close key"
        return
    raise AssertionError("unknown top-level key should raise")


def test_underscore_keys_treated_as_comments() -> None:
    root = _valid_root()
    root["_about"] = "this is a comment"
    root["_fake"] = {"providers": {"claude": {"type": "claude_cli"}}}
    validate_root(root)  # must not raise


# ----- shape (was runtime._validate_root_shapes) -----

def test_bare_string_transport_suggests_typed_block() -> None:
    root = _valid_root()
    root["transport"] = "inproc"
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "transport" in msg
        assert '"type": "inproc"' in msg, "rewrite-to suggestion required"
        return
    raise AssertionError("bare-string transport should raise")


def test_bare_string_providers_suggests_named_map() -> None:
    root = _valid_root()
    root["providers"] = "claude_cli"
    try:
        validate_root(root)
    except ValueError as e:
        assert "providers" in str(e) and "named-map" in str(e)
        return
    raise AssertionError("bare-string providers should raise")


def test_run_must_be_bool() -> None:
    root = _valid_root()
    root["run"] = "true"
    try:
        validate_root(root)
    except ValueError as e:
        assert "run" in str(e) and "bool" in str(e)
        return
    raise AssertionError("non-bool run should raise")


# ----- enum did-you-mean (NEW in R15) -----

def test_trace_mode_did_you_mean() -> None:
    root = _valid_root()
    root["trace"] = {"mode": "tracor"}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "trace.mode" in msg
        assert "tracor" in msg
        assert "tracer" in msg, "did-you-mean must suggest the close enum"
        return
    raise AssertionError("bad trace.mode should raise at load (not at factory)")


def test_transport_type_did_you_mean() -> None:
    root = _valid_root()
    root["transport"] = {"type": "inprc"}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "transport.type" in msg
        assert "inproc" in msg
        return
    raise AssertionError("bad transport.type should raise at load")


def test_state_type_did_you_mean() -> None:
    root = _valid_root()
    root["state"] = {"type": "memry"}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "state.type" in msg
        assert "memory" in msg
        return
    raise AssertionError("bad state.type should raise at load")


def test_trace_capture_did_you_mean() -> None:
    root = _valid_root()
    root["trace"] = {"mode": "tracer", "capture": ["phse"]}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "trace.capture" in msg
        assert "phse" in msg and "phase" in msg
        return
    raise AssertionError("bad trace.capture should raise at load")


def test_trace_sink_type_did_you_mean() -> None:
    root = _valid_root()
    root["trace"] = {"mode": "tracer", "sinks": [{"type": "consle"}]}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "trace" in msg and "sinks" in msg
        assert "consle" in msg and "console" in msg
        return
    raise AssertionError("bad trace.sinks[].type should raise at load")


# ----- unknown spec keys (derived from the factory maps' spec-keys) -----

def test_trace_singular_sink_rejected_with_did_you_mean() -> None:
    """THE sink/sinks bug: factory read `sink`, validator checked `sinks` — every
    seed base silently lost its sinks. Now `sink` is an unknown trace key with a
    did-you-mean, caught at load."""
    root = _valid_root()
    root["trace"] = {"mode": "tracer", "sink": [{"type": "console"}]}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "'sink'" in msg
        assert "sinks" in msg, "did-you-mean must suggest the plural"
        return
    raise AssertionError("trace.sink (singular) should raise at load")


def test_unknown_key_in_trace_sink_entry() -> None:
    root = _valid_root()
    root["trace"] = {"sinks": [{"type": "file", "pth": "t.jsonl"}]}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "pth" in msg and "path" in msg
        return
    raise AssertionError("unknown sink-entry key should raise at load")


def test_unknown_key_in_provider_entry() -> None:
    root = _valid_root()
    root["providers"]["fake"] = {"type": "fake", "respnses": {"x": "y"}}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "respnses" in msg and "responses" in msg
        return
    raise AssertionError("unknown provider spec key should raise at load")


def test_open_spec_provider_keys_pass_through() -> None:
    # claude_cli forwards **kwargs to the constructor (spec-keys None = open);
    # the validator must NOT reject its keys — the constructor enforces them.
    root = _valid_root()
    root["providers"]["claude"] = {"type": "claude_cli", "cli_path": "/usr/bin/claude"}
    validate_root(root)  # must not raise


def test_unknown_key_on_inproc_transport() -> None:
    # `url` on inproc is a silent no-op (only nats reads it) — flag it at load.
    root = _valid_root()
    root["transport"] = {"type": "inproc", "url": "nats://localhost:4222"}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "url" in msg and "inproc" in msg
        return
    raise AssertionError("inproc transport with nats keys should raise at load")


# ----- cross-field (NEW in R15) -----

def test_capture_with_tracer_off_is_an_error() -> None:
    """The user almost certainly didn't mean to silently drop captures by saying
    `mode: none`. R15 surfaces this load-time cross-field mistake."""
    root = _valid_root()
    root["trace"] = {"mode": "none", "capture": ["phase", "cost"]}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "capture" in msg and "none" in msg
        return
    raise AssertionError("capture+mode=none should raise (cross-field)")


def test_trace_must_be_a_dict() -> None:
    """assessment #8: `"trace": "none"` (a bare string) passed shape validation
    and crashed mid-build. trace is keyed on `mode`, not `type`, so the typed-
    block check didn't cover it."""
    root = _valid_root()
    root["trace"] = "none"
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "trace" in msg and '"mode": "none"' in msg, msg
        return
    raise AssertionError("a bare-string trace block should raise at load")


def test_envelope_mode_with_sinks_is_an_error() -> None:
    """assessment #8: envelope carriage has no bus and no sinks — a `sinks` (or
    `topic`) key under mode 'envelope' is silently ignored by the factory, so
    the validator must flag it."""
    root = _valid_root()
    root["trace"] = {"mode": "envelope", "sinks": [{"type": "console"}], "topic": "t"}
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "envelope" in msg and "trace.sinks" in msg and "trace.topic" in msg, msg
        return
    raise AssertionError("sinks/topic under mode=envelope should raise (cross-field)")


def test_tracer_mode_with_buffer_max_is_an_error() -> None:
    """The buffer only exists in envelope mode; tracer mode reading it would be
    a silent no-op."""
    root = _valid_root()
    root["trace"] = {"mode": "tracer", "buffer_max": 64}
    try:
        validate_root(root)
    except ValueError as e:
        assert "buffer_max" in str(e), str(e)
        return
    raise AssertionError("buffer_max under mode=tracer should raise (cross-field)")


# ----- all errors gathered into one pass -----

def test_multiple_errors_gathered() -> None:
    root = _valid_root()
    root["transport"] = "inproc"            # shape
    root["state"] = {"type": "memry"}       # enum did-you-mean
    root["trace"] = {"mode": "tracor"}      # enum did-you-mean
    root["unknown_key"] = 1                 # unknown top-level
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        for needle in ("transport", "state.type", "trace.mode", "unknown_key"):
            assert needle in msg, "missing {!r} in: {}".format(needle, msg)
        return
    raise AssertionError("multiple errors should be reported in one pass")


# ----- pipeline graph (was build.build.validate_pipeline) -----

def test_pipeline_valid_passes() -> None:
    validate_pipeline(_valid_pipeline())


def test_pipeline_typo_then_surfaces() -> None:
    p = _valid_pipeline()
    p["graph"]["stages"]["s1"]["then"] = "s2"  # not a stage
    try:
        validate_pipeline(p)
    except ValueError as e:
        msg = str(e)
        assert "then" in msg and "s2" in msg
        return
    raise AssertionError("typo'd then should raise")


def test_pipeline_unknown_node_role() -> None:
    p = _valid_pipeline()
    p["graph"]["stages"]["s1"]["node"] = "notdeclared"
    try:
        validate_pipeline(p)
    except ValueError as e:
        msg = str(e)
        assert "notdeclared" in msg
        return
    raise AssertionError("unknown node role should raise")


def test_pipeline_typeless_node_names_stale_overlay() -> None:
    # BUG-695 #6b: an `_extends` overlay keyed on a role the base renamed
    # produces an orphan, typeless node after the merge — name it at build
    p = _valid_pipeline()
    p["nodes"]["role:green"] = {"command": ["true"]}   # stale key, no type
    p["nodes"]["role:green-run"] = {"type": "transform", "target": "fn:m:f"}
    try:
        validate_pipeline(p)
    except ValueError as e:
        msg = str(e)
        assert "role:green" in msg and "stale overlay" in msg, msg
        assert "role:green-run" in msg, msg   # the did-you-mean
        return
    raise AssertionError("typeless node should raise")


# ----- ordering constraints (gate-ordering rules as config) -----

def _branchy_pipeline() -> Dict[str, Any]:
    """gate -> work, with a value-routed loop back (work -> gate on 'retry')
    and a bypass route start -> work that skips the gate. Exercises both the
    loop-tolerance (a back-edge alone must NOT fail 'precedes') and the
    bypass detection (a start-route around the early stage MUST fail)."""
    return {
        "nodes": {"x": {"type": "transform", "target": "fn:m:f"}},
        "graph": {
            "start": "entry",
            "stages": {
                "entry": {"node": "x", "branch": {"on": "k", "routes": {"skip": "work"},
                                                  "default": "gate"}},
                "gate": {"node": "x", "then": "work"},
                "work": {"node": "x", "branch": {"on": "k", "routes": {"retry": "gate"},
                                                 "default": "done"}},
                "done": {"node": "x"},
            },
        },
    }


def test_constraint_precedes_holds() -> None:
    p = _branchy_pipeline()
    # remove the bypass: every path to work passes the gate; the work->gate
    # back-loop must NOT trip the check (dominator semantics, not reverse-reach)
    p["graph"]["stages"]["entry"].pop("branch")
    p["graph"]["stages"]["entry"]["then"] = "gate"
    p["graph"]["constraints"] = {"precedes": [["gate", "work"], ["gate", "done"]]}
    validate_pipeline(p)  # must not raise


def test_constraint_precedes_catches_bypass() -> None:
    p = _branchy_pipeline()
    p["graph"]["constraints"] = {"precedes": [["gate", "work"]]}
    try:
        validate_pipeline(p)
    except ValueError as e:
        msg = str(e)
        assert "'work'" in msg and "'gate'" in msg and "bypass" in msg, msg
        return
    raise AssertionError("the start->work bypass route should violate gate-precedes-work")


def test_constraint_names_must_be_stages() -> None:
    p = _branchy_pipeline()
    p["graph"]["constraints"] = {"precedes": [["gate", "wrok"]]}
    try:
        validate_pipeline(p)
    except ValueError as e:
        assert "wrok" in str(e) and "work" in str(e), str(e)  # did-you-mean
        return
    raise AssertionError("unknown stage in a constraint should raise")


def test_constraint_unknown_key_and_shape() -> None:
    p = _branchy_pipeline()
    p["graph"]["constraints"] = {"preceeds": [["gate", "work"]]}
    try:
        validate_pipeline(p)
    except ValueError as e:
        assert "preceeds" in str(e) and "precedes" in str(e), str(e)
        return
    raise AssertionError("unknown constraints key should raise")


# ----- timeout budget coherence (BUG-635/626 class) -----

def test_budget_node_timeout_exceeds_transport_window() -> None:
    from yaah.validate import validate_budgets
    root = {"transport": {"type": "nats", "request_timeout": 60}}
    p = _valid_pipeline()
    p["nodes"]["x"]["timeout"] = 120
    try:
        validate_budgets(root, p)
    except ValueError as e:
        msg = str(e)
        assert "'x'" in msg and "120" in msg and "60" in msg, msg
        return
    raise AssertionError("node timeout > request_timeout should raise")


def test_budget_inproc_has_no_reply_window() -> None:
    from yaah.validate import validate_budgets
    p = _valid_pipeline()
    p["nodes"]["x"]["timeout"] = 9999
    validate_budgets({"transport": {"type": "inproc"}}, p)  # must not raise
    validate_budgets({}, p)                                  # no transport at all


def test_budget_fork_wait_smaller_than_branch_node_timeout() -> None:
    from yaah.validate import validate_budgets
    p = {
        "nodes": {"x": {"type": "transform", "target": "fn:m:f"},
                  "slow": {"type": "transform", "target": "fn:m:f", "timeout": 600}},
        "graph": {"start": "f", "stages": {
            "f": {"fork": ["a", "b"], "then": "after",
                  "wait": {"timeout": 300}},
            "a": {"node": "slow", "then": "j"},
            "b": {"node": "x", "then": "j"},
            "j": {"fanin": {"expect": ["a", "b"]}},
            "after": {"node": "x"},
        }},
    }
    try:
        validate_budgets({}, p)
    except ValueError as e:
        msg = str(e)
        assert "'f'" in msg and "'a'" in msg and "600" in msg and "300" in msg, msg
        return
    raise AssertionError("fork wait.timeout < branch node timeout should raise")


def main() -> None:
    test_valid_root_passes()
    test_unknown_top_level_key_with_did_you_mean()
    test_underscore_keys_treated_as_comments()
    test_bare_string_transport_suggests_typed_block()
    test_bare_string_providers_suggests_named_map()
    test_run_must_be_bool()
    test_trace_mode_did_you_mean()
    test_transport_type_did_you_mean()
    test_state_type_did_you_mean()
    test_trace_capture_did_you_mean()
    test_trace_sink_type_did_you_mean()
    test_trace_singular_sink_rejected_with_did_you_mean()
    test_unknown_key_in_trace_sink_entry()
    test_unknown_key_in_provider_entry()
    test_open_spec_provider_keys_pass_through()
    test_unknown_key_on_inproc_transport()
    test_capture_with_tracer_off_is_an_error()
    test_trace_must_be_a_dict()
    test_envelope_mode_with_sinks_is_an_error()
    test_tracer_mode_with_buffer_max_is_an_error()
    test_multiple_errors_gathered()
    test_pipeline_valid_passes()
    test_pipeline_typo_then_surfaces()
    test_pipeline_unknown_node_role()
    test_pipeline_typeless_node_names_stale_overlay()
    test_constraint_precedes_holds()
    test_constraint_precedes_catches_bypass()
    test_constraint_names_must_be_stages()
    test_constraint_unknown_key_and_shape()
    test_budget_node_timeout_exceeds_transport_window()
    test_budget_inproc_has_no_reply_window()
    test_budget_fork_wait_smaller_than_branch_node_timeout()
    print("test_validate: PASS (32 scenarios)")


if __name__ == "__main__":
    main()
