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


# ----- cross-field: a default_* must name a declared map entry -----

def test_dangling_default_provider_rejected() -> None:
    """A `default_provider` that names no declared provider used to slip past load
    validation and surface as a runtime LookupError on the first model call. R15
    catches it at LOAD with the layer's noun and the available names."""
    root = _valid_root()
    root["default_provider"] = "ghost"   # only "claude" is declared
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "default_provider" in msg and "ghost" in msg, msg
        assert "provider" in msg, msg            # the layer's noun
        assert "claude" in msg, msg              # the available names listed
        return
    raise AssertionError("a dangling default_provider should raise at load")


def test_valid_default_provider_passes() -> None:
    """The positive case: a default pointing at a declared provider must NOT raise
    (the valid baseline already exercises this, but pin it explicitly)."""
    root = _valid_root()
    root["default_provider"] = "claude"   # declared in _valid_root
    validate_root(root)  # must not raise


def test_dangling_default_prompt_source_rejected() -> None:
    """A second layer, proving the check is generic over every default_* pair and
    not special-cased to providers."""
    root = _valid_root()
    root["default_prompt_source"] = "nowhere"   # only "file" is declared
    try:
        validate_root(root)
    except ValueError as e:
        msg = str(e)
        assert "default_prompt_source" in msg and "nowhere" in msg, msg
        assert "prompt source" in msg, msg       # the layer's noun
        assert "file" in msg, msg                # the available names listed
        return
    raise AssertionError("a dangling default_prompt_source should raise at load")


def test_default_without_a_map_is_not_newly_rejected() -> None:
    """Regression: an absent or empty providers map must NOT be newly rejected by
    the default-resolution check. A default whose map is missing/empty/non-dict is
    either flagged elsewhere (shape check) or legitimately deferred — this check
    only fires for a NON-EMPTY default against a present, dict-shaped map."""
    from yaah.validate import _check_cross_field
    # absent map, default present: _check_cross_field must add no error
    errs: list = []
    _check_cross_field({"default_data_source": "ghost"}, errs)
    assert errs == [], "absent map must not be flagged by cross-field: {}".format(errs)
    # empty map, default present: same
    errs = []
    _check_cross_field({"data_sources": {}, "default_data_source": "ghost"}, errs)
    assert errs == [], "empty map must not be flagged by cross-field: {}".format(errs)
    # empty-string default against a real map: nothing to resolve, no error
    errs = []
    _check_cross_field(
        {"providers": {"claude": {"type": "claude_cli"}}, "default_provider": ""}, errs)
    assert errs == [], "empty-string default must not be flagged: {}".format(errs)


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


def test_pipeline_on_error_shape_is_hard_checked() -> None:
    # A typo'd on_error would otherwise be a SILENT no-op ("claer" matches no
    # recovery branch) or silently flip rollback-failure severity ("warning" ->
    # the "error" default) — recovery the author believes exists must not
    # quietly disappear, so these are load errors.
    import copy
    cases = [
        ("claer", "on_error must be"),
        ({"compensate": ""}, "non-empty"),
        ({"compensate": "fn:m:f", "on_compensate_fail": "warning"}, "on_compensate_fail"),
        ({"compensate": "fn:m:f", "on_compensat_fail": "warn"}, "unknown on_error key"),
    ]
    for bad, expect in cases:
        p = _valid_pipeline()
        p["graph"]["stages"]["s1"]["on_error"] = copy.deepcopy(bad)
        try:
            validate_pipeline(p)
        except ValueError as e:
            assert expect in str(e), (bad, str(e))
        else:
            raise AssertionError("bad on_error {!r} should raise".format(bad))
    for good in ("clear", None, {"compensate": "fn:m:f"},
                 {"compensate": "fn:m:f", "on_compensate_fail": "warn"}):
        p = _valid_pipeline()
        p["graph"]["stages"]["s1"]["on_error"] = good
        validate_pipeline(p)  # must not raise


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


# ----- per-node-type spec keys (the silent-no-op class, one level below _STAGE_KEYS) -----

def _shell_pipeline(**node_extra: Any) -> Dict[str, Any]:
    node = {"type": "shell", "command": ["true"]}
    node.update(node_extra)
    return {"nodes": {"x": node},
            "graph": {"start": "s1", "stages": {"s1": {"node": "x"}}}}


