"""Trace file sinks — ProgressFileSink (live tailable stage lines) and
StatsFileSink (the rolling aggregate snapshot). Both are plain TraceSink
subscribers: handle(envelope-with-record-in-payload).

Run: cd yaah && PYTHONPATH=src python3 tests/test_trace_sinks.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah.core import Envelope, Kind
from yaah.adapters.trace import ProgressFileSink, StatsFileSink


def _rec(**kw):
    return Envelope(Kind.RESULT, dict(kw))


async def scenario_progress_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "progress.log")
        sink = ProgressFileSink(path, clock=lambda: 0.0)  # fixed clock for a stable ts
        await sink.handle(_rec(name="stage", stage="spec", status="ok", duration_ms=12.0))
        await sink.handle(_rec(name="model_call", model="x", tokens_in=10))  # ignored
        await sink.handle(_rec(name="stage", stage="review", status="suspended", duration_ms=0.0))

        lines = open(path, encoding="utf-8").read().splitlines()
        assert len(lines) == 2, lines                       # only the two stage spans
        assert "spec" in lines[0] and "ok" in lines[0] and "12ms" in lines[0], lines[0]
        assert "review" in lines[1] and "suspended" in lines[1], lines[1]
        assert "model_call" not in "\n".join(lines)


async def scenario_progress_appends() -> None:
    # a second run/handle APPENDS (tail -f keeps working across the run)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.log")
        s = ProgressFileSink(path, clock=lambda: 0.0)
        await s.handle(_rec(name="stage", stage="a", status="ok", duration_ms=1.0))
        await s.handle(_rec(name="stage", stage="b", status="ok", duration_ms=2.0))
        assert len(open(path, encoding="utf-8").read().splitlines()) == 2


async def scenario_stats_file_rolls_up() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "stats.json")
        price = {"m": {"input": 1.0, "output": 2.0}}  # $/1k
        sink = StatsFileSink(path, price_map=price)
        await sink.handle(_rec(name="stage", stage="eval", status="ok", duration_ms=100.0, corr="R"))
        await sink.handle(_rec(name="model_call", model="m", tokens_in=1000, tokens_out=1000, corr="R"))

        snap = json.load(open(path, encoding="utf-8"))
        t = snap["totals"]
        assert t["model_calls"] == 1 and t["stage_spans"] == 1, t
        assert t["tokens_in"] == 1000 and t["tokens_out"] == 1000, t
        assert abs(t["cost_usd"] - 3.0) < 1e-9, t            # 1k*$1 + 1k*$2
        assert "eval" in snap["stages"], snap["stages"]
        assert t["retries"] == 0, t


async def scenario_stats_file_overwrites_with_latest() -> None:
    # the file holds the CURRENT rollup — a later span updates it (overwrite, not append)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.json")
        sink = StatsFileSink(path)
        await sink.handle(_rec(name="stage", stage="a", status="ok", duration_ms=1.0, corr="R"))
        assert json.load(open(path))["totals"]["stage_spans"] == 1
        await sink.handle(_rec(name="stage", stage="b", status="ok", duration_ms=1.0, corr="R"))
        assert json.load(open(path))["totals"]["stage_spans"] == 2  # latest snapshot, not appended


async def scenario_progress_file_suspend_shows_awaiting() -> None:
    # A suspended stage span carries `awaiting`; the line MUST surface it so
    # the operator tailing progress.log doesn't have to `yaah list` separately
    # to see what just parked. Other statuses keep the legacy short form.
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "progress.log")
        sink = ProgressFileSink(path, clock=lambda: 0.0)
        await sink.handle(_rec(name="stage", stage="extract", status="ok", duration_ms=12.0))
        await sink.handle(_rec(name="stage", stage="review", status="suspended",
                               duration_ms=3.0, awaiting="human:arch-review"))
        lines = open(path, encoding="utf-8").read().splitlines()
        assert "awaiting" not in lines[0], lines[0]            # ok status: no awaiting label
        assert "awaiting=human:arch-review" in lines[1], lines[1]
        assert "suspended" in lines[1], lines[1]


async def main() -> None:
    await scenario_progress_file()
    await scenario_progress_appends()
    await scenario_progress_file_suspend_shows_awaiting()
    await scenario_stats_file_rolls_up()
    await scenario_stats_file_overwrites_with_latest()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
