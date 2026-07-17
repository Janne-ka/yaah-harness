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
from yaah.validate import lint_pipeline, validate_pipeline


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


def fails_loud_branch_parse_false_provides_only_raw() -> None:
    # a parse:false agent provides exactly {raw} — a CLOSED (runtime-exact) set. Branching on
    # `verdict` reads a PROVABLY-ABSENT key, so it is a hard ERROR now (fail-loud), not an
    # advisory warning (ADR-0006 §D5 broadening — closed-path miss → validate rejects at load).
    cfg = _branch_cfg("verdict", {"properties": {}}, parse=False)
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "verdict" in str(e) and "branch-key-absent" in str(e), str(e)
        return
    raise AssertionError("parse=false branch on a provably-absent key must fail loud")


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


def placeholder_regex_is_single_source() -> None:
    # The lint extracts a render's {{keys}} with the SAME regex the render node ACTUALLY fills —
    # if they diverged, the lint would reason about a different key set than runtime and the
    # silent-misroute class reopens. Since ADR-0006 slice B5 there is ONE copy in `templating`;
    # guard that the lint and the runtime both use that exact object (not a re-introduced copy).
    from yaah.templating import PLACEHOLDER, fill
    from yaah.node_contract import render_consumes
    from yaah.nodes.render_node import _fill as render_fill
    # the lint's render-consume extraction (now node-owned, ADR-0006 symmetry) uses the SAME
    # shared regex the runtime fills with — proven behaviourally: same keys out.
    tpl = "{{a}} and {{ b }} but not {c}"
    assert render_consumes({"template_text": tpl}, None) == frozenset(PLACEHOLDER.findall(tpl))
    assert render_fill is fill, "the render node must use the shared templating.fill"


def opaque_nodes_provide_their_real_keys() -> None:
    # shell/worktree REPLACE the payload (N6) but emit a KNOWN, engine-defined set, so the lint
    # models them EXACTLY: a consumer of a guaranteed key is fine; a consumer of a key the node
    # does NOT provide is correctly flagged (fail-loud on accurate ground). agent_loop PRESERVES
    # inbound and adds {answer,turns,outcome}, so it is not a reset at all.
    def chain(node, template):
        return {"nodes": {
            "a": {"type": "agent", "output_schema": {"required": ["v"]}},
            "x": node,
            "r": {"type": "render", "template_text": template}},
            "graph": {"start": "s1", "stages": {
                "s1": {"node": "a", "then": "s2"},
                "s2": {"node": "x", "then": "s3"},
                "s3": {"node": "r"}}}}
    # shell: emits exit_code/ok/stdout_tail; drops inbound v unless carried
    assert not _has(chain({"type": "shell"}, "{{exit_code}}"), "render-key-unprovided")
    assert _has(chain({"type": "shell"}, "{{v}}"), "render-key-unprovided")           # v dropped
    assert not _has(chain({"type": "shell", "carry": ["v"]}, "{{v}}"), "render-key-unprovided")
    assert not _has(chain({"type": "shell", "provides": ["custom"]}, "{{custom}}"),
                    "render-key-unprovided")                                          # author-declared
    # worktree: add (default) → workdir/branch/repo/base; remove → removed/ok
    assert not _has(chain({"type": "worktree"}, "{{workdir}}"), "render-key-unprovided")
    # worktree is a CLOSED reset — a render of an add-only key after `remove` is now FAIL-LOUD
    # (a hard ERROR), not an advisory warning (ADR-0006 §D5 broadening).
    try:
        validate_pipeline(chain({"type": "worktree", "op": "remove"}, "{{workdir}}"))
        raise AssertionError("worktree-remove render of add-only {{workdir}} must fail loud")
    except ValueError as e:
        assert "workdir" in str(e) and "render-key-absent" in str(e), str(e)
    assert not _has(chain({"type": "worktree", "op": "remove"}, "{{removed}}"), "render-key-unprovided")
    # agent_loop preserves inbound v and adds answer
    assert not _has(chain({"type": "agent_loop"}, "{{v}} {{answer}}"), "render-key-unprovided")


def quiet_render_default_into_for_get_and_post() -> None:
    # ADR-0006 eval fix: get/post builder defaults are `into="data"` / `"stored"`, NOT "result".
    # dataflow once modeled "result" for both, false-warning a downstream {{data}} / {{stored}}.
    for role, node, key in (("g", {"type": "get"}, "data"), ("p", {"type": "post"}, "stored")):
        cfg = _chain(("a", {"type": "agent", "output_schema": {"required": ["v"]}}),
                     (role, node), template="{{" + key + "}}")
        assert not _has(cfg, "render-key-unprovided"), key


def custom_node_type_is_opaque_not_false_positive() -> None:
    # ADR-0006 soundness fix: a custom register()'d type is unknown to the lint → it must
    # resolve to OPAQUE (downstream uncheckable), NOT the old PRESERVE default that kept
    # completeness and FALSE-POSITIVEd a downstream read of a key the custom node adds. Under
    # the old code this warned; it must not now.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "x": {"type": "my_custom_node"},
        "r": {"type": "render", "template_text": "{{custom_key}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "x", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has(cfg, "render-key-unprovided")


# ── human seams are OPEN merges: resume MERGES the human's whole reply ───────
# harness._merge_decision folds the human's ENTIRE decision payload onto the pending
# payload — the human can add ARBITRARY keys. So downstream of a human seam (a
# `human_gate` node, or a stage with `escalate: "human"`) the payload is NOT provably
# closed, and a "provably absent" hard ERROR there would false-positive on a working
# pipeline. The contract-gap WARNING must survive (declare the key to silence it).


def gate_does_not_prove_absence_of_human_supplied_keys() -> None:
    # parse:false agent (closed {raw}) -> gate -> render of a key ONLY the human's
    # decision could supply. The old preserve("decision") gate contract carried the
    # closed proof through the merge and validate_pipeline hard-rejected this
    # WORKING pipeline with render-key-absent.
    cfg = _chain(("a", {"type": "agent", "parse": False}),
                 ("g", {"type": "human_gate"}), template="{{decision}} {{notes}}")
    validate_pipeline(cfg)   # must NOT raise: the human can supply `notes` at the gate
    # the contract gap is still SURFACED (complete survives the gate) as a warning...
    w = [m for m in lint_pipeline(cfg) if "render-key-unprovided" in m]
    assert w and "notes" in w[0], lint_pipeline(cfg)
    # ...and declaring the key on the gate (inline `provides`) silences it.
    cfg["nodes"]["g"]["provides"] = ["notes"]
    assert not _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)


def gate_still_provides_decision_on_a_closed_path() -> None:
    # the sound half stays sound: the gate still PROVIDES `decision` (and keeps
    # inbound keys), so a render of engine-guaranteed keys is quiet — no warn, no error.
    cfg = _chain(("a", {"type": "agent", "parse": False}),
                 ("g", {"type": "human_gate"}), template="{{decision}} {{raw}}")
    validate_pipeline(cfg)
    assert not _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)


def gate_branch_on_human_supplied_key_not_hard_failed() -> None:
    # branch.on reads the gate's OUTPUT — the merged payload. A human-supplied route
    # key is not provably absent (no hard error), but the undeclared dependency warns.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "g": {"type": "human_gate"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "g", "branch": {"on": "approved", "routes": {}}}}}}
    validate_pipeline(cfg)   # must NOT raise branch-key-absent
    assert _has(cfg, "branch-key-unprovided"), lint_pipeline(cfg)


def provable_absence_before_the_gate_still_fails_loud() -> None:
    # don't over-widen: the merge happens AT the gate. Upstream of it the closed
    # proof stands — a render BEFORE the gate reading a human-only key fails loud.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "r": {"type": "render", "template_text": "{{notes}}"},
                     "g": {"type": "human_gate"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "r", "then": "s3"},
               "s3": {"node": "g"}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "notes" in str(e) and "render-key-absent" in str(e), str(e)
        return
    raise AssertionError("a provable absence upstream of the gate must still fail loud")


def escalate_human_stage_does_not_prove_absence_downstream() -> None:
    # same seam, other door: `escalate: "human"` resumes through the SAME
    # _merge_decision, so `closed` must not survive the stage — but ONLY when the
    # stage actually declares the escalation (no over-widening of its sibling).
    def cfg(escalate):
        s1 = {"node": "a", "then": "s2"}
        if escalate:
            s1["escalate"] = "human"
        return {"nodes": {"a": {"type": "agent", "parse": False},
                          "r": {"type": "render", "template_text": "{{notes}}"}},
                "graph": {"start": "s1", "stages": {"s1": s1, "s2": {"node": "r"}}}}
    validate_pipeline(cfg(True))    # not provable: the human may supply `notes` on resume
    assert _has(cfg(True), "render-key-unprovided"), lint_pipeline(cfg(True))
    try:
        validate_pipeline(cfg(False))
        raise AssertionError("without escalate the absence IS provable — must fail loud")
    except ValueError as e:
        assert "render-key-absent" in str(e), str(e)


# ── ADR-0010 attach: contract semantics through the REAL validate/lint path ──────────────
# A parse:false agent is CLOSED {raw} — but `attach: [fn:…]` merges attacher-supplied keys
# (fn: code, unenumerable statically) onto the output, so the set is no longer runtime-exact:
# the contract drops `closed` (complete=True). Declaring the keys in the agent's `provides:`
# makes them visible to the data-flow lint (resolve_contract augments provides uniformly).
# These drive the WHOLE pipeline through validate_pipeline/lint_pipeline, not a unit contract.


def _attach_pipeline(render_key, *, provides=None, attach=("fn:m:U",), parse=False):
    agent = {"type": "agent", "parse": parse}
    if attach is not None:
        agent["attach"] = list(attach) if isinstance(attach, (list, tuple)) else attach
    if provides is not None:
        agent["provides"] = provides
    return {"nodes": {"a": agent, "r": {"type": "render",
                                        "template_text": "Report: {{" + render_key + "}}"}},
            "graph": {"start": "s1", "stages": {"s1": {"node": "a", "then": "s2"},
                                                "s2": {"node": "r"}}}}


def _validate_exit(pipeline, strict):
    """Drive `yaah validate [--strict]` on an inline pipeline; return (exit_code, out, err).
    Mirrors _run_validate but takes an arbitrary pipeline so the attach cases run the same
    CLI teeth the CI gate uses (a warning under --strict must be exit 2)."""
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


