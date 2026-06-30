"""lint_pipeline — advisory warnings over a VALID pipeline config (never raises).

The author-time linter: catches valid-but-RISKY shapes that otherwise bite deep in a
run. Each rule is traced to a real s_factory failure (see mailbox M5-r). Rule #1
(weak-output-schema) is the validation wall in lint form: a `parse:true` agent whose
`output_schema` requires keys but does not TYPE them lets a parseable-but-wrong output
pass `check_schema` and detonate stages later.

Run: cd yaah && PYTHONPATH=src python3 tests/test_lint_pipeline.py
"""
from __future__ import annotations

import io
import sys

from yaah.cli import _dispatch_validate
from yaah.validate import lint_pipeline


def _node(schema=None, parse=True, type_="agent"):
    n = {"type": type_}
    if parse is not None:
        n["parse"] = parse
    if schema is not None:
        n["output_schema"] = schema
    return n


def warns_on_required_only_schema() -> None:
    cfg = {"nodes": {"judge": _node({"required": ["verdict"]})}}
    w = lint_pipeline(cfg)
    assert any("judge" in m and "weak-output-schema" in m for m in w), w


def quiet_on_typed_properties() -> None:
    cfg = {"nodes": {"judge": _node(
        {"required": ["verdict"], "properties": {"verdict": {"enum": ["FIX", "SKIP"]}}})}}
    assert lint_pipeline(cfg) == []


def quiet_on_type_constrained() -> None:
    cfg = {"nodes": {"sum": _node(
        {"required": ["reason"], "properties": {"reason": {"type": "string"}}})}}
    assert lint_pipeline(cfg) == []


def quiet_on_parse_false() -> None:
    cfg = {"nodes": {"raw": _node({"required": ["verdict"]}, parse=False)}}
    assert lint_pipeline(cfg) == []


def quiet_without_output_schema() -> None:
    cfg = {"nodes": {"a": _node(None)}}
    assert lint_pipeline(cfg) == []


def quiet_on_non_agent() -> None:
    cfg = {"nodes": {"t": _node({"required": ["x"]}, type_="transform")}}
    assert lint_pipeline(cfg) == []


def partial_typing_still_warns_on_the_untyped_key() -> None:
    # verdict typed, confidence required-but-untyped -> warn, naming confidence
    cfg = {"nodes": {"j": _node({"required": ["verdict", "confidence"],
                                 "properties": {"verdict": {"enum": ["FIX"]}}})}}
    w = lint_pipeline(cfg)
    assert w, w
    assert "confidence" in w[0] and "untyped" in w[0], w[0]


def ignores_overlay_keys_and_non_dicts() -> None:
    cfg = {"nodes": {"_overlay": _node({"required": ["x"]}), "bad": "not-a-dict"}}
    assert lint_pipeline(cfg) == []


# ── the teeth: `yaah validate [--strict]` surfaces warnings / fails on them ──

def _run_validate(strict, schema):
    """Drive _dispatch_validate with an inline pipeline; capture (exit_code, out, err)."""
    pipeline = {"nodes": {"judge": {"type": "agent", "output_schema": schema}},
                "graph": {"start": "s1", "stages": {"s1": {"node": "judge"}}}}
    old_err, old_out = sys.stderr, sys.stdout
    sys.stderr, sys.stdout = io.StringIO(), io.StringIO()
    code = 0
    try:
        _dispatch_validate({"root": "t", "strict": strict}, {"pipeline": pipeline}, ".")
    except SystemExit as e:
        code = 0 if e.code is None else int(e.code)
    finally:
        err, out = sys.stderr.getvalue(), sys.stdout.getvalue()
        sys.stderr, sys.stdout = old_err, old_out
    return code, out, err


def teeth_default_warns_but_passes() -> None:
    code, out, err = _run_validate(False, {"required": ["verdict"]})
    assert code == 0, code
    assert "weak-output-schema" in err, err     # warning surfaced (stderr)
    assert "ok:" in out, out                     # still valid (stdout)


def teeth_strict_fails_with_exit_2() -> None:
    code, out, err = _run_validate(True, {"required": ["verdict"]})
    assert code == 2, code                       # distinct from hard-error exit
    assert "weak-output-schema" in err
    assert "ok:" not in out                       # did not pronounce ok


