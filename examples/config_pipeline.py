"""Spin up a one-stage pipeline from declarative config — no manual wiring.

Shows yaah.build.build(): describe nodes + graph as a dict (load from JSON/YAML
in real use); build() constructs and registers everything and returns a ready
Harness. Switching a node from fake to claude is editing the 'model' string.

Run: cd yaah && PYTHONPATH=src python3 examples/config_pipeline.py
"""
from __future__ import annotations

import asyncio

from yaah import Done, Envelope
from yaah.agents import FakeBackend, RoutingBackend
from yaah.build import build, default_registry

SPEC_PROMPT = "You are a spec worker. Return JSON with keys summary, items.\nTask: {{task}}\n"

# This is the whole pipeline definition. In real use it is a JSON/YAML file.
CONFIG = {
    "nodes": {
        "role:spec": {
            "type": "agent",
            "template": SPEC_PROMPT,
            "model": "fake:spec",   # change to "claude:claude-sonnet-4-6" to use real Claude
            "effort": "low",
            "stage": "spec",
        },
        "role:json": {"type": "json_object", "required": ["summary", "items"]},
    },
    "graph": {
        "start": "spec",
        "stages": {
            "spec": {
                "node": "role:spec",
                "validators": ["role:json"],
                "max_attempts": 3,
                "feedback": True,
                "then": None,
            }
        },
    },
}


async def main() -> None:
    backend = RoutingBackend(
        {
            "fake": FakeBackend(responses=[
                '{"summary": "ok", "items": ["a"  ',     # invalid -> triggers retry
                '{"summary": "ok", "items": ["a"]}',     # valid
            ]),
            # "claude": ClaudeCliBackend(),   # then set role:spec model to "claude:claude-sonnet-4-6"
        },
        default="fake",
    )

    harness = build(CONFIG, backend=backend, registry=default_registry())

    async def on_event(e: Envelope) -> None:
        print("[event] {}: {}".format(e.payload.get("stage"), e.payload.get("msg")))

    harness.comms.subscribe("events", on_event)

    print("--- pipeline built from config, running ---")
    out = await harness.run(Envelope("task", {"task": "review the diff"}))
    assert isinstance(out, Done), out
    print("DONE:", out.output.payload["raw"])
    print("ok")


if __name__ == "__main__":
    asyncio.run(main())