def attach_declared_provides_render_on_declared_key_validates_clean() -> None:
    # (a) parse:false + attach + provides:[usage], render {{usage}} → NO warnings, validates
    # clean, and --strict PASSES (exit 0). The declared attacher key is visible to the lint.
    cfg = _attach_pipeline("usage", provides=["usage"])
    validate_pipeline(cfg)                                    # must NOT raise
    assert not _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)
    assert not _has(cfg, "attach-undeclared-keys"), lint_pipeline(cfg)
    code, out, _ = _validate_exit(cfg, strict=True)
    assert code == 0 and "ok:" in out, (code, out)


def attach_declared_provides_render_typo_warns_and_blocks_strict() -> None:
    # (b) same, but render reads {{usgae}} (a typo) → a WARNING (not a hard error — an attacher
    # COULD emit any key, so absence is unprovable), and --strict FAILS with exit 2.
    cfg = _attach_pipeline("usgae", provides=["usage"])
    validate_pipeline(cfg)                                    # must NOT raise (no hard error)
    w = [m for m in lint_pipeline(cfg) if "render-key-unprovided" in m]
    assert w and "usgae" in w[0], lint_pipeline(cfg)
    code, _, err = _validate_exit(cfg, strict=True)
    assert code == 2, code                                   # warning ⇒ CI gate fails
    assert "render-key-unprovided" in err, err


def attach_without_provides_fires_nudge_lint() -> None:
    # (c) attach present, NO provides declared → the new proactive nudge fires on the NODE
    # (fires even with no downstream consumer): attacher keys are invisible to the lint.
    cfg = _attach_pipeline("raw")   # render reads {{raw}} (provided) so no render warning
    validate_pipeline(cfg)
    w = [m for m in lint_pipeline(cfg) if "attach-undeclared-keys" in m]
    assert w and "'a'" in w[0], lint_pipeline(cfg)


def attach_with_provides_silences_the_nudge() -> None:
    # declaring the keys silences the nudge (the whole point of the remedy it names).
    cfg = _attach_pipeline("raw", provides=["usage"])
    assert not _has(cfg, "attach-undeclared-keys"), lint_pipeline(cfg)


def attach_empty_provides_is_the_explicit_optout() -> None:
    # the contrarian's "intentionally open-ended attach" author: `provides: []` declares "I add
    # no lint-visible keys" and silences the nudge (any declared provides list opts out).
    cfg = _attach_pipeline("raw", provides=[])
    assert not _has(cfg, "attach-undeclared-keys"), lint_pipeline(cfg)


def malformed_attach_non_list_is_hard_error() -> None:
    # (d) a bare-string attach is a validate-time hard ERROR (pre-fix it silently dropped
    # `closed` and NOTHING linted it — it exploded later at build after paid model calls).
    cfg = _attach_pipeline("raw", attach="fn:m:U")
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "'a'" in str(e) and "attach" in str(e), str(e)
        return
    raise AssertionError("a non-list attach must be rejected at validate")


def malformed_attach_non_string_item_is_hard_error() -> None:
    cfg = _attach_pipeline("raw", attach=[123])
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "'a'" in str(e) and "attach" in str(e), str(e)
        return
    raise AssertionError("a non-string attach item must be rejected at validate")


def empty_attach_list_preserves_closed_hard_error() -> None:
    # (e) attach:[] attaches nothing → the parse:false agent stays CLOSED {raw}, so a render of
    # a provably-absent key is still a hard ERROR (the fail-loud floor must not erode).
    cfg = _attach_pipeline("verdict", attach=[])
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "verdict" in str(e) and "render-key-absent" in str(e), str(e)
        return
    raise AssertionError("empty attach must keep the closed-path hard error")


def parse_true_attach_behavior_unchanged() -> None:
    # (f) parse:true + attach + output_schema: render on a SCHEMA key is clean; render on an
    # attach key warns (complete, not closed) — unchanged by this fix, and NO nudge (nudge is
    # parse:false-scoped: parse:true is never closed, so attach isn't the sole loosener).
    clean = _attach_pipeline("verdict", provides=None, parse=True)
    clean["nodes"]["a"]["output_schema"] = {"properties": {"verdict": {"type": "string"}}}
    validate_pipeline(clean)
    assert not _has(clean, "render-key-unprovided"), lint_pipeline(clean)
    assert not _has(clean, "attach-undeclared-keys"), lint_pipeline(clean)
    warn = _attach_pipeline("usage", provides=None, parse=True)
    warn["nodes"]["a"]["output_schema"] = {"properties": {"verdict": {"type": "string"}}}
    validate_pipeline(warn)                                   # complete, not closed → no error
    assert _has(warn, "render-key-unprovided"), lint_pipeline(warn)


def attach_remedy_text_names_provides_first() -> None:
    # finding #3: the soft render remedy must lead with inline `provides:` (works for BOTH parse
    # modes — the attach case has no output_schema to point at), while still naming output_schema
    # (for parsed keys) and sticky. Guards the remedy stays accurate for attach agents.
    cfg = _attach_pipeline("usgae", provides=["usage"])
    w = [m for m in lint_pipeline(cfg) if "render-key-unprovided" in m][0]
    assert "provides" in w and "output_schema" in w and "sticky" in w, w
    assert w.index("provides") < w.index("output_schema"), w   # provides FIRST


# ── #5: a >=2-outcome gate whose decision nothing branches on (silently ignores rejection) ──

def _gate_cfg(gate_node, *, branch_on_decision=False):
    stage = {"node": "g"}
    nodes = {"g": gate_node}
    if branch_on_decision:
        stage["branch"] = {"on": "decision", "routes": {"approve": "done"}, "default": "done"}
        nodes["d"] = {"type": "agent", "parse": False}
        stages = {"s": stage, "done": {"node": "d"}}
    else:
        stages = {"s": stage}
    return {"nodes": nodes, "graph": {"start": "s", "stages": stages}}


def warns_gate_two_outcomes_no_branch() -> None:
    assert _has(_gate_cfg({"type": "human_gate", "form": "approve_or_revise"}),
                "gate-decision-ignored")


def quiet_gate_two_outcomes_with_branch_on_decision() -> None:
    assert not _has(_gate_cfg({"type": "human_gate", "form": "approve_or_revise"},
                              branch_on_decision=True), "gate-decision-ignored")


def quiet_gate_single_outcome_approve() -> None:
    assert not _has(_gate_cfg({"type": "human_gate", "form": "approve"}), "gate-decision-ignored")


def quiet_gate_free_text_has_no_decision() -> None:
    assert not _has(_gate_cfg({"type": "human_gate", "form": "free_text"}), "gate-decision-ignored")


def warns_gate_json_schema_two_outcomes() -> None:
    node = {"type": "human_gate", "form": "json_schema",
            "decision_schema": {"type": "object", "required": ["decision"],
                                "properties": {"decision": {"enum": ["ship", "block"]}}}}
    assert _has(_gate_cfg(node), "gate-decision-ignored")


def quiet_gate_json_schema_single_outcome() -> None:
    node = {"type": "human_gate", "form": "json_schema",
            "decision_schema": {"type": "object", "required": ["decision"],
                                "properties": {"decision": {"enum": ["ack"]}}}}
    assert not _has(_gate_cfg(node), "gate-decision-ignored")


# ── parallel-shape stages: fanout / fork / fanin model the ENGINE's merged payload ──
# Runtime truth (harness._produce_fanout): a fanout stage's output is
# `dict(input.payload)` updated with exactly `results`, `roles`, `failed_roles` —
# inbound keys survive, role outputs stay NESTED under `results`. A role that
# replies AWAIT (a human_gate) parks the whole stage with `last_output=None`, so
# resume REPLACES the payload with the human's response — `closed` cannot survive
# a fanout whose role may suspend. Fork/fanin hand forward a fan-in REDUCE.


def fanout_engine_merged_keys_not_hard_failed() -> None:
    # parse:false agent (closed {raw}) feeds a fanout of two agent roles; the render
    # reads the engine-merged keys AND an inbound key. Runtime: present on EVERY
    # merged pass — must be fully quiet (no error, no warning). Pre-fix the stage
    # was modeled by its `node`'s contract alone → false-positive render-key-absent.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "l1": {"type": "agent"}, "l2": {"type": "agent"},
        "r": {"type": "render",
              "template_text": "{{raw}} {{results}} {{roles}} {{failed_roles}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "a", "fanout": ["l1", "l2"], "then": "s3"},
            "s3": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise render-key-absent
    assert not _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)


def fanout_branch_on_engine_key_not_hard_failed() -> None:
    # branch.on reads the fanout stage's OUTPUT — the engine-merged payload, where
    # `failed_roles` is always set (k-of-n degrade routing is exactly this pattern).
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "l1": {"type": "agent"}, "l2": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "a", "fanout": ["l1", "l2"], "min_success": 1,
                      "branch": {"on": "failed_roles", "routes": {}}}}}}
    validate_pipeline(cfg)   # must NOT raise branch-key-absent
    assert not _has(cfg, "branch-key-unprovided"), lint_pipeline(cfg)


def fanout_gate_role_human_key_not_hard_failed() -> None:
    # a human_gate fanned out as a ROLE parks the stage (AWAIT); resume replaces the
    # payload with the human's whole response — a key only the human supplies is NOT
    # provably absent downstream. Pre-fix this working pipeline was hard-rejected.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "g": {"type": "human_gate"}, "l1": {"type": "agent"},
        "r": {"type": "render", "template_text": "{{notes}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "a", "fanout": ["g", "l1"], "then": "s3"},
            "s3": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise: the human can supply `notes` on resume
    # the contract gap is still SURFACED as a warning (complete survives the merge)
    assert _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)


def fanout_absent_key_still_fails_loud_when_no_role_can_suspend() -> None:
    # soundness guard: all roles are built-ins that never AWAIT, so the merged set is
    # runtime-exact — a key that is neither inbound nor engine-set stays a hard error.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "l1": {"type": "agent"}, "l2": {"type": "agent"},
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "a", "fanout": ["l1", "l2"], "then": "s3"},
            "s3": {"node": "r"}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "verdict" in str(e) and "render-key-absent" in str(e), str(e)
        return
    raise AssertionError("a key absent from the exact merged set must still fail loud")