def teeth_strict_passes_on_typed_schema() -> None:
    code, out, err = _run_validate(
        True, {"required": ["verdict"], "properties": {"verdict": {"enum": ["FIX"]}}})
    assert code == 0, (code, err)
    assert "ok:" in out


# ── 1a contract completeness: branch on a key the agent doesn't DECLARE ──────

def _branch_cfg(on, schema=None, parse=True, node_type="agent"):
    node = {"type": node_type}
    if parse is not None:
        node["parse"] = parse
    if schema is not None:
        node["output_schema"] = schema
    return {"nodes": {"j": node},
            "graph": {"start": "s",
                      "stages": {"s": {"node": "j", "branch": {"on": on, "routes": {}}}}}}


def _has_branch_warn(cfg):
    return any("branch-key-unprovided" in m for m in lint_pipeline(cfg))


def warns_branch_key_not_provided() -> None:
    cfg = _branch_cfg("verdict", {"properties": {"other": {"type": "string"}}})
    w = lint_pipeline(cfg)
    assert any("branch-key-unprovided" in m and "verdict" in m for m in w), w


def quiet_branch_key_in_properties() -> None:
    assert not _has_branch_warn(_branch_cfg("verdict", {"properties": {"verdict": {"enum": ["FIX"]}}}))


def quiet_branch_key_in_required() -> None:
    assert not _has_branch_warn(_branch_cfg("verdict", {"required": ["verdict"]}))


def quiet_branch_key_carried() -> None:
    cfg = _branch_cfg("verdict", {"properties": {}})
    cfg["nodes"]["j"]["carry"] = ["verdict"]
    assert not _has_branch_warn(cfg)


def quiet_branch_key_sticky() -> None:
    cfg = _branch_cfg("verdict", {"properties": {}})
    cfg["graph"]["sticky"] = ["verdict"]
    assert not _has_branch_warn(cfg)


def quiet_branch_on_raw() -> None:
    assert not _has_branch_warn(_branch_cfg("raw", {"properties": {}}))


def quiet_branch_no_schema_or_non_agent() -> None:
    # parse:true with NO output_schema -> parsed keys unknown -> incomplete -> skip (the
    # weak-output-schema lint nudges declaring a schema first); a transform's output at the
    # start of the graph is also incomplete.
    assert not _has_branch_warn(_branch_cfg("verdict", schema=None))
    assert not _has_branch_warn(_branch_cfg("verdict", {"properties": {}}, node_type="transform"))


def warns_branch_parse_false_provides_only_raw() -> None:
    # a parse:false agent provides exactly {raw} -> branching on `verdict` is a real gap the
    # single-hop 1a lint skipped; the broad lint now flags it (the producing-side hard error
    # only covers agent -> SEPARATE branch stage, not a same-stage branch).
    assert _has_branch_warn(_branch_cfg("verdict", {"properties": {}}, parse=False))


# ── 1a-render contract completeness: render needs a key the agent doesn't DECLARE ─

def _render_cfg(template_text=None, template_file=None, schema=None, parse=True,
                node_type="agent", carry=None, cwd_from=None, sticky=None,
                allow_unfilled=False, extra_preds=False, via_branch=False):
    agent = {"type": node_type}
    if parse is not None:
        agent["parse"] = parse
    if schema is not None:
        agent["output_schema"] = schema
    if carry is not None:
        agent["carry"] = carry
    if cwd_from is not None:
        agent["cwd_from"] = cwd_from
    render = {"type": "render", "allow_unfilled": allow_unfilled}
    if template_text is not None:
        render["template_text"] = template_text
    if template_file is not None:
        render["template_file"] = template_file
    stages = {"a": {"node": "agent", "then": "r"}, "r": {"node": "render"}}
    graph = {"start": "a", "stages": stages}
    if sticky is not None:
        graph["sticky"] = sticky
    if via_branch:  # a second stage ALSO routes to r -> multi-path -> not statically sound
        stages["b"] = {"node": "agent", "branch": {"on": "x", "routes": {"y": "r"}}}
    if extra_preds:  # two `then` predecessors -> provides aren't a single known set
        stages["a2"] = {"node": "agent", "then": "r"}
    return {"nodes": {"agent": agent, "render": render}, "graph": graph}


