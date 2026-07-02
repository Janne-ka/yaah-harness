"""Tests for config-driven spin-up (yaah.build).

Run: cd yaah && PYTHONPATH=src python3 tests/test_build.py
"""
from __future__ import annotations

import asyncio
import json

from yaah import Done, Envelope
from yaah.agents import FakeProvider, RoutingProvider
from yaah.build import build

CONFIG = {
    "nodes": {
        "role:spec": {"type": "agent", "template": "do {{task}}", "model": "fake:spec"},
        "role:json": {"type": "json_object", "required": ["x"]},
    },
    "graph": {
        "start": "spec",
        "stages": {
            "spec": {"node": "role:spec", "validators": ["role:json"],
                     "max_attempts": 3, "feedback": True, "then": None},
        },
    },
}


async def scenario_build_and_run() -> None:
    backend = RoutingProvider(
        {"fake": FakeProvider(responses=['{"x": 1', '{"x": 1}'])}, default="fake"
    )
    harness = build(CONFIG, backend=backend)
    out = await harness.run(Envelope("task", {"task": "go"}))
    assert isinstance(out, Done), out
    assert json.loads(out.output.payload["raw"]) == {"x": 1}, out.output


async def scenario_unknown_type() -> None:
    # valid graph (passes validate_pipeline) so the build reaches the bad type;
    # the build error NAMES the node (BUG-695 #6a — was a bare KeyError)
    try:
        build({"nodes": {"r": {"type": "nope"}},
               "graph": {"start": "s", "stages": {"s": {"node": "r", "then": None}}}})
        raise AssertionError("expected ValueError for unknown node type")
    except ValueError as e:
        assert "node 'r'" in str(e), e


async def scenario_builder_error_names_node() -> None:
    # BUG-695 #6a: "a 'shell' node needs 'command'" without the role forced a
    # human to diff overlay vs pipeline by hand — the role is now in the error
    try:
        build({"nodes": {"role:green-run": {"type": "shell"}},
               "graph": {"start": "s", "stages": {"s": {"node": "role:green-run", "then": None}}}})
        raise AssertionError("expected ValueError for shell without command")
    except ValueError as e:
        msg = str(e)
        assert "role:green-run" in msg and "command" in msg, msg


async def scenario_validate_pipeline() -> None:
    """Bad wiring fails fast at build time (early_review #9)."""
    bad = [
        # typo'd `then` -> not a stage
        {"nodes": {"role:a": {"type": "json_object"}},
         "graph": {"start": "a", "stages": {"a": {"node": "role:a", "then": "typo"}}}},
        # node role not declared
        {"nodes": {"role:a": {"type": "json_object"}},
         "graph": {"start": "a", "stages": {"a": {"node": "role:missing", "then": None}}}},
        # start is not a stage
        {"nodes": {"role:a": {"type": "json_object"}},
         "graph": {"start": "nope", "stages": {"a": {"node": "role:a", "then": None}}}},
        # branch route -> a non-stage
        {"nodes": {"role:a": {"type": "json_object"}},
         "graph": {"start": "a", "stages": {"a": {"node": "role:a",
                   "branch": {"on": "k", "routes": {"x": "ghost"}}}}}},
        # unknown stage key (typo) -> silent no-op, now rejected at build
        {"nodes": {"role:a": {"type": "json_object"}},
         "graph": {"start": "a", "stages": {"a": {"node": "role:a",
                   "concerns_form": "x", "then": None}}}},
    ]
    for cfg in bad:
        try:
            build(cfg)
            raise AssertionError("expected ValueError for invalid pipeline: {}".format(cfg))
        except ValueError:
            pass


async def scenario_base_dir_in_agent_tool_strings() -> None:
    """`{base_dir}` in allowed_tools / tools[].usage resolves to the config dir
    (absolute) at build time — tool scripts ship beside the config but the agent
    runs with cwd in a worktree, so the file stays relocatable while the runtime
    path is absolute. Without base_dir the placeholder is a loud config error."""
    import os
    from yaah.build.build_context import BuildContext
    from yaah.build.builders import default_registry
    from yaah.comms import InProcessComms

    spec = {"type": "agent", "template": "x",
            "allowed_tools": ["Bash(bash {base_dir}/tools/fetch.sh*)"],
            "tools": [{"name": "fetch", "impl": "fn:json:loads",
                       "usage": "Run `bash {base_dir}/tools/fetch.sh`"}]}
    ctx = BuildContext(comms=InProcessComms(),
                       backend=FakeProvider(responses=["ok"]), base_dir="rel/dir")
    agent = default_registry().build(spec, ctx)
    want = os.path.abspath("rel/dir")
    assert agent._allowed_tools == ["Bash(bash {}/tools/fetch.sh*)".format(want)], agent._allowed_tools
    assert agent._tools[0].usage == "Run `bash {}/tools/fetch.sh`".format(want), agent._tools[0].usage

    try:
        default_registry().build(spec, BuildContext(
            comms=InProcessComms(), backend=FakeProvider(responses=["ok"])))
        raise AssertionError("expected ValueError for {base_dir} without base_dir")
    except ValueError:
        pass


async def main() -> None:
    await scenario_build_and_run()
    await scenario_unknown_type()
    await scenario_builder_error_names_node()
    await scenario_validate_pipeline()
    await scenario_base_dir_in_agent_tool_strings()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