def provable_absence_upstream_of_the_fanout_still_fails_loud() -> None:
    # don't over-widen: the engine merge happens AT the fanout stage. A render
    # BEFORE it reading `results` is still provably broken.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False}, "l1": {"type": "agent"},
        "r": {"type": "render", "template_text": "{{results}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "r", "then": "s3"},
            "s3": {"node": "a", "fanout": ["l1"]}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "results" in str(e) and "render-key-absent" in str(e), str(e)
        return
    raise AssertionError("a provable absence upstream of the fanout must still fail loud")


def non_fanout_stage_unaffected_engine_keys_still_absent() -> None:
    # the widening is fanout-conditional: the same chain WITHOUT `fanout` never
    # merges `results`, so reading it stays a provable absence.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "r": {"type": "render", "template_text": "{{results}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "a", "then": "s3"},
            "s3": {"node": "r"}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "render-key-absent" in str(e), str(e)
        return
    raise AssertionError("a plain stage must not inherit the fanout widening")


def fork_rejoin_reduce_output_not_provably_absent() -> None:
    # a fork's `then` continuation carries the fan-in's REDUCE output (or the
    # unchanged input on a wait degrade) — a key a branch produces and the reduce
    # merges is NOT provably absent there. Pre-fix the fork stage passed `closed`
    # straight through and hard-rejected this working pipeline.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "b": {"type": "agent",
              "output_schema": {"properties": {"finding": {"type": "string"}}}},
        "r": {"type": "render", "template_text": "{{finding}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s0"},
            "s0": {"fork": ["sb"], "then": "sr"},
            "sb": {"node": "b", "then": "sf"},
            "sf": {"fanin": {"expect": ["sb"]}},
            "sr": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise render-key-absent
    # ...and no false WARNING either: the reduce provably delivers `finding` here, and
    # a warning would fail `--strict` CI on a working pipeline (eval finding 2026-07,
    # runtime-probed). Downstream of a join is unchecked, not wrongly flagged.
    assert not _has(cfg, "render-key-unprovided"), lint_pipeline(cfg)


def combined_fanin_fanout_stage_stays_sound_in_the_lattice() -> None:
    # `validate_pipeline` now REJECTS a stage that is both a fanin (JOIN) and a
    # fanout (SOURCE) — a walker-dependent trap (see the reject test below). But the
    # dataflow lattice keeps handling the combo SOUNDLY as defense-in-depth: the two
    # runtime walkers disagree (`_drive` runs the fanout merge; the branch walker
    # `_walk` runs the join and never sets `results`), so the lattice must take the
    # JOIN arm — the widest — with no hard error on either key and no closed claim
    # that `results` is present (eval finding 2026-07, runtime-probed). We probe the
    # lattice DIRECTLY here since validate now stops the config before it.
    from yaah.dataflow import analyze_dataflow
    nodes = {"a": {"type": "agent", "parse": False}, "l1": {"type": "agent"},
             "r": {"type": "render", "template_text": "{{results}} {{never_provided}}"}}
    stages = {"s1": {"node": "a", "then": "s2"},
              "s2": {"node": "a", "fanout": ["l1"], "fanin": {"expect": []},
                     "then": "s3"},
              "s3": {"node": "r"}}
    errors, _ = analyze_dataflow(nodes, stages, [], "s1", None)
    assert not any("render-key-absent" in e for e in errors), errors


def fanin_reduce_union_not_provably_absent() -> None:
    # the fan-in's default reduce UNIONS the arrived branch payloads; the lattice
    # meet is their INTERSECTION — a key one closed branch provides is not provably
    # absent after the join. Pre-fix the fanin stage passed the closed intersection
    # through and hard-rejected this working pipeline.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "p1": {"type": "agent", "parse": False},
        "p2": {"type": "agent", "parse": False, "carry": ["extra"]},
        "r": {"type": "render", "template_text": "{{extra}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s0"},
            "s0": {"fork": ["sb1", "sb2"], "then": "sr"},
            "sb1": {"node": "p1", "then": "sf"},
            "sb2": {"node": "p2", "then": "sf"},
            "sf": {"fanin": {"expect": ["sb1", "sb2"]}, "then": "sr"},
            "sr": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise render-key-absent


def wrong_typed_parallel_shape_is_a_clean_error_not_a_crash() -> None:
    # `fanout: 5` (or fork/validators as non-lists) used to escape the structural
    # checks and crash validate_pipeline with a raw TypeError ('int' object is not
    # iterable) — the author gets a traceback instead of a finding naming the stage.
    for key in ("fanout", "fork", "validators"):
        cfg = {"nodes": {"n": {"type": "transform", "call": "envelope"}},
               "graph": {"start": "a", "stages": {"a": {"node": "n", key: 5}}}}
        try:
            validate_pipeline(cfg)
        except ValueError as e:
            assert "'a'" in str(e) and key in str(e), (key, e)
        else:
            raise AssertionError("{}: 5 accepted".format(key))


# ── node-owned CONSUMES (ADR-0006 symmetry): a node reports the keys it READS from its
# inbound payload, the mirror of `provides`. render's consumes parses its `{{...}}`; a CUSTOM
# node declares `consumes: [...]` in config and gets its inputs checked — impossible before,
# when the checker hardcoded `type == "render"` as the only consumer. Behaviour for render is
# unchanged; the new reach is custom nodes.

def custom_node_consumes_declared_input_is_checked() -> None:
    # `mine` (an unknown type) declares it READS `missing`. Upstream is a parse:false agent —
    # a CLOSED {raw} payload that provably lacks `missing` — so the read is a provable hard
    # ERROR. Before node-owned consumes, a custom node's reads were invisible: this validated.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "mine": {"type": "my-scorer", "consumes": ["missing"]}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "mine"}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "'s2'" in str(e) and "missing" in str(e), e
        return
    raise AssertionError("custom node reading a provably-absent declared key must fail loud")


def custom_node_consumes_present_key_is_quiet() -> None:
    # same shape, but `mine` reads `raw` — which the parse:false agent DOES provide → quiet.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "mine": {"type": "my-scorer", "consumes": ["raw"]}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "mine"}}}}
    validate_pipeline(cfg)   # must NOT raise: raw is provided upstream


def custom_node_without_consumes_reads_nothing_checkable() -> None:
    # no `consumes:` and an unknown type → the node reports it reads nothing → no check,
    # never a false positive (the "smart by default, but doesn't have to" floor).
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "mine": {"type": "my-scorer"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "mine"}}}}
    validate_pipeline(cfg)   # must NOT raise


def render_consumes_still_hard_fails_a_provably_absent_placeholder() -> None:
    # render behaviour is PRESERVED through the new node-owned path: {{verdict}} after a
    # parse:false agent (closed {raw}) is still a hard error naming verdict.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "r": {"type": "render", "template_text": "{{verdict}}"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "verdict" in str(e) and "render-key-absent" in str(e), e
        return
    raise AssertionError("render of a provably-absent key must still fail loud")


def render_inline_consumes_does_not_fabricate_a_false_render_error() -> None:
    # opus eval finding 1: a render fills {{raw}} fine but ALSO carries consumes:["verdict"].
    # `verdict` is not a placeholder, so merging it would emit a FALSE "render FAILS" error on a
    # render that runs. A built-in consumer is authoritative → inline consumes ignored → clean.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "r": {"type": "render", "template_text": "Report: {{raw}}",
                           "consumes": ["verdict"]}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise: raw is provided; verdict is not actually read


def render_allow_unfilled_with_stray_consumes_is_not_blocked() -> None:
    # opus eval finding 2: allow_unfilled render (author opted out of failing) must not be
    # hard-blocked at load by a stray consumes:[...] — that self-contradicts its own advice.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "r": {"type": "render", "template_text": "{{raw}}",
                           "allow_unfilled": True, "consumes": ["verdict"]}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise


# ── foreach (ADR-0007): dynamic per-item fan-out — config-shape contract ────────────────────

def _foreach_cfg(**foreach_overrides):
    """A minimal pipeline whose swarm stage foreaches over an upstream-provided list."""
    fe = {"items": "requirements"}
    fe.update(foreach_overrides)
    return {"nodes": {"extract": {"type": "agent", "provides": ["requirements"]},
                      "skeptic": {"type": "agent"}},
            "graph": {"start": "s1", "stages": {
                "s1": {"node": "extract", "then": "s2"},
                "s2": {"node": "skeptic", "foreach": fe}}}}


def foreach_valid_config_is_accepted() -> None:
    validate_pipeline(_foreach_cfg())                                    # minimal
    validate_pipeline(_foreach_cfg(into="req", carry=["doc"], max_concurrent=3))


def foreach_items_read_from_a_provably_absent_key_is_a_hard_error() -> None:
    # design-eval #5: the ENGINE reads payload[items] + carries, not the per-item
    # worker — so the check is stage-level (like branch.on), against the INBOUND
    # flow. Upstream is a parse:false agent → a CLOSED {raw} payload that provably
    # lacks `requirements` → the swarm WILL fail foreach_input every run → ERROR.
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "skeptic": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "skeptic", "foreach": {"items": "requirements"}}}}}
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "requirements" in str(e), e
        return
    raise AssertionError("foreach over a provably-absent items key must fail loud")


def foreach_carry_of_a_provably_absent_key_is_a_hard_error() -> None:
    cfg = {"nodes": {"a": {"type": "agent", "parse": False},
                     "skeptic": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "skeptic",
                      "foreach": {"items": "raw", "carry": ["doc"]}}}}}
    # NB items reads `raw` (provided by the parse:false agent); `doc` is absent.
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "doc" in str(e), e
        return
    raise AssertionError("foreach carry of a provably-absent key must fail loud")


def foreach_stage_provides_results_to_downstream_reads() -> None:
    # design-eval #4: without an explicit `_transfer` arm, the else would apply
    # the per-item WORKER's contract (parse:false agent → closed {raw}) to the
    # STAGE output — dropping inbound keys and manufacturing a FALSE hard error
    # on this downstream {{results}} render. The arm must model the merge.
    cfg = {"nodes": {"extract": {"type": "agent", "provides": ["requirements"]},
                     "skeptic": {"type": "agent", "parse": False},
                     "r": {"type": "render",
                           "template_text": "{{results}} {{failed_items}} {{requirements}}"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "extract", "then": "s2"},
               "s2": {"node": "skeptic", "foreach": {"items": "requirements"},
                      "then": "s3"},
               "s3": {"node": "r"}}}}
    validate_pipeline(cfg)   # must NOT raise: results/failed_items provided, inbound kept


