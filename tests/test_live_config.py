"""Live leaf-config re-read (live-vars mechanism (a)) — edit the pipeline
file, the next invocation picks up the MUTABLE leaves; everything
code-equivalent stays constructor-frozen.

Run: cd yaah && PYTHONPATH=src python3 tests/test_live_config.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # _live_helpers

from yaah import Done, Envelope  # noqa: E402
from yaah.build import build  # noqa: E402


def _pipeline(model: str, timeout: float, n: int, name: str,
              target: str = "fn:_live_helpers:echo_config") -> dict:
    return {
        "nodes": {"role:e": {"type": "transform", "target": target,
                             "call": "envelope", "model": model,
                             "timeout": timeout, "config": {"n": n, "name": name}}},
        "graph": {"start": "s1", "stages": {"s1": {"node": "role:e"}}},
    }


def _write(path: str, cfg: dict, *, mtime_bump: float) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    # mtime has 1s granularity on some filesystems — force a visible change
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + mtime_bump))


async def scenario_live_edit_takes_effect_next_call() -> None:
    """Mutable leaves (model, timeout, numeric config) refresh per call from
    the file; non-numeric config and the fn: target stay frozen-at-build —
    a file edit must never become a code-execution channel."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.json")
        _write(path, _pipeline("m1", 10, 1, "x"), mtime_bump=0)
        h = build(_pipeline("m1", 10, 1, "x"), live_config_path=path)

        out = await h.run(Envelope("task", {}))
        assert isinstance(out, Done), out
        p = out.output.payload
        assert p["seen_model"] == "m1" and p["seen_n"] == 1, p

        # edit: new model/timeout/n (mutable) + new name and an EVIL target
        # (code-equivalent — must NOT be adopted)
        _write(path, _pipeline("m2", 20, 5, "evil-name", target="fn:os:system"),
               mtime_bump=2)
        out = await h.run(Envelope("task", {}))
        p = out.output.payload  # still echo_config — target is constructor-frozen
        assert p["seen_model"] == "m2", p
        assert p["seen_timeout"] == 20, p
        assert p["seen_n"] == 5, p
        assert p["seen_name"] == "x", p  # non-numeric config stays as built


async def scenario_without_live_flag_stays_frozen() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.json")
        cfg = _pipeline("m1", 10, 1, "x")
        _write(path, cfg, mtime_bump=0)
        h = build(cfg)  # no live_config_path = today's frozen behavior
        _write(path, _pipeline("m2", 20, 5, "y"), mtime_bump=2)
        out = await h.run(Envelope("task", {}))
        p = out.output.payload
        assert p["seen_model"] == "m1" and p["seen_n"] == 1, p


async def scenario_unreadable_file_keeps_last_known() -> None:
    """A config re-read must never kill a running pipeline: deleted or
    mid-edit-garbled file -> the last known leaves stay in effect."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "p.json")
        _write(path, _pipeline("m1", 10, 1, "x"), mtime_bump=0)
        h = build(_pipeline("m1", 10, 1, "x"), live_config_path=path)
        out = await h.run(Envelope("task", {}))
        assert out.output.payload["seen_model"] == "m1"

        with open(path, "w", encoding="utf-8") as f:
            f.write("{ mid-edit garble")
        st = os.stat(path)
        os.utime(path, (st.st_atime, st.st_mtime + 2))
        out = await h.run(Envelope("task", {}))
        assert out.output.payload["seen_model"] == "m1", out.output.payload

        os.remove(path)
        out = await h.run(Envelope("task", {}))
        assert out.output.payload["seen_model"] == "m1", out.output.payload


async def main() -> None:
    await scenario_live_edit_takes_effect_next_call()
    await scenario_without_live_flag_stays_frozen()
    await scenario_unreadable_file_keeps_last_known()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
