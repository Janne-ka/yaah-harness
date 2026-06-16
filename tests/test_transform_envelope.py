"""Transform — the envelope-style realization (`call: "envelope"`) that subsumed
the former standalone `python` node.

A deterministic step is now just a `transform` with `target: "fn:mod:func"` and
`call: "envelope"`: the fn receives (envelope, config) and its result spreads over
the payload TOP-LEVEL (so a following `branch` can read the keys it sets); a
returned Envelope passes through. The default `call: "args"` mode (fn(args) → `into`)
is unchanged and covered by test_post_transform.

Run: cd yaah && PYTHONPATH=src python3 tests/test_transform_envelope.py
"""
from __future__ import annotations

import asyncio

from yaah import Done, Envelope, Kind, NodeConfig
from yaah.build import build
from yaah.nodes import TransformNode


def dedup(input: Envelope, config: NodeConfig) -> dict:
    """A config-aware dict-returning fn (reads the whole envelope)."""
    seen, out = set(), []
    for f in input.payload.get("findings", []):
        if f.get("desc") not in seen:
            seen.add(f.get("desc"))
            out.append(f)
    return {"findings": out, "count": len(out)}


async def adouble(input: Envelope, config: NodeConfig) -> Envelope:
    """An async fn returning an Envelope (passes through unchanged)."""
    return input.reply(Kind.RESULT, n=input.payload["n"] * 2)


def uses_config(input: Envelope, config: NodeConfig) -> dict:
    """Proves the fn gets the real NodeConfig (e.g. config.extras)."""
    extras = getattr(config, "extras", {}) or {}
    return {"status": extras.get("status", "DONE")}


def addone(args) -> dict:
    """A default-mode (call='args') fn — receives args, not the envelope."""
    return {"v": args["n"] + 1}


async def main() -> None:
    cfg = NodeConfig()

    # sync dict return → spread top-level
    out = await TransformNode("fn:__main__:dedup", call="envelope").invoke(
        Envelope(Kind.TASK, {"findings": [{"desc": "a"}, {"desc": "a"}, {"desc": "b"}]}), cfg)
    assert out.payload["count"] == 2 and "findings" in out.payload, out.payload
    assert "result" not in out.payload, "envelope mode spreads top-level, not under into"

    # async Envelope return → passes through
    out2 = await TransformNode("fn:__main__:adouble", call="envelope").invoke(
        Envelope(Kind.TASK, {"n": 21}), cfg)
    assert out2.payload["n"] == 42, out2.payload

    # default mode still nests under `into` (no regression)
    out3 = await TransformNode("fn:__main__:addone").invoke(Envelope(Kind.TASK, {"n": 1}), cfg)
    assert out3.payload["result"] == {"v": 2}, out3.payload

    # only fn: targets allow envelope mode
    raised = False
    try:
        await TransformNode("node:role:x", call="envelope").invoke(Envelope(Kind.TASK, {}), cfg)
    except ValueError:
        raised = True
    assert raised, "envelope mode must reject a non-fn target"

    # via build config (the migrated shape) — branch reads a top-level key the fn set
    config = {
        "nodes": {"role:dd": {"type": "transform", "target": "fn:__main__:dedup", "call": "envelope"}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:dd"}}},
    }
    res = await build(config).run(Envelope(Kind.TASK, {"findings": [{"desc": "x"}, {"desc": "x"}]}))
    assert isinstance(res, Done) and res.output.payload["count"] == 1, res

    # config.extras reaches the fn through the node config
    cfg2 = {
        "nodes": {"role:m": {"type": "transform", "target": "fn:__main__:uses_config",
                             "call": "envelope", "config": {"status": "BLOCKED"}}},
        "graph": {"start": "s", "stages": {"s": {"node": "role:m"}}},
    }
    res2 = await build(cfg2).run(Envelope(Kind.TASK, {}))
    assert isinstance(res2, Done) and res2.output.payload["status"] == "BLOCKED", res2

    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