def test_node_unknown_key_rejected() -> None:
    # docs/shape-grammar.md promised this since it was written; nothing enforced it,
    # and a pipeline carried target_from/interpolate_from for five weeks against an
    # engine that dropped both silently.
    try:
        validate_pipeline(_shell_pipeline(bogus_key_xyz=1))
    except ValueError as e:
        msg = str(e)
        assert "'x'" in msg and "bogus_key_xyz" in msg, msg
        assert "shell" in msg, msg
        assert "target_from" in msg and "command" in msg, msg   # the legal set is printed
        return
    raise AssertionError("an unknown node key should raise")


def test_node_unknown_key_did_you_mean() -> None:
    try:
        validate_pipeline(_shell_pipeline(targt_from="written_tests"))
    except ValueError as e:
        assert "did you mean 'target_from'?" in str(e), str(e)
        return
    raise AssertionError("a near-miss node key should raise")


def test_node_key_legal_on_another_type_is_still_rejected() -> None:
    # `prompt` is an agent key. On a transform nothing reads it — which is exactly
    # what an overlay that flips a node's `type` leaves behind.
    p = {"nodes": {"x": {"type": "transform", "target": "fn:m:f", "prompt": "file:x"}},
         "graph": {"start": "s1", "stages": {"s1": {"node": "x"}}}}
    try:
        validate_pipeline(p)
    except ValueError as e:
        assert "'prompt'" in str(e) and "transform" in str(e), str(e)
        return
    raise AssertionError("a foreign-type node key should raise")


def test_node_underscore_keys_are_legal_everywhere() -> None:
    validate_pipeline(_shell_pipeline(_comment="why", _about="doc",
                                      _interpolate_from_note="see the overlay"))


def test_node_common_keys_legal_on_every_type() -> None:
    validate_pipeline(_shell_pipeline(model="claude:haiku", timeout=30, retries=1,
                                      note="a comment", placement="cloud",
                                      provides=["exit_code"], idempotent=False,
                                      config={"k": 1}, effort="low",
                                      temperature=0.0, idempotency_key="k"))


def test_custom_node_type_keys_are_not_checked() -> None:
    # an embedding app's registered type: the engine has no builder for it, so its
    # keys are the app's business (the same sound skip the contract resolver takes)
    validate_pipeline({"nodes": {"x": {"type": "widget", "whatever": 1}},
                       "graph": {"start": "s1", "stages": {"s1": {"node": "x"}}}})


def test_allow_unknown_node_keys_escape_hatch() -> None:
    # the release valve for a pipeline authored against a NEWER engine
    p = _shell_pipeline(bogus_key_xyz=1)
    p["allow_unknown_node_keys"] = True
    validate_pipeline(p)


def test_node_key_table_covers_every_built_in_type() -> None:
    # the drift guard: a new builder with no key row would silently check nothing.
    from yaah.build.builders import default_registry
    from yaah.node_keys import BUILTIN_NODE_KEYS
    built = set(default_registry()._builders)          # noqa: SLF001
    assert built == set(BUILTIN_NODE_KEYS), (built ^ set(BUILTIN_NODE_KEYS))


def test_node_key_table_covers_every_spec_read_in_builders() -> None:
    # the OTHER drift direction: a builder that starts reading a new `spec` key
    # without adding it to the table would reject the very config it now supports.
    #
    # The scan is WIDER than builders.py. `builders.py` holds most of the reads, but
    # not all: `build/live_leaf_config.py` re-reads the NodeConfig scalars per
    # invocation, `build/build.py` and `build/registry.py` read `_role`/`type`, and
    # `validate.py` / `replay.py` are the two non-build modules that read a node spec
    # by that name. Scanning only builders.py meant a new key read in any of them
    # would be rejected by the very validator meant to allow it.
    import glob
    import os
    import re

    from yaah.node_keys import BUILTIN_NODE_KEYS, COMMON_NODE_KEYS
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "src", "yaah")
    files = sorted(glob.glob(os.path.join(src, "build", "*.py")))
    files += [os.path.join(src, "validate.py"), os.path.join(src, "replay.py")]
    assert os.path.join(src, "build", "builders.py") in files, files
    read = set()
    for path in files:
        with open(path) as f:
            read |= set(re.findall(r'spec(?:\.get\(|\[)"(\w+)"', f.read()))
    known = set(COMMON_NODE_KEYS).union(*BUILTIN_NODE_KEYS.values())
    missing = sorted(k for k in read if not k.startswith("_") and k not in known)
    assert not missing, "build/validate/replay read node keys absent from node_keys: {}".format(
        missing)