def _has_render_warn(cfg, base_path=None):
    return any("render-key-unprovided" in m for m in lint_pipeline(cfg, base_path))


def warns_render_key_not_provided() -> None:
    cfg = _render_cfg("Report: {{verdict}}", schema={"properties": {"other": {"type": "string"}}})
    w = lint_pipeline(cfg)
    assert any("render-key-unprovided" in m and "verdict" in m for m in w), w


def warns_render_names_only_missing_keys() -> None:
    # {{a}} is provided and {{a}} repeats; only {{b}} is unprovided -> names exactly ['b']
    cfg = _render_cfg("{{a}} {{b}} {{a}}", schema={"properties": {"a": {"type": "string"}}})
    w = [m for m in lint_pipeline(cfg) if "render-key-unprovided" in m]
    assert w and "needs ['b']" in w[0], w


def render_warning_is_contract_nudge_not_crash_prediction() -> None:
    """Falsifies the old 'zero false positives / FAILS at runtime' framing. `check_schema`
    does NOT enforce additionalProperties (jsonschema.py), so an agent declaring only {a}
    may still EMIT {a, b} and a {{b}} render would then SUCCEED. The lint counts only
    DECLARED keys and warns anyway — by design (flag the undeclared dependency) — but the
    wording must be HONEST: conditional ('on any run where the agent omits them'), not a
    certain crash. This pins the honest wording so it isn't silently re-broken."""
    cfg = _render_cfg("{{a}} {{b}}", schema={"properties": {"a": {"type": "string"}}})
    w = [m for m in lint_pipeline(cfg) if "render-key-unprovided" in m]
    assert w, w
    assert "depends on undeclared output" in w[0], w[0]   # contract gap, not certain crash
    assert "where they're absent" in w[0], w[0]           # conditional framing
    assert "FAILS at runtime" not in w[0], w[0]           # the old overclaim is gone


def quiet_render_key_in_properties() -> None:
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"properties": {"verdict": {"type": "string"}}}))


def quiet_render_key_in_required() -> None:
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"required": ["verdict"]}))


def quiet_render_key_raw() -> None:
    assert not _has_render_warn(_render_cfg("{{raw}}", schema={"properties": {}}))


def quiet_render_key_carried() -> None:
    assert not _has_render_warn(_render_cfg("{{ctx}}", schema={"properties": {}}, carry=["ctx"]))


def quiet_render_key_sticky() -> None:
    assert not _has_render_warn(_render_cfg("{{run_id}}", schema={"properties": {}}, sticky=["run_id"]))


def quiet_render_key_cwd_from() -> None:
    assert not _has_render_warn(_render_cfg("{{workdir}}", schema={"properties": {}}, cwd_from="workdir"))


def quiet_render_allow_unfilled() -> None:
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"properties": {}}, allow_unfilled=True))


def quiet_render_pred_no_schema_or_non_agent() -> None:
    # parse:true with no output_schema -> incomplete -> skip; a transform predecessor at the
    # start of the graph is incomplete too. (parse:false agent -> render is caught by the
    # data-flow-contract HARD ERROR in validate_pipeline, which runs before the lint.)
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema=None))
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"properties": {}}, node_type="transform"))


def quiet_render_template_file_without_base() -> None:
    # a template_file but no base_path to resolve it against -> skip, no crash, no warning
    assert not _has_render_warn(_render_cfg(template_file="t.html", schema={"properties": {}}))


def warns_render_template_file_read_from_base() -> None:
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "out.html"), "w") as f:
            f.write("<h1>{{verdict}}</h1>")
        cfg = _render_cfg(template_file="out.html", schema={"properties": {"other": {"type": "string"}}})
        assert _has_render_warn(cfg, base_path=d)


def quiet_render_template_file_unreadable() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg = _render_cfg(template_file="missing.html", schema={"properties": {}})
        assert not _has_render_warn(cfg, base_path=d)


