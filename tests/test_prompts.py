"""Prompt-source layer: static/file/routing fetch, and config-driven via build().

Run: cd yaah && PYTHONPATH=src python3 tests/test_prompts.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from yaah import Done, Envelope
from yaah.agents import FakeProvider, RoutingProvider
from yaah.build import build
from yaah.prompts import RoutingPromptSource, StaticPromptSource
from yaah.adapters.prompts import FilePromptSource


async def scenario_sources() -> None:
    static = StaticPromptSource({"eval": "Eval the {what}"})
    assert await static.get("eval") == "Eval the {what}"

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "eval.md"), "w", encoding="utf-8") as f:
            f.write("FILE eval {what}")
        files = FilePromptSource(d)
        assert (await files.get("eval")) == "FILE eval {what}"

        routing = RoutingPromptSource({"static": static, "file": files}, default="static")
        assert (await routing.get("file:eval")) == "FILE eval {what}"
        assert (await routing.get("static:eval")) == "Eval the {what}"
        assert (await routing.get("eval")) == "Eval the {what}"  # default source

    try:
        await RoutingPromptSource({}).get("nope:x")
        raise AssertionError("expected LookupError")
    except LookupError:
        pass


async def scenario_agent_via_prompt_config() -> None:
    """Config references a prompt by key ('static:spec'); build() wires the source."""
    prompts = StaticPromptSource({"spec": "Return JSON. Task: {{task}}"})
    backend = RoutingProvider(
        {"fake": FakeProvider(responses=['{"x": 1', '{"x": 1}'])}, default="fake"
    )
    config = {
        "nodes": {
            "role:spec": {"type": "agent", "prompt": "static:spec", "model": "fake:spec"},
            "role:json": {"type": "json_object", "required": ["x"]},
        },
        "graph": {"start": "spec", "stages": {
            "spec": {"node": "role:spec", "validators": ["role:json"],
                     "max_attempts": 3, "feedback": True, "then": None},
        }},
    }
    routing = RoutingPromptSource({"static": prompts})
    harness = build(config, backend=backend, prompt_source=routing)
    out = await harness.run(Envelope("task", {"task": "go"}))
    assert isinstance(out, Done), out
    assert json.loads(out.output.payload["raw"]) == {"x": 1}, out.output


async def scenario_file_cache_hot_reload() -> None:
    """FilePromptSource caches by mtime: repeated gets don't re-read, but an edit
    is still picked up (hot-reload preserved) — early_review #5."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "eval.md")
        with open(p, "w") as f:
            f.write("v1")
        src = FilePromptSource(d)
        assert await src.get("eval") == "v1"
        assert await src.get("eval") == "v1"  # served from cache

        with open(p, "w") as f:
            f.write("v2-edited")
        os.utime(p, (os.path.getmtime(p) + 10, os.path.getmtime(p) + 10))  # ensure mtime differs
        assert await src.get("eval") == "v2-edited", "edit must be picked up (hot-reload)"


async def main() -> None:
    await scenario_sources()
    await scenario_agent_via_prompt_config()
    await scenario_file_cache_hot_reload()
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