# ----- shell consumes: interpolate_from / target_from checked at LOAD (ADR-0006) -----

def _worktree_then_shell(**shell_extra: Any) -> Dict[str, Any]:
    # a worktree contract is CLOSED ({workdir, branch, repo, base}), so a read of
    # anything else downstream is provably absent -> an ERROR tier, not a warning
    node = {"type": "shell", "command": ["run"]}
    node.update(shell_extra)
    return {
        "nodes": {"wt": {"type": "worktree", "repo": "/tmp/repo"}, "run": node},
        "graph": {"start": "s1", "stages": {"s1": {"node": "wt", "then": "s2"},
                                            "s2": {"node": "run"}}},
    }


def test_shell_interpolate_from_key_never_produced_fails_at_load() -> None:
    # was a runtime TargetError deep in the run; now a load error naming the stage
    p = _worktree_then_shell(command=["run", "--db={{db_url}}"], interpolate_from=["db_url"])
    try:
        validate_pipeline(p)
    except ValueError as e:
        msg = str(e)
        assert "db_url" in msg and "s2" in msg, msg
        assert "shell-key-absent" in msg, msg
        return
    raise AssertionError("an unproduced interpolate_from key should fail at load")


def test_shell_target_from_key_never_produced_fails_at_load() -> None:
    p = _worktree_then_shell(target_from="written_tests")
    try:
        validate_pipeline(p)
    except ValueError as e:
        assert "written_tests" in str(e), str(e)
        return
    raise AssertionError("an unproduced target_from key should fail at load")


def test_shell_reads_satisfied_upstream_pass() -> None:
    validate_pipeline(_worktree_then_shell(command=["run", "--in={{workdir}}"],
                                           interpolate_from=["workdir"],
                                           target_from="branch"))


def test_shell_placeholder_without_interpolate_from_reads_nothing() -> None:
    # without the opt-in a `{{key}}` stays a LITERAL — nothing is read, nothing to check
    validate_pipeline(_worktree_then_shell(command=["run", "--db={{db_url}}"]))


def test_shell_declared_but_unused_interpolate_key_is_not_a_read() -> None:
    # s_factory declares interpolate_from: ["db_url"] centrally while a host overlay
    # decides whether its runner command carries the token. Taking the declared LIST
    # as the read-set would false-alarm on every consumer that doesn't.
    validate_pipeline(_worktree_then_shell(command=["run"], interpolate_from=["db_url"]))


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


def test_budget_lease_horizon_must_fit_the_checkpoint_window() -> None:
    """A record swept before it can be declared stale is incoherent: a crashed run on
    ANOTHER host would be deleted by the TTL sweep while still inside the window that
    says "too fresh to recover", so it could never be recovered at all."""
    from yaah.validate import validate_budgets
    p = _valid_pipeline()
    try:
        validate_budgets({"checkpoint_ttl": 600, "lease_horizon": 3600}, p)
    except ValueError as e:
        msg = str(e)
        assert "lease_horizon" in msg and "3600" in msg and "600" in msg, msg
        assert "checkpoint_ttl" in msg, msg
    else:
        raise AssertionError("lease_horizon > checkpoint_ttl should raise")

    # checkpoint_ttl absent -> the window is baton_ttl, and the message says which
    try:
        validate_budgets({"baton_ttl": 60, "lease_horizon": 3600}, p)
    except ValueError as e:
        assert "baton_ttl" in str(e), str(e)
    else:
        raise AssertionError("lease_horizon > baton_ttl should raise")

    # coherent combinations, and the defaults (3600 vs the 72h default), must pass
    validate_budgets({"checkpoint_ttl": 21600, "lease_horizon": 3600}, p)
    validate_budgets({"lease_horizon": 3600}, p)
    validate_budgets({"checkpoint_ttl": 3600, "lease_horizon": 3600}, p)   # equal is fine
    validate_budgets({}, p)


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