def foreach_worker_consumes_check_against_the_per_item_payload() -> None:
    # impl-eval HIGH: the worker's consumes were checked against the STAGE inbound
    # — but the worker's real input is the engine-made per-item payload
    # {into, item_index} ∪ carry ∪ sticky. A render worker reading {{item}} was a
    # FALSE load-blocking positive; one reading an uncarried {{doc}} is a REAL bug
    # (the per-item payload provably lacks it) and must fail loud naming carry.
    ok = {"nodes": {"a": {"type": "agent", "parse": False},
                    "summ": {"type": "render", "template_text": "summary of {{item}}"}},
          "graph": {"start": "s1", "stages": {
              "s1": {"node": "a", "then": "s2"},
              "s2": {"node": "summ", "foreach": {"items": "raw"}}}}}
    validate_pipeline(ok)   # must NOT raise: {{item}} fills per item at runtime

    carried = {"nodes": {"a": {"type": "agent", "parse": False, "provides": ["doc"]},
                         "summ": {"type": "render",
                                  "template_text": "{{item}} against {{doc}}"}},
               "graph": {"start": "s1", "stages": {
                   "s1": {"node": "a", "then": "s2"},
                   "s2": {"node": "summ",
                          "foreach": {"items": "raw", "carry": ["doc"]}}}}}
    validate_pipeline(carried)   # must NOT raise: doc is carried into each item

    uncarried = {"nodes": {"a": {"type": "agent", "parse": False, "provides": ["doc"]},
                           "summ": {"type": "render",
                                    "template_text": "{{item}} against {{doc}}"}},
                 "graph": {"start": "s1", "stages": {
                     "s1": {"node": "a", "then": "s2"},
                     "s2": {"node": "summ", "foreach": {"items": "raw"}}}}}
    try:
        validate_pipeline(uncarried)
    except ValueError as e:
        assert "doc" in str(e) and "carry" in str(e), e
        return
    raise AssertionError("a worker reading an uncarried key must fail loud")


def foreach_closed_drops_when_the_item_node_may_suspend() -> None:
    # the one-node analog of fanout's any-role rule: a human_gate item worker may
    # AWAIT → the resume merge makes the runtime set unprovable → `closed` must
    # not survive, so a downstream read of an absent key is NOT a hard error.
    from yaah.dataflow import analyze_dataflow
    nodes = {"a": {"type": "agent", "parse": False, "provides": ["reqs"]},
             "gate": {"type": "human_gate"},
             "r": {"type": "render", "template_text": "{{never_provided}}"}}
    stages = {"s1": {"node": "a", "then": "s2"},
              "s2": {"node": "gate", "foreach": {"items": "reqs"}, "then": "s3"},
              "s3": {"node": "r"}}
    errors, _ = analyze_dataflow(nodes, stages, [], "s1", None)
    assert not any("render-key-absent" in e for e in errors), errors
    # contrast: an agent item worker (never suspends) keeps closed → hard error
    nodes2 = dict(nodes, a2={"type": "agent"})
    stages2 = {"s1": {"node": "a", "then": "s2"},
               "s2": {"node": "a2", "foreach": {"items": "reqs"}, "then": "s3"},
               "s3": {"node": "r"}}
    errors2, _ = analyze_dataflow(nodes2, stages2, [], "s1", None)
    assert any("render-key-absent" in e for e in errors2), errors2


def foreach_structural_shapes_are_rejected() -> None:
    cases = [
        ("not-a-dict", {"nodes": {"a": {"type": "agent"}},
                        "graph": {"start": "s", "stages": {
                            "s": {"node": "a", "foreach": ["x"]}}}}),
        ("items missing", {"nodes": {"a": {"type": "agent"}},
                           "graph": {"start": "s", "stages": {
                               "s": {"node": "a", "foreach": {"max_concurrent": 2}}}}}),
        ("items empty", _foreach_cfg(items="")),
        ("items not str", _foreach_cfg(items=7)),
        ("max_concurrent zero", _foreach_cfg(max_concurrent=0)),
        ("max_concurrent bool", _foreach_cfg(max_concurrent=True)),
        ("max_concurrent str", _foreach_cfg(max_concurrent="3")),
        ("carry not list", _foreach_cfg(carry="doc")),
        ("carry non-str member", _foreach_cfg(carry=["doc", 3])),
        ("into not str", _foreach_cfg(into=5)),
        ("unknown foreach key", _foreach_cfg(max_parallel=3)),
    ]
    for label, cfg in cases:
        try:
            validate_pipeline(cfg)
        except ValueError as e:
            assert "foreach" in str(e), (label, e)
            continue
        raise AssertionError("foreach shape must be rejected: {}".format(label))


def foreach_is_exclusive_with_other_parallel_shapes() -> None:
    # one stage, one parallel shape — foreach joins the fanout/fork/fanin rejects.
    for extra in ({"fanout": ["skeptic"]}, {"fork": ["s1"]}, {"fanin": {"expect": ["x"]}}):
        cfg = _foreach_cfg()
        cfg["graph"]["stages"]["s2"].update(extra)
        try:
            validate_pipeline(cfg)
        except ValueError as e:
            assert "foreach" in str(e), (extra, e)
            continue
        raise AssertionError("foreach + {} must be rejected".format(list(extra)))


def foreach_requires_a_node() -> None:
    cfg = _foreach_cfg()
    del cfg["graph"]["stages"]["s2"]["node"]
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "node" in str(e), e
        return
    raise AssertionError("a foreach stage without a node must be rejected")


def min_success_is_accepted_on_a_foreach_stage() -> None:
    # design-eval finding #1: the old guard demanded `fanout` be a list, so the
    # ADR's own example config failed to load. With foreach the item count is
    # runtime-sized — the only static bound is >= 1.
    cfg = _foreach_cfg()
    cfg["graph"]["stages"]["s2"]["min_success"] = 5
    validate_pipeline(cfg)                                   # must NOT raise
    cfg["graph"]["stages"]["s2"]["min_success"] = 0          # still bounded below
    try:
        validate_pipeline(cfg)
    except ValueError as e:
        assert "min_success" in str(e), e
    else:
        raise AssertionError("min_success 0 must be rejected on foreach too")
    # and the fanout upper-bound rule is unchanged
    bad = {"nodes": {"a": {"type": "agent"}, "b": {"type": "agent"}},
           "graph": {"start": "s", "stages": {
               "s": {"node": "a", "fanout": ["a", "b"], "min_success": 3}}}}
    try:
        validate_pipeline(bad)
    except ValueError as e:
        assert "min_success" in str(e), e
    else:
        raise AssertionError("min_success > len(fanout) must still be rejected")


def fanin_combined_with_a_parallel_source_is_rejected() -> None:
    # A `fanin` is a parallel JOIN; `fanout`/`fork` is a parallel SOURCE. One stage
    # that is BOTH is a walker-dependent trap: `_drive` treats it fork-first, the
    # branch walker treats it fanin-first, so the runtime behaviour depends on which
    # walker reaches it. The dataflow lattice was made sound either way, but the
    # CONFIG is ambiguous — reject it at load, naming the stage and both shapes.
    for src in ("fanout", "fork"):
        cfg = {"nodes": {"n": {"type": "transform", "call": "envelope"}},
               "graph": {"start": "j", "stages": {
                   "b1": {"node": "n", "then": "j"},
                   "j": {"node": "n", "fanin": {"expect": ["b1"]}, src: ["b1"]}}}}
        try:
            validate_pipeline(cfg)
        except ValueError as e:
            assert "'j'" in str(e) and "fanin" in str(e) and src in str(e), (src, e)
        else:
            raise AssertionError("fanin+{} accepted".format(src))
    # a lone fanin (the legitimate join) is NOT rejected by this rule
    ok = {"nodes": {"n": {"type": "transform", "call": "envelope"}},
          "graph": {"start": "b1", "stages": {
              "b1": {"node": "n", "then": "j"},
              "j": {"node": "n", "fanin": {"expect": ["b1"]}}}}}
    validate_pipeline(ok)   # must NOT raise on the fanin-only join


# ── [lint: untrusted-unfenced] — agent-authored text rendered UNFENCED at a consumer ──
# M12-2 (mailbox): s_factory fences agent output in AGENT prompts (`{{!spec}}`), but the same
# text renders UNFRAMED in human_gate `ask` strings and render templates (which use the plain
# templater — no `!` fencing). This ADVISORY heuristic flags a consumer that interpolates
# `{{key}}` where an agent stage on a path to it AUTHORS `key`. Honest framing is LOCKED: it is
# NOT an injection-safety proof, and it does NOT tell the author to write `{{!key}}` at a
# render/gate site (that is a literal no-op there — empirically verified).

_UT = "untrusted-unfenced"


def _ut(cfg, base_path=None):
    return [m for m in lint_pipeline(cfg, base_path) if _UT in m]


def _gate_ask_cfg(ask, *, schema=None, parse=True, agent_type="agent",
                  producer_extra=None):
    """agent (producer) -> human_gate whose `ask` string is `ask`."""
    prod = {"type": agent_type}
    if parse is not None:
        prod["parse"] = parse
    if schema is not None:
        prod["output_schema"] = schema
    if producer_extra:
        prod.update(producer_extra)
    return {"nodes": {"p": prod, "g": {"type": "human_gate", "ask": ask}},
            "graph": {"start": "s1", "stages": {"s1": {"node": "p", "then": "s2"},
                                                "s2": {"node": "g"}}}}


def _render_after_agent_cfg(template, *, schema=None, parse=True):
    prod = {"type": "agent"}
    if parse is not None:
        prod["parse"] = parse
    if schema is not None:
        prod["output_schema"] = schema
    return {"nodes": {"p": prod, "r": {"type": "render", "template_text": template}},
            "graph": {"start": "s1", "stages": {"s1": {"node": "p", "then": "s2"},
                                                "s2": {"node": "r"}}}}


def warns_untrusted_agent_key_in_gate_ask() -> None:
    # grill authors `question`; the gate ask interpolates it unfenced -> warn, naming the key
    cfg = _gate_ask_cfg("Answer: {{question}}", schema={"required": ["question"]})
    w = _ut(cfg)
    assert w and "question" in w[0], w
    assert "'s1'" in w[0], w[0]          # names the producing agent STAGE
    assert "human_gate" in w[0], w[0]    # names the consumer node type


def warns_untrusted_agent_key_in_render() -> None:
    cfg = _render_after_agent_cfg("Report: {{summary}}", schema={"required": ["summary"]})
    w = _ut(cfg)
    assert w and "summary" in w[0], w
    assert "render" in w[0], w[0]


def warns_untrusted_agent_raw_is_authored() -> None:
    # a parse:false agent authors the whole model text as `raw`; a gate showing {{raw}} is untrusted
    cfg = _gate_ask_cfg("Model said: {{raw}}", parse=False)
    assert _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_fenced_key() -> None:
    # `{{!question}}` = author marked the value untrusted -> this rule stays quiet on it
    cfg = _gate_ask_cfg("Answer: {{!question}}", schema={"required": ["question"]})
    assert not _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_no_placeholders() -> None:
    cfg = _gate_ask_cfg("Approve to proceed.", schema={"required": ["question"]})
    assert not _ut(cfg)


