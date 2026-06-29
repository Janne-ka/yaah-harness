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


def quiet_branch_parse_false_or_no_schema_or_non_agent() -> None:
    assert not _has_branch_warn(_branch_cfg("verdict", {"properties": {}}, parse=False))
    assert not _has_branch_warn(_branch_cfg("verdict", schema=None))
    assert not _has_branch_warn(_branch_cfg("verdict", {"properties": {}}, node_type="transform"))


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
    assert "doesn't DECLARE" in w[0] and "omits" in w[0], w[0]   # conditional, not certain
    assert "FAILS at runtime" not in w[0], w[0]                  # the old overclaim is gone


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


def quiet_render_via_branch_multipath() -> None:
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"properties": {}}, via_branch=True))


def quiet_render_via_branch_default() -> None:
    # a render reached via a branch DEFAULT is also multi-path -> skip (no false warn)
    cfg = _render_cfg("{{verdict}}", schema={"properties": {}})
    cfg["graph"]["stages"]["b"] = {"node": "agent",
                                   "branch": {"on": "x", "routes": {}, "default": "r"}}
    assert not _has_render_warn(cfg)


def quiet_render_multiple_then_preds() -> None:
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"properties": {}}, extra_preds=True))


def quiet_render_pred_not_agent_or_parse_false_or_no_schema() -> None:
    assert not _has_render_warn(_render_cfg("{{verdict}}", schema={"properties": {}}, parse=False))
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
    quiet_branch_parse_false_or_no_schema_or_non_agent()
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
    quiet_render_via_branch_multipath()
    quiet_render_via_branch_default()
    quiet_render_multiple_then_preds()
    quiet_render_pred_not_agent_or_parse_false_or_no_schema()
    quiet_render_template_file_without_base()
    warns_render_template_file_read_from_base()
    quiet_render_template_file_unreadable()
    render_template_file_resolves_against_root_dir()
    print("ok")


if __name__ == "__main__":
    main()