def _parse_false_to(consumer: Dict[str, Any], extra_stage: Dict[str, Any]) -> Dict[str, Any]:
    return {"nodes": {"ask": {"type": "agent", "parse": False}, **{"c": consumer}},
            "graph": {"start": "a", "stages": {
                "a": {"node": "ask", "then": "b"},
                "b": {"node": "c", **extra_stage}}}}


def test_parse_false_render_of_raw_is_accepted() -> None:
    # parse=false DOES provide `raw`, so a render of only {{raw}} is runnable — the hard
    # data-flow check must NOT reject it (it reads the template now, not just the edge shape).
    p = _parse_false_to({"type": "render", "template_text": "Result: {{raw}}"}, {})
    validate_pipeline(p)  # must not raise


def test_parse_false_render_of_unprovided_key_still_fails_loud() -> None:
    # but a render needing a key the agent never produced is still rejected at LOAD, and the
    # error names the missing key — fail-loud preserved, just accurate.
    p = _parse_false_to({"type": "render", "template_text": "{{verdict}}"}, {})
    try:
        validate_pipeline(p)
    except ValueError as e:
        assert "verdict" in str(e), str(e)
        return
    raise AssertionError("parse=false → render needing an unprovided key must still raise")


def test_parse_false_branch_after_validator_reset() -> None:
    # A validator used as a MAIN node returns a Verdict → its output payload is a FRESH
    # {status, severity, failures} (core/verdict.py to_envelope) — it RESETS, so the agent's
    # `raw` is GONE downstream. Branching on `status` (which the validator DOES provide) is
    # fine; branching on `raw`/`verdict` (reset away) fails loud. (The old model wrongly treated
    # the validator as a passthrough and blessed `branch on raw` — a runtime-broken pipeline.)
    on_status = _parse_false_to({"type": "json_object"},
                                {"branch": {"on": "status", "routes": {}, "default": "a"}})
    validate_pipeline(on_status)  # `status` IS in the validator's fresh payload
    for absent in ("raw", "verdict"):
        bad = _parse_false_to({"type": "json_object"},
                              {"branch": {"on": absent, "routes": {}, "default": "a"}})
        try:
            validate_pipeline(bad)
        except ValueError as e:
            assert absent in str(e), str(e)
        else:
            raise AssertionError(
                "branch on {!r} (reset away by the validator) must fail loud".format(absent))


def test_validator_main_node_render_status_ok_inbound_fails() -> None:
    # the code-eval's repro: agent(parse=false) → json_object(main) → render. The validator
    # RESETS the payload to {status,...}, so `{{status}}` renders fine and must NOT be blocked
    # (the false positive we fixed), while `{{raw}}` (reset away) correctly fails loud.
    def chain(tpl: str) -> Dict[str, Any]:
        return {"nodes": {"ask": {"type": "agent", "parse": False},
                          "chk": {"type": "json_object"},
                          "rep": {"type": "render", "template_text": tpl}},
                "graph": {"start": "a", "stages": {
                    "a": {"node": "ask", "then": "b"},
                    "b": {"node": "chk", "then": "c"},
                    "c": {"node": "rep"}}}}
    validate_pipeline(chain("Result: {{status}}"))   # provided by the validator → must pass
    try:
        validate_pipeline(chain("{{raw}}"))
    except ValueError as e:
        assert "raw" in str(e), str(e)
    else:
        raise AssertionError("render of reset-away {{raw}} after a validator must fail loud")


def test_stage_error_retries_is_a_known_key() -> None:
    """Regression (found with mailbox M9): build_graph reads `error_retries`
    (build.py) but the key was missing from _STAGE_KEYS — the documented
    transient-budget knob was falsely REJECTED at validation."""
    p = _valid_pipeline()
    p["graph"]["stages"]["s1"]["error_retries"] = 5
    validate_pipeline(p)


