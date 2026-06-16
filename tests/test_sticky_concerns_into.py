"""Graph `sticky` + stage `concerns_into` — the two engine seams added 2026-06-12.

sticky: payload keys re-folded forward when a payload-replacing stage drops
them (fill-if-missing; a stage that SETS the key wins) — the engine-level kill
for the H5 dropped-key class. concerns_into: the inverse of concerns_from — a
late stage receives a copy of baton.concerns in its input payload, so a report
renderer can show the run's soft-gate story before the terminal Done.

Run: cd yaah && PYTHONPATH=src python3 tests/test_sticky_concerns_into.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for _sticky_helpers

from yaah import Done, Envelope, Kind
from yaah.build import build
from yaah.comms import InProcessComms
from yaah.validate import validate_pipeline

CONFIG = {
    "nodes": {
        "role:sceptic":  {"type": "transform", "target": "fn:_sticky_helpers:emit_concerns", "call": "envelope"},
        "role:dropper":  {"type": "transform", "target": "fn:_sticky_helpers:drop_all", "call": "envelope"},
        "role:snapshot": {"type": "transform", "target": "fn:_sticky_helpers:snapshot", "call": "envelope"},
        "role:override": {"type": "transform", "target": "fn:_sticky_helpers:override_task", "call": "envelope"},
    },
    "graph": {
        "start": "sceptic",
        "sticky": ["task"],
        "stages": {
            "sceptic":  {"node": "role:sceptic", "concerns_from": "found", "then": "dropper"},
            "dropper":  {"node": "role:dropper", "then": "snapshot"},
            "snapshot": {"node": "role:snapshot", "concerns_into": "run_concerns", "then": "override"},
            "override": {"node": "role:override", "then": None},
        },
    },
}


async def main() -> None:
    harness = build(CONFIG, comms=InProcessComms())
    done = await harness.run(Envelope(Kind.TASK, {"task": "T-1", "raw": "x"}))
    assert isinstance(done, Done), done
    p = done.output.payload

    # sticky: dropper forgot every key; the harness re-folded task, so the
    # NEXT stage saw it (the H5 class, killed at the engine seam)
    assert p["seen_task"] == "T-1", p

    # fill-if-missing: a stage that deliberately SETS the sticky key wins
    assert p["task"] == "OVERRIDE", p

    # concerns_from consumed the raw key; concerns_into delivered the
    # accumulated, NORMALIZED concerns to the late stage's input
    assert "found" not in p, p
    rc = p["run_concerns"]
    assert rc and rc[0]["message"] == "spec smells" and rc[0]["stage"] == "sceptic", rc
    # and the existing terminal attach still works alongside
    assert p["concerns"] and p["concerns"][0]["code"] == "sceptic", p["concerns"]

    # validator: typo'd graph key + malformed concerns_into both fail at build
    bad_graph = {"nodes": CONFIG["nodes"],
                 "graph": {"start": "sceptic", "stiky": ["task"],
                           "stages": {"sceptic": {"node": "role:sceptic", "then": None}}}}
    try:
        validate_pipeline(bad_graph)
        raise SystemExit("expected unknown graph key 'stiky' to fail validation")
    except ValueError as e:
        assert "stiky" in str(e) and "sticky" in str(e), e  # with the did-you-mean

    bad_ci = {"nodes": CONFIG["nodes"],
              "graph": {"start": "s", "stages": {"s": {"node": "role:sceptic",
                                                       "concerns_into": "", "then": None}}}}
    try:
        validate_pipeline(bad_ci)
        raise SystemExit("expected empty concerns_into to fail validation")
    except ValueError as e:
        assert "concerns_into" in str(e), e

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