def quiet_untrusted_non_agent_transform_key() -> None:
    # an args-transform NESTS its output under `into: data` (config-derived, not agent text) ->
    # a render of {{data}} has no agent author -> quiet.
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["v"]}},
        "t": {"type": "transform", "target": "fn:m:f", "into": "data"},
        "r": {"type": "render", "template_text": "{{data}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_entry_or_carried_key() -> None:
    # `request` rides the payload from the entry input (carried), no agent authors it -> quiet,
    # even though it flows through an agent stage.
    cfg = _render_after_agent_cfg("{{request}}",
                                  schema={"required": ["v"]})
    cfg["nodes"]["p"]["carry"] = ["request"]
    assert not _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_gate_decision_is_human_typed() -> None:
    # the human_gate produces `decision` (human-typed, trusted-ish); a downstream render of
    # {{decision}} where NO agent authored `decision` -> quiet.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "g": {"type": "human_gate"},
        "r": {"type": "render", "template_text": "{{decision}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "g", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _ut(cfg), lint_pipeline(cfg)


def warns_untrusted_judge_decision_at_gate() -> None:
    # but a `decision` AUTHORED by a judge AGENT (parse:true, schema) and shown unfenced at a
    # gate IS flagged — same key name, agent provenance (the review-pipeline gate pattern).
    cfg = _gate_ask_cfg("Judge said {{decision}} — proceed?",
                        schema={"properties": {"decision": {"enum": ["ship", "block"]}}})
    assert _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_engine_key_from_shell() -> None:
    # `exit_code` is an engine key a shell node emits (not agent text) -> quiet.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "sh": {"type": "shell"},
        "r": {"type": "render", "template_text": "{{exit_code}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "sh", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_past_opaque_unknown_provenance() -> None:
    # a key that only an OPAQUE (undeclared) envelope-transform could have put on the payload,
    # NOT authored by any agent, has UNKNOWN provenance -> quiet (no guessing). The agent here
    # authors only `raw`; `derived` is invented by the opaque transform.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "t": {"type": "transform", "target": "fn:m:f", "call": "envelope"},
        "g": {"type": "human_gate", "ask": "See {{derived}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "g"}}}}
    assert not _ut(cfg), lint_pipeline(cfg)


def warns_untrusted_through_opaque_transform_when_agent_authors_it() -> None:
    # the CLIENT's real shape: agent AUTHORS `question` (schema), an opaque parse transform
    # folds it, the gate shows it. The agent is a reaching ancestor -> flag (the lattice loses
    # the key through the opaque transform, but provenance is by graph reachability, not flow).
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["question"]}},
        "t": {"type": "transform", "target": "fn:m:f", "call": "envelope"},
        "g": {"type": "human_gate", "ask": "Q: {{question}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "g"}}}}
    assert _ut(cfg), lint_pipeline(cfg)


def quiet_untrusted_agent_does_not_reach_consumer() -> None:
    # an agent that authors `spec` but sits on a FORWARD-ONLY branch that never reaches the gate
    # must not taint it (reachability, not "any agent anywhere").
    cfg = {"nodes": {
        "entry": {"type": "transform", "target": "fn:m:f", "provides": ["seed"], "call": "envelope"},
        "off": {"type": "agent", "output_schema": {"required": ["spec"]}},
        "g": {"type": "human_gate", "ask": "See {{spec}}"}},
        "graph": {"start": "s1", "stages": {
            # s1 branches: to the gate OR to the off-path agent; the agent has no route to the gate
            "s1": {"node": "entry", "branch": {"on": "seed", "routes": {"x": "soff"}, "default": "sg"}},
            "soff": {"node": "off", "then": None},
            "sg": {"node": "g"}}}}
    assert not _ut(cfg), lint_pipeline(cfg)


def untrusted_message_makes_no_soundness_claim() -> None:
    # honest framing LOCKED: the message must NOT claim proof/soundness/safety, and MUST carry
    # the heuristic caveat. (Falsifies any future edit that over-sells the rule.)
    cfg = _gate_ask_cfg("Q: {{question}}", schema={"required": ["question"]})
    m = _ut(cfg)[0].lower()
    assert "heuristic" in m, m
    assert "not an injection-safety proof" in m, m
    for overclaim in ("guarantee", "proven", "safe from", "prevents injection", "secure"):
        assert overclaim not in m, (overclaim, m)


def untrusted_render_gate_message_does_not_prescribe_broken_fence() -> None:
    # render/gate can't fence: the fix wording must NOT tell the author to just write {{!key}}
    # there (it renders literally). It must point at sanitize-upstream / confirm-consumer.
    cfg = _gate_ask_cfg("Q: {{question}}", schema={"required": ["question"]})
    m = _ut(cfg)[0]
    assert "sanitize" in m.lower(), m
    # it explains the site can't fence rather than prescribing {{!question}} as the fix
    assert "does NOT honor" in m or "does not honor" in m, m


def untrusted_lint_never_raises_on_malformed() -> None:
    for cfg in (
        {"nodes": {"g": {"type": "human_gate", "ask": None}}, "graph": {"stages": {}}},
        {"nodes": {"a": {"type": "agent", "output_schema": ["bad"]},
                   "g": {"type": "human_gate", "ask": "{{x}}"}},
         "graph": {"start": "s1", "stages": {"s1": {"node": "a", "then": "s2"},
                                             "s2": {"node": "g"}}}},
        {"nodes": {"g": {"type": "human_gate", "ask": "{{x}}"}}, "graph": {}},
    ):
        lint_pipeline(cfg)   # must not raise


def untrusted_hits_carry_one_floor_caveat() -> None:
    # eval finding (2026-06-30): renamed-key false negatives DOMINATE on the real client config,
    # so lint output must not read as exhaustive. With hits: exactly ONE consolidated caveat,
    # naming the count and the provides fix. Without hits: NO caveat (never a fresh --strict
    # failure on an otherwise-quiet pipeline).
    cfg = _gate_ask_cfg("Q: {{question}} and {{spec}}",
                        schema={"required": ["question", "spec"]})
    w = _ut(cfg)
    caveats = [m for m in w if "FLOOR" in m]
    assert len(caveats) == 1, w
    assert "2 untrusted-unfenced finding(s)" in caveats[0], caveats[0]
    assert "provides" in caveats[0], caveats[0]
    quiet = _gate_ask_cfg("Approve to proceed.", schema={"required": ["question"]})
    assert not _ut(quiet), lint_pipeline(quiet)


def untrusted_strict_fails_with_exit_2() -> None:
    # the rule rides the existing warning->strict convention: `yaah validate --strict` exits 2.
    pipeline = _gate_ask_cfg("Q: {{question}}", schema={"required": ["question"]})
    old_err, old_out = sys.stderr, sys.stdout
    sys.stderr, sys.stdout = io.StringIO(), io.StringIO()
    code = 0
    try:
        _dispatch_validate({"root": "t", "strict": True}, {"pipeline": pipeline}, ".")
    except SystemExit as e:
        code = 0 if e.code is None else int(e.code)
    finally:
        err = sys.stderr.getvalue()
        sys.stderr, sys.stdout = old_err, old_out
    assert code == 2, code
    assert _UT in err, err


# ── allow_untrusted: opt-out for RENDER sites whose output feeds a human/file ──
# A render can't fence ({{!key}} is a literal there — templating.fill), so the
# untrusted-unfenced warning has no in-place remedy on a render. `allow_untrusted:
# true` on the render node is the author's explicit "this output feeds a human/
# file, not a model prompt" opt-out — parallel to `allow_unfilled`. It silences
# ONLY the render's own sites; agent-prompt (n/a) and human_gate sites are
# unaffected.


def render_untrusted_fires_without_allow_untrusted() -> None:
    # baseline (may already be covered): a render of an agent-authored key warns.
    cfg = _render_after_agent_cfg("Report: {{summary}}", schema={"required": ["summary"]})
    assert _ut(cfg), lint_pipeline(cfg)


def allow_untrusted_silences_the_render_site() -> None:
    cfg = _render_after_agent_cfg("Report: {{summary}}", schema={"required": ["summary"]})
    cfg["nodes"]["r"]["allow_untrusted"] = True
    assert not _ut(cfg), lint_pipeline(cfg)


def allow_untrusted_does_not_silence_a_gate_site() -> None:
    # the opt-out is per-RENDER-node: a human_gate site is a different consumer and
    # stays flagged even if some unrelated render sets allow_untrusted.
    cfg = _gate_ask_cfg("Answer: {{question}}", schema={"required": ["question"]})
    cfg["nodes"]["g"]["allow_untrusted"] = True   # not a render → must NOT silence
    assert _ut(cfg), lint_pipeline(cfg)


def allow_untrusted_only_silences_the_render_that_sets_it() -> None:
    # two renders of the same agent-authored key; only the one with the flag is quiet.
    cfg = {"nodes": {
        "a": {"type": "agent", "output_schema": {"required": ["summary"]}},
        "r1": {"type": "render", "template_text": "{{summary}}", "allow_untrusted": True},
        "r2": {"type": "render", "template_text": "{{summary}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "r1", "then": "s3"},
            "s3": {"node": "r2"}}}}
    w = _ut(cfg)
    # exactly the r2 site warns (r1 silenced); the floor caveat is the only other hit
    site_hits = [m for m in w if "FLOOR" not in m]
    assert len(site_hits) == 1, w
    assert "'s3'" in site_hits[0], site_hits[0]


def render_untrusted_message_names_allow_untrusted_remedy() -> None:
    cfg = _render_after_agent_cfg("Report: {{summary}}", schema={"required": ["summary"]})
    m = [x for x in _ut(cfg) if "FLOOR" not in x][0]
    assert "allow_untrusted" in m, m
    # honest framing preserved: still a heuristic, still points at sanitize
    assert "heuristic" in m.lower() and "sanitize" in m.lower(), m


def gate_untrusted_message_does_not_name_allow_untrusted() -> None:
    # allow_untrusted is a RENDER remedy — the gate message must not prescribe it.
    cfg = _gate_ask_cfg("Answer: {{question}}", schema={"required": ["question"]})
    m = [x for x in _ut(cfg) if "FLOOR" not in x][0]
    assert "allow_untrusted" not in m, m


# ── [lint: reserved-key-collision] — author declares an engine-injected key as their own ──
# The harness `_with_feedback` (harness.py) injects `feedback` (the validator failures) AND
# `priorAttempt` (the prior output) onto the payload on every `feedback: true` retry, and the
# agent node's `_render` (agents/agent.py) auto-appends a non-empty `feedback` payload value to
# the prompt. An author who DECLARES either name as their own key gets silent double-injection/
# collision — the verify-loop example renames its own key to `loop_feedback` for exactly this.

_RK = "reserved-key-collision"


def _rk(cfg):
    return [m for m in lint_pipeline(cfg) if _RK in m]


def warns_reserved_feedback_in_node_provides() -> None:
    cfg = {"nodes": {"t": {"type": "transform", "target": "fn:m:f", "provides": ["feedback"]}},
           "graph": {"start": "s", "stages": {"s": {"node": "t"}}}}
    w = _rk(cfg)
    assert w and "feedback" in w[0] and "provides" in w[0], w


def warns_reserved_priorattempt_in_node_provides() -> None:
    cfg = {"nodes": {"t": {"type": "transform", "target": "fn:m:f", "provides": ["priorAttempt"]}},
           "graph": {"start": "s", "stages": {"s": {"node": "t"}}}}
    w = _rk(cfg)
    assert w and "priorAttempt" in w[0], w


def warns_reserved_in_output_schema_properties() -> None:
    cfg = {"nodes": {"a": {"type": "agent",
                          "output_schema": {"properties": {"feedback": {"type": "string"}}}}},
           "graph": {"start": "s", "stages": {"s": {"node": "a"}}}}
    w = _rk(cfg)
    assert w and "feedback" in w[0] and "output_schema" in w[0], w


def warns_reserved_in_output_schema_required() -> None:
    cfg = {"nodes": {"a": {"type": "agent", "output_schema": {"required": ["priorAttempt"]}}},
           "graph": {"start": "s", "stages": {"s": {"node": "a"}}}}
    assert any("priorAttempt" in m for m in _rk(cfg)), _rk(cfg)


def warns_reserved_in_sticky() -> None:
    cfg = {"nodes": {"a": {"type": "agent"}},
           "graph": {"start": "s", "stages": {"s": {"node": "a"}}, "sticky": ["feedback"]}}
    w = _rk(cfg)
    assert w and "feedback" in w[0] and "sticky" in w[0], w


def warns_reserved_in_foreach_into() -> None:
    cfg = {"nodes": {"a": {"type": "agent", "provides": ["items"]}, "w": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "w", "foreach": {"items": "items", "into": "feedback"}}}}}
    w = _rk(cfg)
    assert w and "feedback" in w[0] and "foreach" in w[0], w


def warns_reserved_in_foreach_items() -> None:
    cfg = {"nodes": {"a": {"type": "agent", "provides": ["feedback"]}, "w": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "w", "foreach": {"items": "feedback"}}}}}
    assert any("feedback" in m and "foreach" in m for m in _rk(cfg)), _rk(cfg)