def render_template_file_resolves_against_root_dir() -> None:
    """Regression: the render lint resolves `template_file` against the ROOT config
    dir (what the runtime passes as base_dir), NOT the pipeline file's dir. A pipeline
    in a SUBDIR with the template next to the ROOT proves it — the old code read
    `dirname(pipeline_path)` and would have missed the file (and silently not warned)."""
    import io
    import json
    import os
    import sys
    import tempfile
    with tempfile.TemporaryDirectory() as base:
        os.makedirs(os.path.join(base, "sub"))
        pipeline = {
            "nodes": {
                "a": {"type": "agent",
                      "output_schema": {"properties": {"other": {"type": "string"}}}},
                "r": {"type": "render", "template_file": "report.html"},
            },
            "graph": {"start": "s1", "stages": {"s1": {"node": "a", "then": "s2"},
                                                "s2": {"node": "r"}}},
        }
        with open(os.path.join(base, "sub", "pipe.json"), "w") as f:
            json.dump(pipeline, f)
        with open(os.path.join(base, "report.html"), "w") as f:  # next to ROOT, not pipeline
            f.write("<h1>{{verdict}}</h1>")
        old_err = sys.stderr
        sys.stderr = io.StringIO()
        try:
            _dispatch_validate({"root": "r.local.json", "strict": False},
                               {"pipeline": "sub/pipe.json"}, base)
        except SystemExit:
            pass
        finally:
            err = sys.stderr.getvalue()
            sys.stderr = old_err
        assert "render-key-unprovided" in err and "verdict" in err, err


# ── broad dataflow (multi-hop / multi-path): ADR-0005 slice B ────────────────

def _has(cfg, tag, base_path=None):
    return any(tag in m for m in lint_pipeline(cfg, base_path))


def _fork_to_render(a1_schema, a2_schema, template="{{verdict}}"):
    """A reachable two-path graph: a pure `fork` start splits to two agent stages that
    both `then` the same render. Both paths run, so the render sees the INTERSECTION."""
    return {"nodes": {
        "a1": {"type": "agent", "output_schema": a1_schema},
        "a2": {"type": "agent", "output_schema": a2_schema},
        "r": {"type": "render", "template_text": template}},
        "graph": {"start": "s0", "stages": {
            "s0": {"fork": ["s1", "s2"]},
            "s1": {"node": "a1", "then": "rr"},
            "s2": {"node": "a2", "then": "rr"},
            "rr": {"node": "r"}}}}


def warns_render_multipath_one_path_missing() -> None:
    # one reachable path provides verdict, the other doesn't -> intersection drops it -> warn
    cfg = _fork_to_render({"required": ["verdict"]}, {"properties": {"other": {"type": "string"}}})
    assert _has(cfg, "render-key-unprovided")


def quiet_render_multipath_both_provide() -> None:
    # both reachable paths provide verdict -> intersection keeps it -> quiet
    cfg = _fork_to_render({"required": ["verdict"]},
                          {"properties": {"verdict": {"type": "string"}}})
    assert not _has(cfg, "render-key-unprovided")


def quiet_render_unreachable_pred_not_intersected() -> None:
    # a predecessor unreachable from start must NOT be intersected (it never runs); else the
    # single reachable path that DOES provide verdict would be falsely warned.
    cfg = {"nodes": {
        "good": {"type": "agent", "output_schema": {"required": ["verdict"]}},
        "ghost": {"type": "agent", "output_schema": {"properties": {"x": {"type": "string"}}}},
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "good", "then": "rr"},
            "dead": {"node": "ghost", "then": "rr"},   # nothing routes to `dead`
            "rr": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided")


def quiet_render_through_args_transform_preserves() -> None:
    # agent declares verdict -> args-transform PRESERVES inbound -> render sees verdict -> quiet
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["verdict"]}},
        "t": {"type": "transform", "target": "fn:m:f"},   # call defaults "args" -> preserves
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided")


def quiet_render_needs_args_transform_into() -> None:
    # the args-transform nests its result under `into: summary` -> render needs summary -> quiet
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["verdict"]}},
        "t": {"type": "transform", "target": "fn:m:f", "into": "summary"},
        "r": {"type": "render", "template_text": "{{summary}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided")


def warns_render_through_args_transform_missing() -> None:
    # nobody provides verdict along the chain -> warn even multi-hop
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"properties": {"foo": {"type": "string"}}}},
        "t": {"type": "transform", "target": "fn:m:f"},
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert _has(cfg, "render-key-unprovided")


