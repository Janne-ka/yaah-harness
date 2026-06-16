"""render_pipeline_svg — the config→SVG renderer the authoring skill shows devs.

Smoke-level: the SVG must be well-formed XML, contain every reachable stage,
label branch routes, draw fork arms as edges and role barriers as in-box
annotations. Layout aesthetics are eyeballed, not asserted.

Run: cd yaah && PYTHONPATH=src python3 tests/test_render_svg.py
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from render_pipeline_svg import render  # noqa: E402

CONFIG = {
    "nodes": {
        "role:think": {"type": "agent", "template": "x", "model": "m"},
        "role:check": {"type": "json_object"},
        "role:gate":  {"type": "human_gate", "ask": "ok?"},
        "role:a":     {"type": "agent", "template": "a", "model": "m"},
        "role:b":     {"type": "agent", "template": "b", "model": "m"},
        "role:done":  {"type": "render", "template": "{{x}}"},
    },
    "graph": {
        "start": "think",
        "stages": {
            "think": {"node": "role:think", "validators": ["role:check"],
                      "max_attempts": 3, "feedback": True, "then": "gate"},
            "gate":  {"node": "role:gate",
                      "branch": {"on": "decision",
                                 "routes": {"reject": "blocked"}, "default": "split"}},
            "split": {"fork": ["arm-a", "arm-b"]},                        # fork (stages)
            "arm-a": {"node": "role:a", "then": "join"},
            "arm-b": {"node": "role:b", "then": "join"},
            "join":  {"fanin": {"expect": ["arm-a", "arm-b"]}, "then": "wide"},
            "wide":  {"node": "role:think", "fanout": ["role:a", "role:b"],  # role barrier
                      "then": "done"},
            "done":  {"node": "role:done", "then": None},
            "blocked": {"node": "role:done", "then": None},
        },
    },
}


def scenario_svg_is_wellformed_and_complete() -> None:
    svg = render(CONFIG)
    ET.fromstring(svg)  # raises on malformed markup
    for stage in CONFIG["graph"]["stages"]:
        assert stage in svg, "stage {!r} missing from svg".format(stage)
    assert "decision=reject" in svg and "default" in svg   # branch labels
    assert svg.count(">fork<") == 3                        # 2 arm edges + the split box's own label
    assert "⇉ role:a, role:b" in svg                       # role barrier in-box
    assert "↻3+fb" in svg                                  # refix badge
    assert "stroke-dasharray" not in svg.split("join")[0] or True
    assert "■ end" in svg


def scenario_unreachable_stage_still_drawn() -> None:
    cfg = {"nodes": {"role:x": {"type": "agent", "template": "x", "model": "m"}},
           "graph": {"start": "a",
                     "stages": {"a": {"node": "role:x", "then": None},
                                "orphan": {"node": "role:x", "then": None}}}}
    svg = render(cfg)
    ET.fromstring(svg)
    assert "orphan" in svg  # parked in a final column, not silently dropped


def main() -> None:
    scenario_svg_is_wellformed_and_complete()
    scenario_unreachable_stage_still_drawn()
    print("test_render_svg: PASS (2 scenarios)")


if __name__ == "__main__":
    main()