def warns_reserved_in_foreach_carry() -> None:
    cfg = {"nodes": {"a": {"type": "agent", "provides": ["reqs"]}, "w": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "then": "s2"},
               "s2": {"node": "w", "foreach": {"items": "reqs", "carry": ["priorAttempt"]}}}}}
    assert any("priorAttempt" in m and "foreach" in m for m in _rk(cfg)), _rk(cfg)


def warns_reserved_in_effects_from() -> None:
    cfg = {"nodes": {"p": {"type": "post"}},
           "graph": {"start": "s", "stages": {"s": {"node": "p", "effects_from": "feedback"}}}}
    w = _rk(cfg)
    assert w and "feedback" in w[0] and "effects_from" in w[0], w


def warns_reserved_in_concerns_from_and_into() -> None:
    cfg = {"nodes": {"a": {"type": "agent"}},
           "graph": {"start": "s", "stages": {
               "s": {"node": "a", "concerns_from": "feedback", "concerns_into": "priorAttempt"}}}}
    w = _rk(cfg)
    assert any("feedback" in m and "concerns_from" in m for m in w), w
    assert any("priorAttempt" in m and "concerns_into" in m for m in w), w


def reserved_message_names_key_surface_and_injection() -> None:
    cfg = {"nodes": {"t": {"type": "transform", "target": "fn:m:f", "provides": ["feedback"]}},
           "graph": {"start": "s", "stages": {"s": {"node": "t"}}}}
    m = _rk(cfg)[0]
    assert "feedback" in m and "provides" in m, m       # names the key + surface
    assert "inject" in m.lower(), m                      # cites the auto-injection behavior
    assert "retry" in m.lower(), m
    assert _RK in m, m


# ── reserved-key near-misses that must stay SILENT ──

def quiet_reserved_normal_config() -> None:
    cfg = {"nodes": {"a": {"type": "agent", "provides": ["verdict"],
                          "output_schema": {"required": ["verdict"]}}},
           "graph": {"start": "s", "stages": {"s": {"node": "a"}}, "sticky": ["run_id"]}}
    assert not _rk(cfg), lint_pipeline(cfg)


def quiet_reserved_loop_feedback_rename() -> None:
    # the verify-loop workaround: an author who renames the key to `loop_feedback` is CLEAN,
    # across provides + sticky (the real example's surfaces).
    cfg = {"nodes": {"t": {"type": "transform", "target": "fn:m:f",
                          "provides": ["cycle", "loop_feedback"]}},
           "graph": {"start": "s", "stages": {"s": {"node": "t"}},
                     "sticky": ["cycle", "loop_feedback"]}}
    assert not _rk(cfg), lint_pipeline(cfg)


def quiet_reserved_node_level_carry_not_a_surface() -> None:
    # arch-drift carries `feedback` on a NODE-level `carry` list (cooperating with the engine
    # convention to keep `{{feedback}}` filled) — node-level carry is NOT a declared-key surface,
    # only a foreach `carry` is. Must stay silent.
    cfg = {"nodes": {"a": {"type": "agent", "carry": ["snapshot", "feedback"]}},
           "graph": {"start": "s", "stages": {"s": {"node": "a"}}}}
    assert not _rk(cfg), lint_pipeline(cfg)


def quiet_reserved_human_gate_decision_schema_not_a_surface() -> None:
    # arch-drift-ab's human_gate exposes a `feedback` field in its `decision_schema` (the human's
    # revise note) — decision_schema is NOT `output_schema` and is out of scope. Silent.
    cfg = {"nodes": {"g": {"type": "human_gate", "form": "json_schema",
                          "decision_schema": {"type": "object", "required": ["decision"],
                                              "properties": {"decision": {"enum": ["ok"]},
                                                             "feedback": {"type": "string"}}}}},
           "graph": {"start": "s", "stages": {"s": {"node": "g"}}}}
    assert not _rk(cfg), lint_pipeline(cfg)


def reserved_lint_never_raises_on_malformed() -> None:
    for cfg in (
        {"nodes": {"a": {"type": "agent", "provides": "feedback"}},   # provides not a list
         "graph": {"start": "s", "stages": {"s": {"node": "a"}}}},
        {"nodes": {"a": {"type": "agent", "output_schema": ["bad"]}},
         "graph": {"start": "s", "stages": {"s": {"node": "a"}}}},
        {"nodes": {"a": {"type": "agent"}},
         "graph": {"stages": {"s": {"node": "a", "foreach": "not-a-dict"}}}},
        {"nodes": {"a": "not-a-dict"}, "graph": {}},
        # adversarial-eval 2026-07-07: the container shapes that ACTUALLY crashed
        # lint_pipeline (via pre-existing sibling lints, reached before the new rules) —
        # a non-dict stages container, a non-dict stage VALUE, a non-dict graph,
        # a non-dict nodes container. The never-raises contract must hold on the
        # dimension where it was weak, not only where it was already strong.
        {"nodes": {"a": {"type": "agent"}}, "graph": {"stages": ["not-a-dict"]}},
        {"nodes": {"a": {"type": "agent"}},
         "graph": {"start": "s", "stages": {"s": "not-a-dict"}}},
        {"nodes": {"a": {"type": "agent"}}, "graph": ["not-a-dict"]},
        {"nodes": "not-a-dict", "graph": {"stages": {"s": {"node": "a"}}}},
    ):
        lint_pipeline(cfg)   # must not raise


# ── [lint: clear-is-not-rollback] — an idempotent (committed-effect) node under bare `clear` ──
# ADR-0008: `on_error: "clear"` (the default) drops ENGINE state only. A node the author marked
# `idempotent: true` commits a replay-sensitive EXTERNAL effect; failing under clear READS as
# cleaned up while the effect persists. Nudge the author to declare compensate / rollback, or
# `on_error: null` to acknowledge fail-as-is.

_CR = "clear-is-not-rollback"


def _cr(cfg):
    return [m for m in lint_pipeline(cfg) if _CR in m]


def _idem_cfg(*, rollback=None, on_error="__absent__", idempotent=True):
    node = {"type": "post"}
    if idempotent:
        node["idempotent"] = True
    if rollback is not None:
        node["rollback"] = rollback
    stage = {"node": "p"}
    if on_error != "__absent__":
        stage["on_error"] = on_error
    return {"nodes": {"p": node}, "graph": {"start": "s", "stages": {"s": stage}}}


def warns_idempotent_default_clear() -> None:
    w = _cr(_idem_cfg())                                  # no on_error, no rollback
    assert w and "'p'" in w[0], w


def warns_idempotent_explicit_clear() -> None:
    assert _cr(_idem_cfg(on_error="clear")), lint_pipeline(_idem_cfg(on_error="clear"))


def clear_rollback_message_names_the_escape_hatches() -> None:
    m = _cr(_idem_cfg())[0]
    assert "clear" in m and "engine" in m.lower(), m
    assert "compensate" in m and "rollback" in m and "on_error: null" in m, m
    assert _CR in m, m


def quiet_idempotent_with_rollback() -> None:
    cfg = _idem_cfg(rollback={"target": "fn:undo:x"})
    assert not _cr(cfg), lint_pipeline(cfg)


def quiet_idempotent_with_compensate() -> None:
    cfg = _idem_cfg(on_error={"compensate": "fn:undo:x"})
    assert not _cr(cfg), lint_pipeline(cfg)


def quiet_idempotent_on_error_null_optout() -> None:
    cfg = _idem_cfg(on_error=None)                        # explicit fail-as-is
    assert not _cr(cfg), lint_pipeline(cfg)


def quiet_non_idempotent_bare_clear() -> None:
    cfg = _idem_cfg(idempotent=False)                    # not a committed-effect node
    assert not _cr(cfg), lint_pipeline(cfg)


