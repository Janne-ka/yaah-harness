"""overlay_lint — the deterministic gate on AI-authored `_extends` overlays.

The keystone safeguard of the AI-run goal (why-yaah.md §YAAH+AI): the proposer
is assumed prompt-injected, so the lint is deny-by-default — leaf,
non-code-equivalent changes on existing nodes pass; topology, execution surface,
safety properties, bound raises, stacking, and missing provenance are rejected.

Run: cd yaah && PYTHONPATH=src python3 tests/test_overlay_lint.py
"""
from __future__ import annotations

import json
import os
import tempfile

from yaah.overlay_lint import lint_overlay

BASE = {
    "nodes": {
        "role:think": {"type": "agent", "prompt": "file:think",
                       "model": "claude:claude-sonnet-4-6", "timeout": 60},
        "role:route": {"type": "transform", "target": "fn:app:route",
                       "call": "envelope", "config": {"max_reworks": 2}},
        "role:gate": {"type": "human_gate", "awaiting": "x",
                      "ask": "ok?"},
    },
    "graph": {"start": "think", "stages": {
        "think": {"node": "role:think", "then": None}}},
}


def _write(d: str, name: str, obj: dict) -> str:
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return p


def _lint(d: str, overlay: dict) -> list:
    _write(d, "base.json", BASE)
    return lint_overlay(_write(d, "overlay.json", overlay))


def scenario_leaf_changes_pass() -> None:
    with tempfile.TemporaryDirectory() as d:
        errs = _lint(d, {
            "_extends": "base.json", "_authored_by": "ai:claude",
            "nodes": {
                "role:think": {"model": "claude:claude-haiku-4-5",
                               "prompt": "file:think-v2",
                               "effort": "low",       # leaf string, allowed
                               "timeout": 30},        # node-level tighten = ok
                "role:route": {"config": {"max_reworks": 1}},  # tighten = ok
            }})
        assert errs == [], errs


def scenario_rejections() -> None:
    cases = [
        # (overlay-fragment, expected substring)
        ({"graph": {"start": "x"}}, "top-level key 'graph'"),
        ({"providers": {}}, "top-level key 'providers'"),
        ({"nodes": {"role:new": {"model": "m"}}}, "NEW node"),
        ({"nodes": {"role:think": None}}, "deletion"),
        ({"nodes": {"role:think": {"type": "transform"}}}, "key 'type'"),
        ({"nodes": {"role:route": {"target": "fn:os:system"}}}, "key 'target'"),
        ({"nodes": {"role:think": {"allowed_tools": ["Bash(*)"]}}}, "key 'allowed_tools'"),
        ({"nodes": {"role:think": {"validators": []}}}, "key 'validators'"),
        ({"nodes": {"role:gate": {"ask": "changed"}}}, "key 'ask'"),
        ({"nodes": {"role:route": {"config": {"max_reworks": 5}}}}, "raised 2 -> 5"),
        ({"nodes": {"role:route": {"config": {"force": True}}}}, "boolean"),
        ({"nodes": {"role:route": {"config": {"out": "/etc/x"}}}}, "non-numeric"),
        ({"nodes": {"role:think": {"timeout": 120}}}, "raised 60 -> 120"),
        # a numeric bound ABSENT from base has no ceiling to tighten against, so
        # introducing one must be deny-by-default (else the gate fails open: an
        # AI overlay could set an arbitrarily large timeout/retries on any node
        # whose base happened to leave the bound unset).
        ({"nodes": {"role:route": {"timeout": 5}}}, "absent from base"),
        ({"nodes": {"role:gate": {"retries": 99}}}, "absent from base"),
        ({"nodes": {"role:route": {"temperature": 0.1}}}, "absent from base"),
        ({"nodes": {"role:think": {"timeout": "9000"}}}, "must be a number"),
        ({"nodes": {"role:think": {"effort": 3}}}, "must be a string"),
    ]
    with tempfile.TemporaryDirectory() as d:
        for fragment, needle in cases:
            overlay = {"_extends": "base.json", "_authored_by": "ai:claude"}
            overlay.update(fragment)
            errs = _lint(d, overlay)
            assert any(needle in e for e in errs), (fragment, needle, errs)


def scenario_provenance_and_stacking() -> None:
    with tempfile.TemporaryDirectory() as d:
        # missing provenance
        errs = _lint(d, {"_extends": "base.json",
                         "nodes": {"role:think": {"model": "m"}}})
        assert any("_authored_by" in e for e in errs), errs
        # AI-on-AI stacking rejected
        _write(d, "base.json", BASE)
        mid = _write(d, "mid.json", {"_extends": "base.json", "_authored_by": "ai:claude",
                                     "nodes": {"role:think": {"model": "m1"}}})
        top = _write(d, "top.json", {"_extends": "mid.json", "_authored_by": "ai:claude",
                                     "nodes": {"role:think": {"model": "m2"}}})
        errs = lint_overlay(top)
        assert any("no stacking" in e for e in errs), errs
        assert mid  # silence unused warning


def main() -> None:
    scenario_leaf_changes_pass()
    scenario_rejections()
    scenario_provenance_and_stacking()
    print("ok")


if __name__ == "__main__":
    main()