def quiet_render_through_declared_envelope_transform() -> None:
    # parse:false agent -> envelope-transform that DECLARES provides:[verdict] -> render -> quiet
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "t": {"type": "transform", "target": "fn:m:f", "call": "envelope", "provides": ["verdict"]},
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided")


def declared_envelope_transform_preserves_inbound() -> None:
    # PRESERVE + ADD, not reset: an envelope-transform whose fn does `{**payload, "c": ...}`
    # declares only the ADDED key `c`, yet a downstream render of an INBOUND key (`a`) must NOT
    # warn — inbound survives. (The old reset model would false-positive on `a`.) Mirrors the
    # real arch-drift transforms (`return {**envelope.payload, "snapshot": ...}`).
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["a", "b"]}},
        "t": {"type": "transform", "target": "fn:m:f", "call": "envelope", "provides": ["c"]},
        "r": {"type": "render", "template_text": "{{a}} {{c}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)


def undeclared_envelope_transform_warns_and_taints() -> None:
    # an UNDECLARED envelope-transform: warn on IT (actionable), and SKIP the downstream
    # render (its provides are unknown -> tainted -> no false render warning).
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "t": {"type": "transform", "target": "fn:m:f", "call": "envelope"},
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    w = lint_pipeline(cfg)
    assert any("transform-provides-undeclared" in m and "s2" in m for m in w), w
    assert not any("render-key-unprovided" in m for m in w), w   # downstream skipped, not falsely warned


def render_warnings_are_actionable() -> None:
    # every requires-warning must name the concrete fix (human/llm-applicable) — ADR-0005 §6
    cfg = _render_cfg("{{verdict}}", schema={"properties": {"other": {"type": "string"}}})
    w = [m for m in lint_pipeline(cfg) if "render-key-unprovided" in m]
    assert w and "Declare" in w[0] and "output_schema" in w[0] and "sticky" in w[0], w[0]


def _chain(*node_pairs, template, sticky=None):
    """A linear `then` chain: each (role, node) becomes stage s1, s2, ... ending in a render
    of `template`. Returns the cfg."""
    nodes = {role: node for role, node in node_pairs}
    nodes["r"] = {"type": "render", "template_text": template}
    names = ["s{}".format(i + 1) for i in range(len(node_pairs))]
    stages = {}
    for i, (role, _n) in enumerate(node_pairs):
        stages[names[i]] = {"node": role, "then": (names[i + 1] if i + 1 < len(names) else "rr")}
    stages["rr"] = {"node": "r"}
    graph = {"start": names[0], "stages": stages}
    if sticky:
        graph["sticky"] = sticky
    return {"nodes": nodes, "graph": graph}


def quiet_render_after_gate_provides_decision() -> None:
    cfg = _chain(("a", {"type": "agent", "output_schema": {"required": ["v"]}}),
                 ("g", {"type": "human_gate"}), template="{{decision}} {{v}}")
    assert not _has(cfg, "render-key-unprovided")


def quiet_render_after_get_into() -> None:
    cfg = _chain(("a", {"type": "agent", "output_schema": {"required": ["v"]}}),
                 ("g", {"type": "get", "into": "fetched"}), template="{{fetched}}")
    assert not _has(cfg, "render-key-unprovided")


def quiet_render_sticky_survives_multihop() -> None:
    cfg = _chain(("a", {"type": "agent", "output_schema": {"required": ["v"]}}),
                 ("t", {"type": "transform", "target": "fn:m:f"}),
                 template="{{run_id}}", sticky=["run_id"])
    assert not _has(cfg, "render-key-unprovided")


def loop_converges_and_keeps_key() -> None:
    # a self-loop (retry) must reach a fixpoint (no hang) and not false-warn on a key the
    # loop body re-provides each turn.
    cfg = {"nodes": {
        "w": {"type": "agent", "output_schema": {"required": ["v"]}},
        "r": {"type": "render", "template_text": "{{v}}"}},
        "graph": {"start": "s2", "stages": {
            "s2": {"node": "w", "branch": {"on": "v", "routes": {"again": "s2"}, "default": "s3"}},
            "s3": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided") and not _has(cfg, "branch-key-unprovided")


def quiet_render_parse_false_agent_forwards_cwd_from() -> None:
    # opus-review regression: carry_cwd forwards the `cwd_from` key onto EVERY agent reply
    # (agent.py:342, before the parse branch), so a parse:false repo-bound agent DOES provide
    # it — a downstream render needing it must NOT be warned (the transfer once omitted it).
    cfg = {"nodes": {
        "wt": {"type": "worktree", "provides": ["workdir"]},
        "a": {"type": "agent", "parse": False, "cwd_from": "workdir"},
        "t": {"type": "transform", "target": "fn:m:f"},
        "r": {"type": "render", "template_text": "{{workdir}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "wt", "then": "s2"},
            "s2": {"node": "a", "then": "s3"},
            "s3": {"node": "t", "then": "s4"},
            "s4": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided")


def lint_never_raises_on_malformed_output_schema() -> None:
    # opus-review: lint_pipeline must NEVER raise. A non-dict output_schema + a declared
    # `provides` used to crash `_agent_provides_keys` with AttributeError. (validate_pipeline
    # may reject it first in production, but the lint must be independently safe.)
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": ["not", "a", "dict"], "provides": ["v"]},
        "r": {"type": "render", "template_text": "{{v}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "r"}}}}
    lint_pipeline(cfg)   # must not raise


def opaque_shell_reset_skips_downstream_unless_declared() -> None:
    # a shell REPLACES the payload (N6): undeclared -> downstream render skipped (no warn);
    # declaring `provides` re-enables the check (and then a missing key warns).
    base = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["v"]}},
        "sh": {"type": "shell"},
        "r": {"type": "render", "template_text": "{{v}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "sh", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has(base, "render-key-unprovided")   # undeclared shell -> skipped, no false warn
    base["nodes"]["sh"]["provides"] = ["other"]      # declares it provides `other`, not `v`
    assert _has(base, "render-key-unprovided")        # now v is provably absent -> warn


def main() -> None:
    warns_on_required_only_schema()
    quiet_on_typed_properties()
    quiet_on_type_constrained()
    quiet_on_parse_false()
    quiet_without_output_schema()
    quiet_on_non_agent()
    partial_typing_still_warns_on_the_untyped_key()
    ignores_overlay_keys_and_non_dicts()
    warns_branch_key_not_provided()
    quiet_branch_key_in_properties()
    quiet_branch_key_in_required()
    quiet_branch_key_carried()
    quiet_branch_key_sticky()
    quiet_branch_on_raw()
    quiet_branch_no_schema_or_non_agent()
    warns_branch_parse_false_provides_only_raw()
    teeth_default_warns_but_passes()
    teeth_strict_fails_with_exit_2()
    teeth_strict_passes_on_typed_schema()
    warns_render_key_not_provided()
    warns_render_names_only_missing_keys()
    render_warning_is_contract_nudge_not_crash_prediction()
    quiet_render_key_in_properties()
    quiet_render_key_in_required()
    quiet_render_key_raw()
    quiet_render_key_carried()
    quiet_render_key_sticky()
    quiet_render_key_cwd_from()
    quiet_render_allow_unfilled()
    quiet_render_pred_no_schema_or_non_agent()
    quiet_render_template_file_without_base()
    warns_render_template_file_read_from_base()
    quiet_render_template_file_unreadable()
    render_template_file_resolves_against_root_dir()
    warns_render_multipath_one_path_missing()
    quiet_render_multipath_both_provide()
    quiet_render_unreachable_pred_not_intersected()
    quiet_render_through_args_transform_preserves()
    quiet_render_needs_args_transform_into()
    warns_render_through_args_transform_missing()
    quiet_render_through_declared_envelope_transform()
    declared_envelope_transform_preserves_inbound()
    undeclared_envelope_transform_warns_and_taints()
    render_warnings_are_actionable()
    quiet_render_after_gate_provides_decision()
    quiet_render_after_get_into()
    quiet_render_sticky_survives_multihop()
    loop_converges_and_keeps_key()
    quiet_render_parse_false_agent_forwards_cwd_from()
    lint_never_raises_on_malformed_output_schema()
    opaque_shell_reset_skips_downstream_unless_declared()
    print("ok")


if __name__ == "__main__":
    main()
