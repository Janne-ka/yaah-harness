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


# ── 1a edge-soundness: branch on a key the agent doesn't provide ─────────────

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
    print("ok")


if __name__ == "__main__":
    main()