def clear_rollback_multistage_warns_on_the_uncovered_stage() -> None:
    # one node run by TWO stages: one compensates, one clears. The clearing stage is a real
    # silent-effect risk -> warn, naming that stage (soundness over per-node blanket-quiet).
    cfg = {"nodes": {"p": {"type": "post", "idempotent": True}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "p", "on_error": {"compensate": "fn:undo:x"}, "then": "s2"},
               "s2": {"node": "p"}}}}
    w = _cr(cfg)
    assert w and "'s2'" in w[0] and "'s1'" not in w[0], w


def clear_rollback_lint_never_raises_on_malformed() -> None:
    for cfg in (
        {"nodes": {"p": {"type": "post", "idempotent": True, "rollback": "bad"}},
         "graph": {"start": "s", "stages": {"s": {"node": "p"}}}},
        {"nodes": {"p": {"type": "post", "idempotent": True}},
         "graph": {"stages": {"s": {"node": "p", "on_error": ["weird"]}}}},
        {"nodes": {"p": "not-a-dict"}, "graph": {}},
    ):
        lint_pipeline(cfg)   # must not raise


# ── missing-carry: an agent that carries neither-declares a key a downstream reads ──
# The 2026-07-07 naive-agent field-test trap: a `parse:true` agent with NO
# output_schema/provides RESETS the payload to exactly {raw} (+carry +cwd) — its
# contract is neither `closed` nor `complete`, so the closed/complete render+branch
# checks both go silent (dataflow.py `else: note_blocked()`), and note_blocked finds
# no tainted envelope ancestor → nothing warned. `validate --strict` blessed the
# config; the run died with render_unfilled_placeholders. A WARNING tier (never a hard
# error — the model MAY echo the key), fired ONLY on the incomplete-agent-reset
# producer, distinguished from a fork/fanin JOIN output (same neither-flag Flow shape,
# but legitimately unchecked).


def _carry_cfg(template="{{topic}}", *, node_overrides=None, sticky=None,
               node_type="agent", parse=True):
    agent = {"type": node_type}
    if parse is not None:
        agent["parse"] = parse
    if node_overrides:
        agent.update(node_overrides)
    render = {"type": "render", "template_text": template}
    graph = {"start": "s1", "stages": {"s1": {"node": "a", "then": "s2"},
                                       "s2": {"node": "r"}}}
    if sticky is not None:
        graph["sticky"] = sticky
    return {"nodes": {"a": agent, "render": render, "r": render},
            "graph": graph}


def _has_carry_warn(cfg, base_path=None):
    return any("missing-carry" in m for m in lint_pipeline(cfg, base_path))


def missing_carry_fires_on_the_exact_2026_07_07_trap() -> None:
    # (1) parse:true agent, no output_schema/provides, does not carry `topic`; a
    # downstream render reads {{topic}}. Today silent; must now WARN naming the
    # reading stage, the key, the producing agent stage, and BOTH remedies.
    cfg = _carry_cfg("Report on {{topic}}")
    validate_pipeline(cfg)   # must NOT raise: warning-tier, not a hard error
    w = [m for m in lint_pipeline(cfg) if "missing-carry" in m]
    assert w, lint_pipeline(cfg)
    m = w[0]
    assert "topic" in m, m               # names the key
    assert "'s2'" in m, m                # names the reading stage
    assert "'s1'" in m or "'a'" in m, m  # names the producing agent (stage or role)
    assert "carry" in m, m               # remedy 1
    assert "output_schema" in m or "provides" in m, m  # remedy 2


def missing_carry_silent_when_key_is_carried() -> None:
    # (2) same pipeline + carry: ["topic"] on the agent → the reset's known set now
    # includes topic → silent BY CONSTRUCTION (no new knob).
    cfg = _carry_cfg("Report on {{topic}}", node_overrides={"carry": ["topic"]})
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_silent_when_key_is_sticky() -> None:
    # (3) graph.sticky re-applies the key on every transfer → in `known` → silent.
    cfg = _carry_cfg("Report on {{topic}}", sticky=["topic"])
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_silent_when_key_in_output_schema() -> None:
    # (4a) declaring the key in output_schema flips the contract to `complete`, and the
    # key is in `known` → the incomplete-reset case no longer applies → silent.
    cfg = _carry_cfg("Report on {{topic}}",
                     node_overrides={"output_schema": {"properties": {"topic": {"type": "string"}}}})
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_silent_when_key_in_provides() -> None:
    # (4b) declaring the key in inline `provides` does the same (author-declared → complete).
    cfg = _carry_cfg("Report on {{topic}}", node_overrides={"provides": ["topic"]})
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_silent_when_key_present_in_pipeline_input_and_carried() -> None:
    # (5) A pipeline-input key is only a silencer if it SURVIVES to the reading stage
    # (an agent reset drops it unless carried). The lint doesn't know the input keys at
    # load time, so "present in input" is honoured via carry/sticky — an input key the
    # agent CARRIES reaches the render and is silent; an input key it DROPS is exactly
    # the trap and warns. This pins the surviving-input case as silent.
    cfg = _carry_cfg("Report on {{topic}}", node_overrides={"carry": ["topic"]})
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_does_not_fire_on_a_join_output() -> None:
    # (6a) NO REGRESSION / no false positive: a fork+fanin JOIN output is Flow(known,
    # False, False) — the SAME neither-flag shape as the incomplete agent reset, but its
    # `known` legitimately does not list keys the reduce provides. The producing stage is
    # a fork/fanin, NOT an incomplete-agent-reset linear stage, so missing-carry must NOT
    # fire (the existing fork_rejoin test already forbids render-key-unprovided here).
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "b": {"type": "agent",
              "output_schema": {"properties": {"finding": {"type": "string"}}}},
        "r": {"type": "render", "template_text": "{{finding}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s0"},
            "s0": {"fork": ["sb"], "then": "sr"},
            "sb": {"node": "b", "then": "sf"},
            "sf": {"fanin": {"expect": ["sb"]}},
            "sr": {"node": "r"}}}}
    validate_pipeline(cfg)
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_does_not_fire_on_a_fanin_union_output() -> None:
    # (6a-bis) the fan-in UNION reduce: the lattice meet INTERSECTS closed branch payloads,
    # so a key only ONE branch carries falls out of `known` and the join output is
    # Flow(known, False, False) — untainted neither-flag, like the incomplete reset. But the
    # producer is a fanin, not an agent stage, so missing-carry must stay silent (mirrors
    # fanin_reduce_union_not_provably_absent).
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "p1": {"type": "agent", "parse": False},
        "p2": {"type": "agent", "parse": False, "carry": ["extra"]},
        "r": {"type": "render", "template_text": "{{extra}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s0"},
            "s0": {"fork": ["sb1", "sb2"], "then": "sr"},
            "sb1": {"node": "p1", "then": "sf"},
            "sb2": {"node": "p2", "then": "sf"},
            "sf": {"fanin": {"expect": ["sb1", "sb2"]}, "then": "sr"},
            "sr": {"node": "r"}}}}
    validate_pipeline(cfg)
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_is_silent_multihop_through_a_preserve_transform() -> None:
    # KNOWN LIMITATION (documented in dataflow._is_incomplete_reset_producer): the gate
    # checks the IMMEDIATE producer. incomplete-agent → args-transform (PRESERVE) → render
    # of a dropped key is a real bug, but the immediate producer is the preserve transform
    # (not the incomplete reset), so it is NOT flagged. A conservative FALSE NEGATIVE (safe:
    # under-warns, never false-positives on a working pipeline that would break --strict).
    # Pins the accepted behaviour so a future change to widen it is a deliberate choice.
    cfg = {"nodes": {
        "a": {"type": "agent"},
        "t": {"type": "transform", "target": "fn:m:f"},
        "r": {"type": "render", "template_text": "{{topic}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_does_not_fire_through_an_undeclared_envelope_transform() -> None:
    # (6b) NO REGRESSION: an UNDECLARED envelope-transform makes the consumer opaque —
    # note_blocked already emits the consolidated `transform-provides-undeclared` nudge.
    # missing-carry must NOT double-warn: the producer is a transform, not an agent reset.
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "t": {"type": "transform", "target": "fn:m:f", "call": "envelope"},
        "r": {"type": "render", "template_text": "{{verdict}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "t", "then": "s3"},
            "s3": {"node": "r"}}}}
    w = lint_pipeline(cfg)
    assert any("transform-provides-undeclared" in m for m in w), w
    assert not any("missing-carry" in m for m in w), w


def missing_carry_does_not_fire_on_a_custom_opaque_node() -> None:
    # (6c) NO REGRESSION: a custom (unknown) node type is OPAQUE — downstream uncheckable,
    # not an incomplete agent reset. missing-carry must stay silent (matches
    # custom_node_type_is_opaque_not_false_positive).
    cfg = {"nodes": {
        "a": {"type": "agent", "parse": False},
        "x": {"type": "my_custom_node"},
        "r": {"type": "render", "template_text": "{{custom_key}}"}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "x", "then": "s3"},
            "s3": {"node": "r"}}}}
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_does_not_regress_closed_or_complete_paths() -> None:
    # (6d) NO REGRESSION on the existing severities: a parse:false agent is CLOSED, so a
    # render of an absent key is still a hard ERROR (render-key-absent), NOT a
    # missing-carry warning; a `complete` (declared) agent still yields the
    # render-key-unprovided WARNING, not missing-carry.
    closed = {"nodes": {"a": {"type": "agent", "parse": False},
                        "r": {"type": "render", "template_text": "{{verdict}}"}},
              "graph": {"start": "s1", "stages": {
                  "s1": {"node": "a", "then": "s2"}, "s2": {"node": "r"}}}}
    try:
        validate_pipeline(closed)
        raise AssertionError("closed-path miss must still be a hard error")
    except ValueError as e:
        assert "render-key-absent" in str(e), str(e)
    assert not _has_carry_warn(closed), lint_pipeline(closed)   # not a missing-carry
    complete = _carry_cfg("{{verdict}}",
                          node_overrides={"output_schema": {"properties": {"other": {"type": "string"}}}})
    w = lint_pipeline(complete)
    assert any("render-key-unprovided" in m for m in w), w
    assert not any("missing-carry" in m for m in w), w   # complete path is render-key-unprovided


def missing_carry_fires_on_a_branch_read_too() -> None:
    # branch.on reads the agent's OUTPUT — same incomplete-reset producer, same trap.
    cfg = {"nodes": {"a": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "branch": {"on": "verdict", "routes": {}}}}}}
    validate_pipeline(cfg)
    w = [m for m in lint_pipeline(cfg) if "missing-carry" in m]
    assert w and "verdict" in w[0], lint_pipeline(cfg)


def missing_carry_silent_on_a_provided_read() -> None:
    # a render of {{raw}} (which the reset DOES provide) must not warn.
    cfg = _carry_cfg("{{raw}}")
    assert not _has_carry_warn(cfg), lint_pipeline(cfg)