def test_min_success_rules() -> None:
    """M9a: `min_success` (k-of-n fanout completion) is fanout-only, an int,
    and 1 <= k <= len(fanout) — anything else is a config error, not a knob
    that silently never fires."""
    def fan(**stage_extra):
        return {"nodes": {"x": {"type": "transform", "target": "fn:m:f"}},
                "graph": {"start": "s1", "stages": {
                    "s1": {"node": "x", "fanout": ["x", "x2"], **stage_extra}}}}
    # note: fanout entries are roles; use the one node twice via alias roles
    ok = fan(min_success=1)
    ok["nodes"]["x2"] = {"type": "transform", "target": "fn:m:f"}
    validate_pipeline(ok)
    for bad, expect in [
        (fan(min_success=0), "min_success"),          # below 1
        (fan(min_success=3), "min_success"),          # above len(fanout)
        (fan(min_success="two"), "min_success"),      # not an int
    ]:
        bad["nodes"]["x2"] = {"type": "transform", "target": "fn:m:f"}
        try:
            validate_pipeline(bad)
            raise AssertionError("bad min_success must be rejected: " + expect)
        except ValueError as e:
            assert expect in str(e), str(e)
    # min_success without fanout is meaningless -> rejected
    p = _valid_pipeline()
    p["graph"]["stages"]["s1"]["min_success"] = 1
    try:
        validate_pipeline(p)
        raise AssertionError("min_success without fanout must be rejected")
    except ValueError as e:
        assert "fanout" in str(e), str(e)


def _branch_non_dict_cfg(branch_val: Any) -> Dict[str, Any]:
    return {"nodes": {"a": {"type": "agent", "parse": False}},
            "graph": {"start": "s1", "stages": {"s1": {"node": "a", "branch": branch_val}}}}


def test_branch_string_is_a_clean_structural_error() -> None:
    """A string `branch` must produce a named structural error, never AttributeError."""
    for val in ("oops", [], 42):
        try:
            validate_pipeline(_branch_non_dict_cfg(val))
            raise AssertionError("branch={!r} must be rejected".format(val))
        except ValueError as e:
            msg = str(e)
            assert "'s1'" in msg, msg
            assert "branch" in msg, msg
        except AttributeError as exc:
            raise AssertionError(
                "branch={!r} crashed with AttributeError instead of a clean error: {}".format(
                    val, exc))


def test_branch_non_dict_does_not_crash_dataflow() -> None:
    """dataflow._edges is documented NEVER raises — a non-dict branch must be tolerated."""
    from yaah.dataflow import _edges
    for val in ("oops", [], 42):
        # must not raise anything
        _edges({"s1": {"node": "a", "branch": val}})


def main() -> None:
    test_valid_root_passes()
    test_validator_main_node_render_status_ok_inbound_fails()
    test_parse_false_render_of_raw_is_accepted()
    test_parse_false_render_of_unprovided_key_still_fails_loud()
    test_parse_false_branch_after_validator_reset()
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
    test_dangling_default_provider_rejected()
    test_valid_default_provider_passes()
    test_dangling_default_prompt_source_rejected()
    test_default_without_a_map_is_not_newly_rejected()
    test_multiple_errors_gathered()
    test_pipeline_valid_passes()
    test_pipeline_typo_then_surfaces()
    test_pipeline_on_error_shape_is_hard_checked()
    test_pipeline_unknown_node_role()
    test_pipeline_typeless_node_names_stale_overlay()
    test_constraint_precedes_holds()
    test_constraint_precedes_catches_bypass()
    test_constraint_names_must_be_stages()
    test_constraint_unknown_key_and_shape()
    test_budget_node_timeout_exceeds_transport_window()
    test_budget_inproc_has_no_reply_window()
    test_budget_lease_horizon_must_fit_the_checkpoint_window()
    test_budget_fork_wait_smaller_than_branch_node_timeout()
    test_stage_error_retries_is_a_known_key()
    test_min_success_rules()
    test_branch_string_is_a_clean_structural_error()
    test_branch_non_dict_does_not_crash_dataflow()
    test_node_unknown_key_rejected()
    test_node_unknown_key_did_you_mean()
    test_node_key_legal_on_another_type_is_still_rejected()
    test_node_underscore_keys_are_legal_everywhere()
    test_node_common_keys_legal_on_every_type()
    test_custom_node_type_keys_are_not_checked()
    test_allow_unknown_node_keys_escape_hatch()
    test_node_key_table_covers_every_built_in_type()
    test_node_key_table_covers_every_spec_read_in_builders()
    test_shell_interpolate_from_key_never_produced_fails_at_load()
    test_shell_target_from_key_never_produced_fails_at_load()
    test_shell_reads_satisfied_upstream_pass()
    test_shell_placeholder_without_interpolate_from_reads_nothing()
    test_shell_declared_but_unused_interpolate_key_is_not_a_read()
    print("test_validate: PASS (58 scenarios)")


if __name__ == "__main__":
    main()