def missing_carry_render_read_names_render_unfilled_consequence() -> None:
    # a render/consumes read of a dropped key really does die with
    # render_unfilled_placeholders — the per-site consequence for render.
    cfg = _carry_cfg("Report on {{topic}}")
    w = [m for m in lint_pipeline(cfg) if "missing-carry" in m]
    assert w, lint_pipeline(cfg)
    assert "render_unfilled_placeholders" in w[0], w[0]


def missing_carry_branch_read_names_branch_default_consequence() -> None:
    # FALSIFIER (write-first): a branch.on read of a dropped key does NOT fail
    # with render_unfilled_placeholders — branch.on absent silently takes
    # branch.default EVERY run (harness._next_stage). The message must name that
    # real consequence, not the render one.
    cfg = {"nodes": {"a": {"type": "agent"}},
           "graph": {"start": "s1", "stages": {
               "s1": {"node": "a", "branch": {"on": "verdict", "routes": {}}}}}}
    validate_pipeline(cfg)
    w = [m for m in lint_pipeline(cfg) if "missing-carry" in m]
    assert w, lint_pipeline(cfg)
    m = w[0]
    assert "branch.default" in m, m                        # names the real consequence
    assert "render_unfilled_placeholders" not in m, m      # NOT the render wording
    # remedies + trailer are unchanged from the shared message
    assert "carry" in m and ("output_schema" in m or "provides" in m), m


def missing_carry_foreach_read_names_foreach_input_consequence() -> None:
    # a foreach carry/items read of a dropped key fails with foreach_input (or
    # fans out without its carry), NOT render_unfilled_placeholders. Producer is
    # an incomplete-reset agent so the read is neither closed nor complete.
    cfg = {"nodes": {
        "a": {"type": "agent"},
        "w": {"type": "agent", "parse": False}},
        "graph": {"start": "s1", "stages": {
            "s1": {"node": "a", "then": "s2"},
            "s2": {"node": "w", "foreach": {"items": "raw", "carry": ["topic"]}}}}}
    validate_pipeline(cfg)
    w = [m for m in lint_pipeline(cfg) if "missing-carry" in m]
    assert w, lint_pipeline(cfg)
    m = w[0]
    assert "foreach_input" in m, m
    assert "render_unfilled_placeholders" not in m, m


def missing_carry_fails_strict_with_exit_2() -> None:
    # the whole point: --strict (the CI gate) would have caught the field-test trap.
    cfg = _carry_cfg("Report on {{topic}}")
    code, out, err = _validate_exit(cfg, strict=True)
    assert code == 2, (code, err)
    assert "missing-carry" in err, err
    # ...and plain validate surfaces it but still passes (advisory), consistent with the
    # other warn-tier lints.
    code2, out2, err2 = _validate_exit(cfg, strict=False)
    assert code2 == 0, (code2, err2)
    assert "missing-carry" in err2 and "ok:" in out2, (out2, err2)


def missing_carry_lint_never_raises_on_malformed() -> None:
    for cfg in (
        {"nodes": {"a": {"type": "agent", "carry": "not-a-list"},
                   "r": {"type": "render", "template_text": "{{topic}}"}},
         "graph": {"start": "s1", "stages": {"s1": {"node": "a", "then": "s2"},
                                             "s2": {"node": "r"}}}},
        {"nodes": {"a": {"type": "agent"}, "r": "not-a-dict"},
         "graph": {"start": "s1", "stages": {"s1": {"node": "a"}}}},
    ):
        lint_pipeline(cfg)   # must not raise


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
    fails_loud_branch_parse_false_provides_only_raw()
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
    gate_does_not_prove_absence_of_human_supplied_keys()
    gate_still_provides_decision_on_a_closed_path()
    gate_branch_on_human_supplied_key_not_hard_failed()
    provable_absence_before_the_gate_still_fails_loud()
    escalate_human_stage_does_not_prove_absence_downstream()
    quiet_render_after_get_into()
    quiet_render_sticky_survives_multihop()
    loop_converges_and_keeps_key()
    quiet_render_parse_false_agent_forwards_cwd_from()
    lint_never_raises_on_malformed_output_schema()
    placeholder_regex_is_single_source()
    attach_declared_provides_render_on_declared_key_validates_clean()
    attach_declared_provides_render_typo_warns_and_blocks_strict()
    attach_without_provides_fires_nudge_lint()
    attach_with_provides_silences_the_nudge()
    attach_empty_provides_is_the_explicit_optout()
    malformed_attach_non_list_is_hard_error()
    malformed_attach_non_string_item_is_hard_error()
    empty_attach_list_preserves_closed_hard_error()
    parse_true_attach_behavior_unchanged()
    attach_remedy_text_names_provides_first()
    warns_gate_two_outcomes_no_branch()
    quiet_gate_two_outcomes_with_branch_on_decision()
    quiet_gate_single_outcome_approve()
    quiet_gate_free_text_has_no_decision()
    warns_gate_json_schema_two_outcomes()
    quiet_gate_json_schema_single_outcome()
    opaque_nodes_provide_their_real_keys()
    quiet_render_default_into_for_get_and_post()
    custom_node_type_is_opaque_not_false_positive()
    fanout_engine_merged_keys_not_hard_failed()
    fanout_branch_on_engine_key_not_hard_failed()
    fanout_gate_role_human_key_not_hard_failed()
    fanout_absent_key_still_fails_loud_when_no_role_can_suspend()
    provable_absence_upstream_of_the_fanout_still_fails_loud()
    non_fanout_stage_unaffected_engine_keys_still_absent()
    fork_rejoin_reduce_output_not_provably_absent()
    combined_fanin_fanout_stage_stays_sound_in_the_lattice()
    fanin_reduce_union_not_provably_absent()
    wrong_typed_parallel_shape_is_a_clean_error_not_a_crash()
    fanin_combined_with_a_parallel_source_is_rejected()
    foreach_valid_config_is_accepted()
    foreach_items_read_from_a_provably_absent_key_is_a_hard_error()
    foreach_carry_of_a_provably_absent_key_is_a_hard_error()
    foreach_stage_provides_results_to_downstream_reads()
    foreach_worker_consumes_check_against_the_per_item_payload()
    foreach_closed_drops_when_the_item_node_may_suspend()
    foreach_structural_shapes_are_rejected()
    foreach_is_exclusive_with_other_parallel_shapes()
    foreach_requires_a_node()
    min_success_is_accepted_on_a_foreach_stage()
    custom_node_consumes_declared_input_is_checked()
    custom_node_consumes_present_key_is_quiet()
    custom_node_without_consumes_reads_nothing_checkable()
    render_consumes_still_hard_fails_a_provably_absent_placeholder()
    render_inline_consumes_does_not_fabricate_a_false_render_error()
    render_allow_unfilled_with_stray_consumes_is_not_blocked()
    warns_untrusted_agent_key_in_gate_ask()
    warns_untrusted_agent_key_in_render()
    warns_untrusted_agent_raw_is_authored()
    quiet_untrusted_fenced_key()
    quiet_untrusted_no_placeholders()
    quiet_untrusted_non_agent_transform_key()
    quiet_untrusted_entry_or_carried_key()
    quiet_untrusted_gate_decision_is_human_typed()
    warns_untrusted_judge_decision_at_gate()
    quiet_untrusted_engine_key_from_shell()
    quiet_untrusted_past_opaque_unknown_provenance()
    warns_untrusted_through_opaque_transform_when_agent_authors_it()
    quiet_untrusted_agent_does_not_reach_consumer()
    untrusted_message_makes_no_soundness_claim()
    untrusted_render_gate_message_does_not_prescribe_broken_fence()
    untrusted_lint_never_raises_on_malformed()
    untrusted_hits_carry_one_floor_caveat()
    untrusted_strict_fails_with_exit_2()
    render_untrusted_fires_without_allow_untrusted()
    allow_untrusted_silences_the_render_site()
    allow_untrusted_does_not_silence_a_gate_site()
    allow_untrusted_only_silences_the_render_that_sets_it()
    render_untrusted_message_names_allow_untrusted_remedy()
    gate_untrusted_message_does_not_name_allow_untrusted()
    warns_reserved_feedback_in_node_provides()
    warns_reserved_priorattempt_in_node_provides()
    warns_reserved_in_output_schema_properties()
    warns_reserved_in_output_schema_required()
    warns_reserved_in_sticky()
    warns_reserved_in_foreach_into()
    warns_reserved_in_foreach_items()
    warns_reserved_in_foreach_carry()
    warns_reserved_in_effects_from()
    warns_reserved_in_concerns_from_and_into()
    reserved_message_names_key_surface_and_injection()
    quiet_reserved_normal_config()
    quiet_reserved_loop_feedback_rename()
    quiet_reserved_node_level_carry_not_a_surface()
    quiet_reserved_human_gate_decision_schema_not_a_surface()
    reserved_lint_never_raises_on_malformed()
    warns_idempotent_default_clear()
    warns_idempotent_explicit_clear()
    clear_rollback_message_names_the_escape_hatches()
    quiet_idempotent_with_rollback()
    quiet_idempotent_with_compensate()
    quiet_idempotent_on_error_null_optout()
    quiet_non_idempotent_bare_clear()
    clear_rollback_multistage_warns_on_the_uncovered_stage()
    clear_rollback_lint_never_raises_on_malformed()
    missing_carry_fires_on_the_exact_2026_07_07_trap()
    missing_carry_silent_when_key_is_carried()
    missing_carry_silent_when_key_is_sticky()
    missing_carry_silent_when_key_in_output_schema()
    missing_carry_silent_when_key_in_provides()
    missing_carry_silent_when_key_present_in_pipeline_input_and_carried()
    missing_carry_does_not_fire_on_a_join_output()
    missing_carry_does_not_fire_on_a_fanin_union_output()
    missing_carry_is_silent_multihop_through_a_preserve_transform()
    missing_carry_does_not_fire_through_an_undeclared_envelope_transform()
    missing_carry_does_not_fire_on_a_custom_opaque_node()
    missing_carry_does_not_regress_closed_or_complete_paths()
    missing_carry_fires_on_a_branch_read_too()
    missing_carry_silent_on_a_provided_read()
    missing_carry_render_read_names_render_unfilled_consequence()
    missing_carry_branch_read_names_branch_default_consequence()
    missing_carry_foreach_read_names_foreach_input_consequence()
    missing_carry_fails_strict_with_exit_2()
    missing_carry_lint_never_raises_on_malformed()
    print("ok")


if __name__ == "__main__":
    main()
